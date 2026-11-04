package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/mattermost/mattermost/server/public/model"
)

func openAgentOperatorDM(t *testing.T, p *Plugin, actor, op string) map[string]any {
	t.Helper()
	result := agentRequest(t, p, "POST", "operator-dm", memberRequest{Actor: actor, OpID: op}, 200)
	c := result["conversation"].(map[string]any)
	if len(c) != 3 || c["kind"] != "dm" {
		t.Fatalf("opening must return immutable channel metadata: %#v", result)
	}
	return c
}

func TestAgentOperatorDMRejectsUnconfiguredUnauthorizedAndInjectedTargets(t *testing.T) {
	for _, failure := range []string{"disabled", "invalid-id", "missing-human", "deleted-human", "bot-human", "not-admin", "no-permission", "unenrolled", "foreign-bot"} {
		t.Run(failure, func(t *testing.T) {
			p, f, user := dmFixture(t)
			p.config.OperatorUserID = user.Id
			actor, want := "codex:a", 403
			switch failure {
			case "disabled":
				p.config.OperatorUserID = ""
			case "invalid-id":
				p.config.OperatorUserID, want = "username", 409
			case "missing-human":
				delete(f.users, user.Username)
			case "deleted-human":
				user.DeleteAt = 1
			case "bot-human":
				user.IsBot = true
			case "not-admin":
				user.Roles = "system_user"
			case "no-permission":
				f.permission = false
			case "unenrolled":
				actor = "codex:absent"
			case "foreign-bot":
				f.bots[p.state.Agents[actor].UserID].OwnerId, want = "other-plugin", 409
			}
			agentRequest(t, p, "POST", "operator-dm", memberRequest{Actor: actor, OpID: "denied"}, want)
			if f.directCalls != 0 || p.state.Operations["denied"] != nil {
				t.Fatal("invalid fresh request reached journal or native pair creation")
			}
		})
	}
	p, f, user := dmFixture(t)
	p.config.OperatorUserID = user.Id
	for _, field := range []string{"human_user_id", "operator_user_id", "agent_id", "bot_user_id", "channel_id", "causal"} {
		agentRequest(t, p, "POST", "operator-dm", map[string]any{"actor": "codex:a", "op_id": "injected", field: user.Id}, 400)
	}
	if f.directCalls != 0 || p.state.Operations["injected"] != nil {
		t.Fatal("caller selected a target")
	}
}

func TestAgentOperatorDMDurableOwnPairQuietAdoptionAndFutureMail(t *testing.T) {
	p, f, human := dmFixture(t)
	p.config.OperatorUserID = human.Id
	mustRegister(t, p, "codex:child", "codex:a")
	bot := p.state.Agents["codex:a"].UserID
	native, _ := f.fakeAPI.GetDirectChannel(human.Id, bot)
	old, _ := f.CreatePost(&model.Post{ChannelId: native.Id, UserId: human.Id, Message: "existing history"})
	f.beforeDirect = func() {
		var disk State
		if err := json.Unmarshal(f.kv, &disk); err != nil {
			t.Fatal(err)
		}
		o := disk.Operations["open"]
		if o == nil || o.Kind != "agent_operator_dm" || o.Actor != "codex:a" || o.AgentID != "codex:a" || o.HumanUserID != human.Id || o.BotUserID != bot {
			t.Fatal("native effect preceded durable agent-owned pinned intent")
		}
	}
	opened := openAgentOperatorDM(t, p, "codex:a", "open")
	channel := opened["id"].(string)
	if channel != native.Id || f.creates != 1 || len(p.state.Agents) != 4 || len(f.members[channel]) != 2 || !f.members[channel][human.Id] || !f.members[channel][bot] {
		t.Fatal("open sent text, enrolled human, or selected the wrong native pair")
	}
	if !p.state.Observed[old.Id].Historical || p.state.Conversations[channel].Grants["codex:child"].Role != "observer" || f.members[channel][p.state.Agents["codex:child"].UserID] {
		t.Fatal("adoption did not preserve quiet reference inheritance")
	}
	// A child observing the parent's pair may still open its own distinct pair.
	f.beforeDirect = nil
	child := openAgentOperatorDM(t, p, "codex:child", "child-open")["id"].(string)
	if child == channel || p.state.Conversations[child].Grants["codex:child"].Role != "member" {
		t.Fatal("observer could not open its own human DM")
	}
	sent := send(t, p, "codex:a", channel, "agent-send", "first agent message", "")
	if f.posts[sent].UserId != bot {
		t.Fatal("agent send impersonated the human")
	}
	fresh, _ := f.CreatePost(&model.Post{ChannelId: channel, UserId: human.Id, Message: "human reply"})
	p = reloadDM(t, p, f)
	eligibility := map[string]bool{}
	for _, raw := range changesFor(t, p, "codex:a")["changes"].([]any) {
		entry := raw.(map[string]any)
		eligibility[entry["post"].(map[string]any)["id"].(string)] = entry["notification_eligible"].(bool)
	}
	if eligibility[old.Id] || !eligibility[fresh.Id] || p.state.Observed[fresh.Id].Historical {
		t.Fatal("human reply did not enter the ordinary fresh attention feed")
	}
	for _, raw := range changesFor(t, p, "codex:child")["changes"].([]any) {
		entry := raw.(map[string]any)
		if entry["notification_eligible"] == true {
			t.Fatal("inherited parent DM generated child attention")
		}
	}
}

