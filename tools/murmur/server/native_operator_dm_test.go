package main

import (
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/mattermost/mattermost/server/public/model"
)

type nativeDMAPI struct {
	*dmFake
	lookupError    *model.AppError
	lookupResult   *model.Channel
	nilResult      bool
	lookupAfter    func()
	includeDeleted bool
	lookups        int
}

func (f *nativeDMAPI) GetChannelByName(team, name string, deleted bool) (*model.Channel, *model.AppError) {
	if team != "" {
		return f.fakeAPI.GetChannelByName(team, name, deleted)
	}
	f.lookups++
	f.includeDeleted = deleted
	if f.lookupAfter != nil {
		after := f.lookupAfter
		f.lookupAfter = nil
		after()
	}
	if f.lookupError != nil {
		return nil, f.lookupError
	}
	if f.nilResult {
		return nil, nil
	}
	if f.lookupResult != nil {
		return f.lookupResult, nil
	}
	c, err := f.fakeAPI.GetChannelByName(team, name, deleted)
	if c != nil && c.DeleteAt != 0 && !deleted {
		return nil, missing()
	}
	return c, err
}

func nativeDMFixture(t *testing.T, baseline bool) (*Plugin, *nativeDMAPI, *model.User) {
	t.Helper()
	p, base, human := dmFixture(t)
	f := &nativeDMAPI{dmFake: base}
	p.API = f
	p.config.OperatorUserID = human.Id
	if baseline {
		p.discoverNativeOperatorDMs()
		if len(p.operatorDMErrors) != 0 || f.directCalls != 0 || len(f.channels) != 0 {
			t.Fatal("baseline created a native DM or failed", p.operatorDMErrors)
		}
	}
	return p, f, human
}

func nativeHumanPost(t *testing.T, p *Plugin, f *nativeDMAPI, human *model.User, actor, message string) *model.Post {
	t.Helper()
	c, err := f.fakeAPI.GetDirectChannel(human.Id, p.state.Agents[actor].UserID)
	if err != nil {
		t.Fatal(err)
	}
	post, appErr := f.CreatePost(&model.Post{ChannelId: c.Id, UserId: human.Id, Message: message})
	if appErr != nil {
		t.Fatal(appErr)
	}
	return post
}

func nativeEligibility(t *testing.T, p *Plugin, actor string) map[string]bool {
	t.Helper()
	result := map[string]bool{}
	for _, item := range changesFor(t, p, actor)["changes"].([]any) {
		entry := item.(map[string]any)
		result[entry["post"].(map[string]any)["id"].(string)] = entry["notification_eligible"].(bool)
	}
	return result
}

func TestNativeOperatorDMFirstHintAndMissedCallbackStayFreshAcrossRestart(t *testing.T) {
	for _, mode := range []string{"hint", "missed-callback"} {
		t.Run(mode, func(t *testing.T) {
			p, f, human := nativeDMFixture(t, true)
			mustRegister(t, p, "codex:child", "codex:a")
			p.initializePostHints()
			post := nativeHumanPost(t, p, f, human, "codex:a", "first native request")
			post.CreateAt = 1 // Admin backdating is not evidence of old mail.
			if mode == "hint" {
				p.nextRepair = p.repairClock().Add(time.Hour)
				p.MessageHasBeenPosted(nil, post)
				p.MessageHasBeenPosted(nil, post)
				p.drainPostHints()
			} else {
				p = reloadDM(t, p, f.dmFake)
			}
			c := p.state.Conversations[post.ChannelId]
			if c == nil || c.Grants["codex:a"].Role != "member" || c.Grants["codex:child"].Role != "observer" ||
				len(f.members[c.ID]) != 2 || len(p.state.Agents) != 4 || f.directCalls != 0 {
				t.Fatal("discovery changed native membership/enrollment, or lost inherited reference access")
			}
			if p.state.Observed[post.Id].Historical || !nativeEligibility(t, p, "codex:a")[post.Id] || len(nativeEligibility(t, p, "codex:child")) != 0 {
				t.Fatal("first native request was quiet, or woke an observer")
			}
			seq, generation := p.state.Sequence, p.state.Agents["codex:a"].Generation
			p = reloadDM(t, p, f.dmFake)
			if p.state.Sequence != seq || p.state.Agents["codex:a"].Generation != generation || len(p.state.Observed) != 1 || len(f.channels) != 1 {
				t.Fatal("restart duplicated observation, grants, or native channel")
			}
		})
	}
}

