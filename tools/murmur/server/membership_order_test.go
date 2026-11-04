package main

import (
	"encoding/json"
	"testing"
)

func assertLeft(t *testing.T, p *Plugin, f *fakeAPI, actor, channel string) {
	t.Helper()
	if f.members[channel][p.state.Agents[actor].UserID] {
		t.Fatal("completed leave was undone in native membership")
	}
	if _, ok := p.state.Conversations[channel].Grants[actor]; ok {
		t.Fatal("completed leave was undone in Murmur grants")
	}
	if !p.state.Conversations[channel].Left[actor] {
		t.Fatal("completed leave lost its departure marker")
	}
}

func TestLeaveSettlesLostJoinBeforeAcceptingNewIntentAndSurvivesRestart(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "ordering-room", "claude:b")
	mustRegister(t, p, "codex:child", "codex:a")
	f.addFault = true
	_, err := p.membership(channel, "join", memberRequest{Actor: "codex:child", OpID: "old-join"})
	if err == nil || !f.members[channel][p.state.Agents["codex:child"].UserID] {
		t.Fatal("join response loss did not occur after native addition")
	}
	if p.state.Operations["old-join"].Status != "pending" {
		t.Fatal("lost join did not retain journal")
	}
	// A still-failing earlier effect must block before a new leave is allocated
	// or persisted. The caller can retry its same previously unaccepted ID.
	f.addFault = true
	_, err = p.membership(channel, "leave", memberRequest{Actor: "codex:child", OpID: "new-leave"})
	status(t, err, 409)
	var persisted State
	if err := json.Unmarshal(f.kv, &persisted); err != nil {
		t.Fatal(err)
	}
	if p.state.Operations["new-leave"] != nil || persisted.Operations["new-leave"] != nil {
		t.Fatal("blocked leave was accepted before earlier join settled")
	}
	_, err = p.membership(channel, "leave", memberRequest{Actor: "codex:child", OpID: "new-leave"})
	if err != nil {
		t.Fatal(err)
	}
	if p.state.Operations["old-join"].Status != "complete" {
		t.Fatal("successful leave left an earlier conflicting join pending")
	}
	assertLeft(t, p, f, "codex:child", channel)
	p = reload(t, p, f)
	assertLeft(t, p, f, "codex:child", channel)
}

func TestLeaveSettlesPartiallyCreatedConversationBeforeRestart(t *testing.T) {
	p, f := fixture(t)
	// Sorted members put claude:b first. Its grant is saved before the second
	// native add commits and loses its response, interrupting room creation.
	f.addFaultAt = f.addCalls + 2
	_, err := p.conversation(conversationRequest{Actor: "claude:b", OpID: "partial-create", Kind: "channel", Name: "Interrupted creation", Members: []string{"codex:a"}})
	if err == nil {
		t.Fatal("second native add did not fail")
	}
	creation := p.state.Operations["partial-create"]
	channel := creation.ChannelID
	if creation.Status != "pending" || p.state.Conversations[channel].Grants["claude:b"].Role != "member" || !f.members[channel][p.state.Agents["codex:a"].UserID] {
		t.Fatal("fixture did not reach partially saved membership with committed second add")
	}
	_, err = p.membership(channel, "leave", memberRequest{Actor: "claude:b", OpID: "leave-partial"})
	if err != nil {
		t.Fatal(err)
	}
	assertLeft(t, p, f, "claude:b", channel)
	p = reload(t, p, f)
	assertLeft(t, p, f, "claude:b", channel)
	if p.state.Operations["partial-create"].Status != "complete" {
		t.Fatal("leave overtook unfinished room membership journal")
	}
	if p.state.Conversations[channel].Grants["codex:a"].Role != "member" {
		t.Fatal("settling room creation lost other participant")
	}
}

func TestJoinSettlesLostLeaveBeforeAcceptingNewIntent(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "rejoin-ordering-room", "claude:b")
	f.deleteMemberFault = true
	_, err := p.membership(channel, "leave", memberRequest{Actor: "codex:a", OpID: "old-leave"})
	if err == nil || f.members[channel][p.state.Agents["codex:a"].UserID] {
		t.Fatal("leave response loss did not occur after native removal")
	}
	_, err = p.membership(channel, "join", memberRequest{Actor: "codex:a", OpID: "new-join"})
	if err != nil {
		t.Fatal(err)
	}
	p = reload(t, p, f)
	if p.state.Operations["old-leave"].Status != "complete" || !f.members[channel][p.state.Agents["codex:a"].UserID] || p.state.Conversations[channel].Grants["codex:a"].Role != "member" || p.state.Conversations[channel].Left["codex:a"] {
		t.Fatal("earlier leave replay undid the newer successful join")
	}
}

func TestUnknownDirectChannelJournalIsSettledOnlyForMatchingPair(t *testing.T) {
	for _, samePair := range []bool{true, false} {
		name := "same-pair"
		if !samePair {
			name = "other-pair"
		}
		t.Run(name, func(t *testing.T) {
			p, f := fixture(t)
			dm, err := p.conversation(conversationRequest{Actor: "codex:a", OpID: "original-dm", Kind: "dm", Members: []string{"claude:b"}})
			if err != nil {
				t.Fatal(err)
			}
			channel := dm["conversation"].(map[string]any)["id"].(string)
			peer := "claude:b"
			if !samePair {
				peer = "codex:x"
			}
			f.directFault = true
			_, err = p.conversation(conversationRequest{Actor: "codex:a", OpID: "uncertain-dm", Kind: "dm", Members: []string{peer}})
			if err == nil || p.state.Operations["uncertain-dm"].ChannelID != "" {
				t.Fatal("direct-channel response loss did not leave unknown journal target")
			}
			_, err = p.membership(channel, "leave", memberRequest{Actor: "codex:a", OpID: "leave-dm"})
			if err != nil {
				t.Fatal(err)
			}
			wantStatus := "pending"
			if samePair {
				wantStatus = "complete"
			}
			if p.state.Operations["uncertain-dm"].Status != wantStatus {
				t.Fatal("DM ordering did not distinguish native fixed pair")
			}
			p = reload(t, p, f)
			if _, ok := p.state.Conversations[channel].Grants["codex:a"]; ok || !p.state.Conversations[channel].Left["codex:a"] {
				t.Fatal("direct-channel replay restored relinquished visibility")
			}
		})
	}
}

func TestFreshMembershipDoesNotSettleUnrelatedPendingJoin(t *testing.T) {
	p, f := fixture(t)
	first := channelConversation(t, p, "first-room", "claude:b")
	second := channelConversation(t, p, "second-room", "claude:b")
	mustRegister(t, p, "codex:child", "codex:a")
	f.addFault = true
	_, err := p.membership(first, "join", memberRequest{Actor: "codex:child", OpID: "unrelated-join"})
	if err == nil {
		t.Fatal("join failure not injected")
	}
	before := f.addCalls
	_, err = p.membership(second, "leave", memberRequest{Actor: "codex:child", OpID: "independent-leave"})
	if err != nil {
		t.Fatal(err)
	}
	if f.addCalls != before || p.state.Operations["unrelated-join"].Status != "pending" {
		t.Fatal("membership command reconciled an unrelated channel")
	}
}