func TestAgentOperatorDMPendingRevocationIsLocalAndNeverReroutes(t *testing.T) {
	for _, change := range []string{"disabled", "changed-human", "mapping-drift", "deleted-human", "during-snapshot"} {
		t.Run(change, func(t *testing.T) {
			p, f, user := dmFixture(t)
			p.config.OperatorUserID = user.Id
			channelConversation(t, p, "healthy-room", "claude:b")
			if change == "during-snapshot" {
				p.history = &dmSnapshot{base: f.fakeAPI, after: func() { p.config.OperatorUserID = "" }}
				agentRequest(t, p, "POST", "operator-dm", memberRequest{Actor: "codex:a", OpID: "held"}, 409)
			} else {
				f.directFault = true
				agentRequest(t, p, "POST", "operator-dm", memberRequest{Actor: "codex:a", OpID: "held"}, 502)
				switch change {
				case "disabled":
					p.config.OperatorUserID = ""
				case "changed-human":
					p.config.OperatorUserID = model.NewId()
				case "mapping-drift":
					p.state.Agents["codex:a"].UserID = p.state.Agents["claude:b"].UserID
				case "deleted-human":
					user.DeleteAt = 1
				}
				agentRequest(t, p, "GET", "operations/held?actor=codex:a", nil, 200)
			}
			o := p.state.Operations["held"]
			if o.Status != "failed" || o.HumanUserID != user.Id || f.directCalls != 1 || len(f.channels) != 2 || (o.ChannelID != "" && p.state.Conversations[o.ChannelID] != nil) {
				t.Fatal("revoked intent rerouted, retried the native effect, or published access")
			}
			p.reconcile()
			if p.lastErr != "" || p.fault != "" {
				t.Fatal("local conflict poisoned unrelated repair")
			}
			changesFor(t, p, "claude:b")
		})
	}
}