func TestNativeOperatorDMExistingSnapshotQuietAndConcurrentArrivalFresh(t *testing.T) {
	p, f, human := nativeDMFixture(t, false)
	old := nativeHumanPost(t, p, f, human, "codex:a", "before feature enable")
	var fresh *model.Post
	p.history = &dmSnapshot{base: f.fakeAPI, after: func() {
		fresh = nativeHumanPost(t, p, f, human, "codex:a", "after canonical snapshot")
		fresh.CreateAt = old.CreateAt // Equal milliseconds cannot drop the arrival.
	}}
	p.refreshNativeOperatorDM("codex:a", "")
	if len(p.operatorDMErrors) != 0 || !p.state.Observed[old.Id].Historical {
		t.Fatal("existing native backlog was not adopted quietly", p.operatorDMErrors)
	}
	if _, exists := p.state.Observed[fresh.Id]; exists {
		t.Fatal("snapshot included a concurrent arrival committed afterward")
	}
	p.reconcile()
	mail := nativeEligibility(t, p, "codex:a")
	if len(mail) != 2 || mail[old.Id] || !mail[fresh.Id] || p.state.Observed[fresh.Id].Historical {
		t.Fatal("quiet snapshot consumed later arrivals", mail)
	}
}

func TestNativeOperatorDMEnrollmentEstablishesAbsenceAndManualInbox(t *testing.T) {
	p, f, human := nativeDMFixture(t, false)
	for _, mode := range []string{"native", "manual"} {
		actor := "codex:new-" + mode
		proof := []LineageEdge{}
		if mode == "native" {
			proof = append(proof, LineageEdge{Actor: actor})
		}
		agentRequest(t, p, "POST", "register", map[string]any{"actor": actor, "name": mode, "mode": mode,
			"lineage": proof, "enrollment_id": "enroll-" + mode}, 200)
		watch := nativeOperatorDMWatch(&Operation{AgentID: actor, HumanUserID: human.Id, BotUserID: p.state.Agents[actor].UserID})
		loaded, err := p.metadata.Load()
		if err != nil || loaded.Operations[watch.ID] == nil {
			t.Fatal("enrollment returned before its absence baseline was durable", err)
		}
		post := nativeHumanPost(t, p, f, human, actor, "after registration returned")
		p.reconcile()
		if !nativeEligibility(t, p, actor)[post.Id] || p.state.Agents[actor].Mode != mode {
			t.Fatal("native/manual first request missing, or delivery mode changed")
		}
	}
	if f.directCalls != 0 {
		t.Fatal("discovery used create-or-get as a probe")
	}
}

func TestNativeOperatorDMExplicitQuietAdoptionAndDepartureWin(t *testing.T) {
	p, f, human := nativeDMFixture(t, true)
	post := nativeHumanPost(t, p, f, human, "codex:a", "explicit open adopts this quietly")
	openAgentOperatorDM(t, p, "codex:a", "explicit-wins")
	p.discoverNativeOperatorDMs()
	if !p.state.Observed[post.Id].Historical || nativeEligibility(t, p, "codex:a")[post.Id] {
		t.Fatal("old absence reclassified explicit adoption")
	}
	if _, err := p.membership(post.ChannelId, "leave", memberRequest{Actor: "codex:a", OpID: "leave-dm"}); err != nil {
		t.Fatal(err)
	}
	p.reconcile()
	c := p.state.Conversations[post.ChannelId]
	if !c.Left["codex:a"] || c.Grants["codex:a"].Role != "" {
		t.Fatal("native discovery rejoined a departed actor")
	}
	delete(c.Left, "codex:a")
	c.Grants["codex:a"] = Grant{Role: "observer"}
	p.discoverNativeOperatorDMs()
	if c.Grants["codex:a"].Role != "observer" {
		t.Fatal("native discovery repaired an inconsistent managed grant")
	}
}

func TestNativeOperatorDMOnlyVerifiedAbsenceCreatesWatch(t *testing.T) {
	for _, failure := range []string{"lookup-error", "nil-channel", "deleted-channel", "wrong-pair", "wrong-members", "foreign-bot", "config-race", "absence-config-race"} {
		t.Run(failure, func(t *testing.T) {
			p, f, human := nativeDMFixture(t, false)
			post := nativeHumanPost(t, p, f, human, "codex:a", "must remain unmanaged")
			channel := f.channels[post.ChannelId]
			switch failure {
			case "lookup-error":
				f.lookupError = unavailable()
			case "nil-channel":
				f.nilResult = true
			case "deleted-channel":
				channel.DeleteAt = 1
			case "wrong-pair":
				channel.Name = model.NewId() + "__" + model.NewId()
				f.lookupResult = channel
			case "wrong-members":
				f.members[channel.Id][model.NewId()] = true
			case "foreign-bot":
				f.bots[p.state.Agents["codex:a"].UserID].OwnerId = "foreign-plugin"
			case "config-race":
				f.lookupAfter = func() { p.config.OperatorUserID = "" }
			case "absence-config-race":
				f.lookupError = missing()
				f.lookupAfter = func() { p.config.OperatorUserID = "" }
			}
			p.refreshNativeOperatorDM("codex:a", "")
			if p.operatorDMErrors["codex:a"] == "" || len(p.state.Operations) != 0 || len(p.state.Conversations) != 0 || len(p.state.Observed) != 0 || p.fault != "" {
				t.Fatal("unverified native state established a baseline or poisoned global state", p.operatorDMErrors)
			}
			if failure == "deleted-channel" {
				if !f.includeDeleted {
					t.Fatal("filtered lookup could mistake a deleted DM for absence")
				}
				channel.DeleteAt = 0
				p.refreshNativeOperatorDM("codex:a", "")
				if !p.state.Observed[post.Id].Historical {
					t.Fatal("restored DM backlog became fresh")
				}
			}
		})
	}
}