func TestAgentOperatorDMRestartCompletedRetryRenameAndDeparture(t *testing.T) {
	p, f, human := dmFixture(t)
	p.config.OperatorUserID = human.Id
	f.directFault = true
	agentRequest(t, p, "POST", "operator-dm", memberRequest{Actor: "codex:a", OpID: "lost"}, 502)
	p = reloadDM(t, p, f)
	result := agentRequest(t, p, "GET", "operations/lost?actor=codex:a", nil, 200)
	channel := result["result"].(map[string]any)["channel_id"].(string)
	if result["status"] != "complete" || f.directCalls != 2 || len(f.channels) != 1 {
		t.Fatal("restart did not recover the pinned native pair")
	}
	before := openAgentOperatorDM(t, p, "codex:a", "lost")
	delete(f.users, human.Username)
	human.Username = "renamed-human"
	f.users[human.Username] = human
	if openAgentOperatorDM(t, p, "codex:a", "after-rename")["id"] != channel {
		t.Fatal("human rename split stable-ID pair")
	}
	if _, err := p.membership(channel, "leave", memberRequest{Actor: "codex:a", OpID: "depart"}); err != nil {
		t.Fatal(err)
	}
	p.config.OperatorUserID = ""
	human.DeleteAt = 1
	calls := f.directCalls
	stateBefore, _ := json.Marshal(p.state)
	after := openAgentOperatorDM(t, p, "codex:a", "lost")
	stateAfter, _ := json.Marshal(p.state)
	if digest(before) != digest(after) || !bytes.Equal(stateBefore, stateAfter) || f.directCalls != calls {
		t.Fatal("completed retry changed historical result, revalidated configuration, or restored membership")
	}
	agentRequest(t, p, "GET", "operations/lost?actor=claude:b", nil, 403)
	p.config.OperatorUserID, human.DeleteAt = human.Id, 0
	operatorWant(t, p, human.Id, "GET", "operations/lost", nil, 403)
	agentRequest(t, p, "POST", "operator-dm", memberRequest{Actor: "codex:a", OpID: "new-after-leave"}, 409)
	if !p.state.Conversations[channel].Left["codex:a"] || p.state.Conversations[channel].Grants["codex:a"].Role != "" {
		t.Fatal("fresh opening rejoined departed agent")
	}
}

func TestAgentOperatorDMRejectsOwnManagedDriftAndWrongNativePair(t *testing.T) {
	for _, drift := range []string{"missing", "observer", "third-member", "wrong-pair"} {
		t.Run(drift, func(t *testing.T) {
			p, f, human := dmFixture(t)
			p.config.OperatorUserID = human.Id
			channel := openAgentOperatorDM(t, p, "codex:a", "first")["id"].(string)
			c := p.state.Conversations[channel]
			switch drift {
			case "missing":
				delete(c.Grants, "codex:a")
			case "observer":
				g := c.Grants["codex:a"]
				g.Role = "observer"
				c.Grants["codex:a"] = g
			case "third-member":
				f.members[channel][p.state.Agents["claude:b"].UserID] = true
			case "wrong-pair":
				f.wrongPair, _ = f.fakeAPI.GetDirectChannel(human.Id, p.state.Agents["claude:b"].UserID)
			}
			before, _ := json.Marshal(c)
			agentRequest(t, p, "POST", "operator-dm", memberRequest{Actor: "codex:a", OpID: "no-repair"}, 409)
			after, _ := json.Marshal(c)
			if !bytes.Equal(before, after) || p.state.Operations["no-repair"].Status != "failed" {
				t.Fatal("opening repaired an inconsistent managed/native pair")
			}
		})
	}
}