func TestNativeOperatorDMSnapshotRevalidatesPrincipalsAndNativePair(t *testing.T) {
	for _, change := range []string{"disabled", "mapping-drift", "native-departure"} {
		t.Run(change, func(t *testing.T) {
			p, f, human := nativeDMFixture(t, true)
			post := nativeHumanPost(t, p, f, human, "codex:a", "arrives before revocation")
			p.history = &dmSnapshot{base: f.fakeAPI, after: func() {
				switch change {
				case "disabled":
					p.config.OperatorUserID = ""
				case "mapping-drift":
					p.state.Agents["codex:a"].UserID = p.state.Agents["claude:b"].UserID
				case "native-departure":
					delete(f.members[post.ChannelId], human.Id)
				}
			}}
			p.refreshNativeOperatorDM("codex:a", "")
			if p.operatorDMErrors["codex:a"] == "" || p.state.Conversations[post.ChannelId] != nil || len(p.state.Observed) != 0 {
				t.Fatal("revocation during canonical snapshot published access")
			}
		})
	}
}

func TestNativeOperatorDMSamePrincipalReenableAndRenamePreserveAbsentBaseline(t *testing.T) {
	p, f, human := nativeDMFixture(t, true)
	watch := nativeOperatorDMWatch(&Operation{AgentID: "codex:a", HumanUserID: human.Id, BotUserID: p.state.Agents["codex:a"].UserID})
	before := digest(p.state.Operations[watch.ID])
	p.config.OperatorUserID = ""
	p.discoverNativeOperatorDMs()
	post := nativeHumanPost(t, p, f, human, "codex:a", "created while delivery discovery is disabled")
	p.config.OperatorUserID = human.Id
	delete(f.users, human.Username)
	human.Username = "renamed-human"
	f.users[human.Username] = human
	if _, err := p.register("codex:a", "Renamed native session", ""); err != nil {
		t.Fatal(err)
	}
	if digest(p.state.Operations[watch.ID]) != before || !nativeEligibility(t, p, "codex:a")[post.Id] {
		t.Fatal("temporary disable or display-name change moved the durable absence boundary")
	}
}

func TestNativeOperatorDMHintsNeverAdoptUnrelatedOrUnenrolledPairs(t *testing.T) {
	p, f, human := nativeDMFixture(t, true)
	p.initializePostHints()
	bot, appErr := f.CreateBot(&model.Bot{OwnerId: "murmur", Username: "not-enrolled"})
	if appErr != nil {
		t.Fatal(appErr)
	}
	other := &model.User{Id: model.NewId(), Username: "other-human", Roles: "system_admin"}
	f.users[other.Username] = other
	for _, pair := range [][2]string{{human.Id, bot.UserId}, {other.Id, p.state.Agents["codex:a"].UserID}} {
		channel, _ := f.fakeAPI.GetDirectChannel(pair[0], pair[1])
		post, _ := f.CreatePost(&model.Post{ChannelId: channel.Id, UserId: pair[0], Message: "unrelated native DM"})
		p.MessageHasBeenPosted(nil, post)
	}
	p.drainPostHints()
	p.reconcile()
	if len(p.state.Agents) != 3 || len(p.state.Conversations) != 0 || len(p.state.Observed) != 0 || len(p.state.Operations) != 3 || f.directCalls != 0 {
		t.Fatal("native callback enrolled a bot, adopted an unrelated pair, or created a DM")
	}
}