func TestAgentAndHumanOperatorDMOpenOrderingConvergesAcrossKinds(t *testing.T) {
	for _, agentFirst := range []bool{true, false} {
		t.Run(fmt.Sprint(agentFirst), func(t *testing.T) {
			p, f, human := dmFixture(t)
			p.config.OperatorUserID = human.Id
			f.directFault = true
			if agentFirst {
				agentRequest(t, p, "POST", "operator-dm", memberRequest{Actor: "codex:a", OpID: "first"}, 502)
			} else {
				operatorWant(t, p, human.Id, "POST", "dms", operatorDMRequest{OpID: "first", AgentID: "codex:a"}, 502)
			}
			f.directFault = true
			if agentFirst {
				operatorWant(t, p, human.Id, "POST", "dms", operatorDMRequest{OpID: "second", AgentID: "codex:a"}, 409)
			} else {
				agentRequest(t, p, "POST", "operator-dm", memberRequest{Actor: "codex:a", OpID: "second"}, 409)
			}
			if p.state.Operations["second"] != nil {
				t.Fatal("fresh intent overtook accepted pending pair")
			}
			var channel string
			if agentFirst {
				channel = openDM(t, p, human.Id, "second", "codex:a")
			} else {
				channel = openAgentOperatorDM(t, p, "codex:a", "second")["id"].(string)
			}
			if p.state.Operations["first"].Status != "complete" || len(f.channels) != 1 || p.state.Agents["codex:a"].Generation != 1 {
				t.Fatal("cross-kind recovery split pair or repeated grant")
			}
			f.directFault = true
			agentRequest(t, p, "POST", "operator-dm", memberRequest{Actor: "codex:a", OpID: "before-leave"}, 502)
			if _, err := p.membership(channel, "leave", memberRequest{Actor: "codex:a", OpID: "leave"}); err != nil {
				t.Fatal(err)
			}
			p = reloadDM(t, p, f)
			if p.state.Operations["before-leave"].Status != "complete" || !p.state.Conversations[channel].Left["codex:a"] {
				t.Fatal("pending opening outlived and reversed departure")
			}
			agentRequest(t, p, "POST", "operator-dm", memberRequest{Actor: "claude:b", OpID: "first"}, 409)
		})
	}
}

func TestAgentAndHumanOperatorDMConcurrentOpenConverges(t *testing.T) {
	p, f, human := dmFixture(t)
	p.config.OperatorUserID = human.Id
	var wg sync.WaitGroup
	results := make(chan *httptest.ResponseRecorder, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			path, payload := "/v1/operator-dm", any(memberRequest{Actor: "codex:a", OpID: "agent"})
			if i == 1 {
				path, payload = "/operator/v1/dms", operatorDMRequest{OpID: "human", AgentID: "codex:a"}
			}
			body, _ := json.Marshal(payload)
			r := httptest.NewRequest("POST", path, bytes.NewReader(body))
			r.Header.Set("Content-Type", "application/json")
			if i == 1 {
				r.Header.Set("Mattermost-User-Id", human.Id)
			} else {
				r.Header.Set("Authorization", "Bearer test-token")
			}
			w := httptest.NewRecorder()
			p.ServeHTTP(nil, w, r)
			results <- w
		}(i)
	}
	wg.Wait()
	close(results)
	for result := range results {
		if result.Code != 200 {
			t.Fatalf("concurrent opening failed: %d %s", result.Code, result.Body.String())
		}
	}
	if len(f.channels) != 1 || len(p.state.Conversations) != 1 || p.state.Agents["codex:a"].Generation != 1 || p.state.Operations["agent"].ChannelID != p.state.Operations["human"].ChannelID {
		t.Fatal("concurrent initiators did not converge")
	}
}

type operatorConfigFake struct {
	*fakeAPI
	config map[string]any
	err    error
}

func (f *operatorConfigFake) LoadPluginConfiguration(dst any) error {
	if f.err != nil {
		return f.err
	}
	data, err := json.Marshal(f.config)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, dst)
}

func TestOperatorConfigurationOmissionDisablesAndLoadErrorPreservesLastGood(t *testing.T) {
	p, base := fixture(t)
	f := &operatorConfigFake{fakeAPI: base, config: map[string]any{"coordinatortoken": "test-token", "operatoruserid": model.NewId()}}
	p.API = f
	if err := p.OnConfigurationChange(); err != nil {
		t.Fatal(err)
	}
	if p.config.OperatorUserID != f.config["operatoruserid"] {
		t.Fatal("configured stable human ID not loaded")
	}
	delete(f.config, "operatoruserid")
	if err := p.OnConfigurationChange(); err != nil {
		t.Fatal(err)
	}
	if p.config.OperatorUserID != "" || p.config.CoordinatorToken != "test-token" {
		t.Fatal("omitted setting retained old target or lost token")
	}
	f.err = errors.New("configuration unavailable")
	if err := p.OnConfigurationChange(); err == nil || p.config.CoordinatorToken != "test-token" {
		t.Fatal("failed decode replaced last good configuration")
	}
}