func TestNativeOperatorDMInvalidPolicyIsDiagnosticAndDoesNotBlockEnrollmentOrOtherFeeds(t *testing.T) {
	p, _, human := nativeDMFixture(t, false)
	human.Roles = "system_user"
	actor := "claude:held"
	result := agentRequest(t, p, "POST", "register", map[string]any{"actor": actor, "name": "Held discovery", "mode": "manual"}, 200)
	if result["operator_dm_discovery_error"] == nil || p.state.Agents[actor] == nil {
		t.Fatal("discovery hold either hid its diagnostic or prevented enrollment")
	}
	room := channelConversation(t, p, "independent-room", "claude:b")
	post := send(t, p, "claude:b", room, "independent-post", "ordinary mail still works", "")
	if !nativeEligibility(t, p, "codex:a")[post] || p.lastErr != "" {
		t.Fatal("one discovery hold blocked unrelated managed mail")
	}
	health := call(t, p, "health")
	if health["operator_dm_discovery_errors"].(map[string]any)[actor] == nil {
		t.Fatal("health hid an actor's discovery hold")
	}
}

func TestNativeOperatorDMUnestablishedValidBaselineWithholdsRegistrationSuccess(t *testing.T) {
	p, f, human := nativeDMFixture(t, false)
	f.lookupError = unavailable()
	actor := "claude:retry-baseline"
	req := map[string]any{"actor": actor, "name": "Retry baseline", "mode": "manual", "enrollment_id": "retry-baseline"}
	result := agentRequest(t, p, "POST", "register", req, 503)
	if p.state.Agents[actor] == nil || p.state.Enrollments["retry-baseline"].Actor != "" || result["error"] == nil {
		t.Fatal("baseline failure either lost the actor journal or returned enrollment success")
	}
	during := nativeHumanPost(t, p, f, human, actor, "sent while registration has not succeeded")
	f.lookupError = nil
	agentRequest(t, p, "POST", "register", req, 200)
	if !p.state.Observed[during.Id].Historical {
		t.Fatal("unestablished pre-success history became fresh")
	}
	after := nativeHumanPost(t, p, f, human, actor, "first message after successful registration")
	p.reconcile()
	if !nativeEligibility(t, p, actor)[after.Id] {
		t.Fatal("first message after registration success stayed quiet")
	}

	// A durable absence is already sufficient proof; transient lookup failure
	// on a later name synchronization cannot erase it or force a new boundary.
	actor = "codex:already-watched"
	mustRegister(t, p, actor, "")
	f.lookupError = unavailable()
	if _, err := p.register(actor, "Same identity, renamed", ""); err != nil {
		t.Fatal("established absence was downgraded by a transient refresh failure", err)
	}
}

func TestNativeOperatorDMPublicationFailureRecoversWithoutDuplicateOrRebase(t *testing.T) {
	p, f, human := nativeDMFixture(t, true)
	post := nativeHumanPost(t, p, f, human, "codex:a", "survives failed publication")
	f.beforeSave = func(state *State) *model.AppError {
		if state.Conversations[post.ChannelId] != nil {
			return unavailable()
		}
		return nil
	}
	p.refreshNativeOperatorDM("codex:a", "")
	if p.fault == "" {
		t.Fatal("failed publication was exposed as durable")
	}
	var disk State
	if err := json.Unmarshal(f.kv, &disk); err != nil || disk.Conversations[post.ChannelId] != nil || len(disk.Observed) != 0 {
		t.Fatal("failed save published a partial managed DM", err)
	}
	f.beforeSave = nil
	p = reloadDM(t, p, f.dmFake)
	if !nativeEligibility(t, p, "codex:a")[post.Id] || len(p.state.Observed) != 1 || len(f.channels) != 1 || f.directCalls != 0 {
		t.Fatal("restart lost the absent baseline or duplicated native effects")
	}
}

func TestNativeOperatorDMWatchCannotBeReusedForAnotherScope(t *testing.T) {
	for _, change := range []string{"kind", "hash", "actor", "human", "bot", "status", "stage", "channel"} {
		t.Run(change, func(t *testing.T) {
			p, f, human := nativeDMFixture(t, true)
			a := p.state.Agents["codex:a"]
			key := nativeOperatorDMWatch(&Operation{AgentID: a.ID, HumanUserID: human.Id, BotUserID: a.UserID}).ID
			watch := p.state.Operations[key]
			switch change {
			case "kind":
				watch.Kind = "post"
			case "hash":
				watch.Hash = "different"
			case "actor":
				watch.Actor = "claude:b"
			case "human":
				watch.HumanUserID = model.NewId()
			case "bot":
				watch.BotUserID = model.NewId()
			case "status":
				watch.Status = "pending"
			case "stage":
				watch.Stage = "present"
			case "channel":
				watch.ChannelID = model.NewId()
			}
			post := nativeHumanPost(t, p, f, human, a.ID, fmt.Sprint("scope ", change))
			p.refreshNativeOperatorDM(a.ID, "")
			if p.operatorDMErrors[a.ID] == "" || p.state.Conversations[post.ChannelId] != nil || len(p.state.Observed) != 0 {
				t.Fatal("unscoped watch authorized fresh adoption")
			}
		})
	}
}
