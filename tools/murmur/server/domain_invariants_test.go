package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/mattermost/mattermost/server/public/model"
)

func agentRequest(t *testing.T, p *Plugin, method, path string, payload any, want int) map[string]any {
	t.Helper()
	data, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(method, "/v1/"+path, bytes.NewReader(data))
	request.Header.Set("Authorization", "Bearer test-token")
	response := httptest.NewRecorder()
	p.ServeHTTP(nil, response, request)
	if response.Code != want {
		t.Fatalf("%s %s: want %d got %d %s", method, path, want, response.Code, response.Body.String())
	}
	result := map[string]any{}
	if err := json.Unmarshal(response.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	return result
}

func TestVerifiedLineageCrossesUnenrolledNodesAndPublishesLateGrants(t *testing.T) {
	p, f := fixture(t)
	channel := legacyPrivateChannel(t, p, "lineage-private", "claude:b")
	mustRegister(t, p, "codex:child", "codex:middle")
	mustRegister(t, p, "codex:grandchild", "codex:child")
	if p.state.Agents["codex:child"].LineageComplete {
		t.Fatal("missing ancestor was treated as a root")
	}
	if _, err := p.access("codex:child", channel, false); err == nil {
		t.Fatal("unverified ancestry granted access")
	}
	graph := []LineageEdge{{Actor: "codex:middle", Parent: "codex:a"}}
	old := append([]byte{}, f.kv...)
	f.kvFault = true
	if _, err := p.registerLineage("codex:child", "Child", "codex:middle", graph); err == nil {
		t.Fatal("expected atomic publication failure")
	}
	if !bytes.Equal(old, f.kv) {
		t.Fatal("failed lineage publication changed durable grants")
	}
	f.kvFault = false
	p = reload(t, p, f)
	if _, known := p.state.Lineage["codex:middle"]; known {
		t.Fatal("edge persisted without its grants")
	}
	if _, err := p.registerLineage("codex:child", "Child", "codex:middle", graph); err != nil {
		t.Fatal(err)
	}
	if p.state.Agents["codex:middle"] != nil || len(f.bots) != 5 {
		t.Fatal("lineage-only ancestor was enrolled/provisioned")
	}
	for _, actor := range []string{"codex:child", "codex:grandchild"} {
		if !p.state.Agents[actor].LineageComplete || p.state.Conversations[channel].Grants[actor].Role != "observer" {
			t.Fatal("late edge did not publish descendant coverage/access", actor)
		}
	}
	later := legacyPrivateChannel(t, p, "lineage-later", "claude:b")
	if p.state.Conversations[later].Grants["codex:grandchild"].Role != "observer" {
		t.Fatal("future ancestor grant did not cross unenrolled node")
	}
	if _, err := p.membership(channel, "leave", memberRequest{Actor: "codex:a", OpID: "lineage-parent-leave"}); err != nil {
		t.Fatal(err)
	}
	p = reload(t, p, f)
	if _, err := p.access("codex:grandchild", channel, false); err != nil {
		t.Fatal("ancestor leave revoked retained descendant grant", err)
	}
	if ids := changedIDs(changesFor(t, p, "codex:child")); len(ids) != 0 {
		t.Fatal("inherited reference created attention", ids)
	}
}

func TestLineageRejectsCyclesReparentingUnrelatedAndImplicitRootsBeforeEffects(t *testing.T) {
	p, f := fixture(t)
	before, bots := append([]byte{}, f.kv...), len(f.bots)
	cases := []struct {
		edges  []LineageEdge
		status int
	}{
		{[]LineageEdge{{Actor: "codex:middle", Parent: "codex:new"}}, 400},
		{[]LineageEdge{{Actor: "codex:a", Parent: "codex:x"}}, 409},
		{[]LineageEdge{{Actor: "codex:unrelated", Parent: ""}}, 400},
	}
	for _, item := range cases {
		_, err := p.registerLineage("codex:new", "New", "codex:middle", item.edges)
		status(t, err, item.status)
		if !bytes.Equal(before, f.kv) || len(f.bots) != bots {
			t.Fatal("invalid lineage reached a durable/native effect")
		}
	}
	agentRequest(t, p, "POST", "register", map[string]any{"actor": "codex:new", "name": "New", "parent": "codex:middle", "lineage": []any{map[string]any{"actor": "codex:middle"}}}, 400)
	result := agentRequest(t, p, "POST", "register", map[string]any{"actor": "codex:new", "name": "New", "mode": "native", "parent": "codex:middle", "lineage": []any{map[string]any{"actor": "codex:new", "parent": "codex:middle"}, map[string]any{"actor": "codex:middle", "parent": nil}}}, 200)
	if result["agent"].(map[string]any)["lineage_complete"] != true || p.state.Agents["codex:middle"] != nil {
		t.Fatal("explicit verified root changed enrollment or coverage")
	}
}

func TestEnrollmentBoundarySurvivesResponseLossRestartAndOldEdits(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "enrollment-room", "claude:b")
	old, _ := f.CreatePost(&model.Post{ChannelId: channel, UserId: p.state.Agents["claude:b"].UserID, Message: "quiet initial history"})
	req := map[string]any{"actor": "codex:a", "name": "codex:a", "enrollment_id": "host-one-enrollment", "mode": "native", "lineage": []LineageEdge{{Actor: "codex:a", Parent: ""}}}
	first := agentRequest(t, p, "POST", "register", req, 200)
	cutoff := first["enrollment_seq"].(float64)
	if cutoff != float64(p.state.Observed[old.Id].FirstSeenSeq) {
		t.Fatal("registration did not capture canonical history")
	}
	fresh, _ := f.CreatePost(&model.Post{ChannelId: channel, UserId: p.state.Agents["claude:b"].UserID, Message: "fresh at equal timestamp"})
	fresh.CreateAt = old.CreateAt
	old.Message = "old edited after enrollment"
	old.UpdateAt++
	p = reload(t, p, f)
	retry := agentRequest(t, p, "POST", "register", req, 200)
	if retry["enrollment_seq"] != cutoff {
		t.Fatal("lost-response retry moved canonical enrollment boundary")
	}
	page := changesFor(t, p, "codex:a")
	origins := map[string]float64{}
	for _, raw := range page["changes"].([]any) {
		item := raw.(map[string]any)
		origins[item["post"].(map[string]any)["id"].(string)] = item["first_seen_seq"].(float64)
	}
	if origins[old.Id] > cutoff || origins[fresh.Id] <= cutoff {
		t.Fatal("edit sequence or wall clock replaced first observation", origins, cutoff)
	}
	req["enrollment_id"] = "host-two-fresh-ledger"
	second := agentRequest(t, p, "POST", "register", req, 200)
	if second["enrollment_seq"].(float64) < origins[fresh.Id] {
		t.Fatal("new local state did not get its own quiet baseline")
	}
	req["actor"] = "claude:b"
	req["lineage"] = []LineageEdge{{Actor: "claude:b", Parent: ""}}
	agentRequest(t, p, "POST", "register", req, 409)
	if p.state.Enrollments["host-one-enrollment"].Actor != "codex:a" {
		t.Fatal("enrollment ownership changed")
	}
}

func TestAgentRecoveryRequiresCanonicalAuthorRootBodyAndCausal(t *testing.T) {
	for _, mismatch := range []string{"author", "message", "root", "causal"} {
		t.Run(mismatch, func(t *testing.T) {
			p, f := fixture(t)
			channel := channelConversation(t, p, "correlation-"+mismatch, "claude:b")
			root := send(t, p, "codex:a", channel, "root", "thread", "")
			request := postRequest{Actor: "codex:a", OpID: "uncertain", ChannelID: channel, RootID: root, Message: "exact", Causal: map[string]any{"chain_ids": []string{"chain"}}}
			f.postFault = "before"
			if _, err := p.send(request); err == nil {
				t.Fatal("expected uncertain creation")
			}
			f.postFault = ""
			o := p.state.Operations[request.OpID]
			post := &model.Post{ChannelId: channel, UserId: p.state.Agents[request.Actor].UserID, RootId: root, Message: request.Message, Props: model.StringInterface{"murmur_op": o.ID, "murmur_actor": o.Actor, "murmur_hash": o.Hash, "murmur_causal": request.Causal}}
			switch mismatch {
			case "author":
				post.UserId = p.state.Agents["claude:b"].UserID
			case "message":
				post.Message = "changed"
			case "root":
				post.RootId = ""
			case "causal":
				post.Props["murmur_causal"] = map[string]any{"chain_ids": []string{"different"}}
			}
			f.CreatePost(post)
			if err := p.recoverPost(o); err != nil {
				t.Fatal(err)
			}
			if o.Status != "uncertain" || o.PostID != "" {
				t.Fatal("copied props proved a different canonical request", mismatch)
			}
		})
	}
}

func TestDuplicateCanonicalEchoHoldsOnlyItsOperation(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "duplicates", "claude:b")
	req := postRequest{Actor: "codex:a", OpID: "lost-duplicate", ChannelID: channel, Message: "exact"}
	f.postFault = "before"
	p.send(req)
	f.postFault = ""
	o := p.state.Operations[req.OpID]
	for i := 0; i < 2; i++ {
		f.CreatePost(&model.Post{ChannelId: channel, UserId: p.state.Agents[req.Actor].UserID, Message: req.Message, Props: model.StringInterface{"murmur_op": o.ID, "murmur_actor": o.Actor, "murmur_hash": o.Hash}})
	}
	p.reconcile()
	if o.Status != "uncertain" || !strings.Contains(o.Error, "duplicate") || p.lastErr != "" {
		t.Fatal("duplicate echo escaped operation-local uncertainty", o, p.lastErr)
	}
	changesFor(t, p, "claude:b")
	send(t, p, "claude:b", channel, "independent-send", "unrelated work", "")
}

type deniedMembershipAPI struct {
	*fakeAPI
	user  string
	calls int
}

func (f *deniedMembershipAPI) AddChannelMember(channel, user string) (*model.ChannelMember, *model.AppError) {
	if user == f.user {
		f.calls++
		return nil, &model.AppError{StatusCode: 403, Message: "native policy denial"}
	}
	return f.fakeAPI.AddChannelMember(channel, user)
}
func TestDefinitiveMembershipDenialIsTerminalAndDoesNotBlockFeeds(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "denied-membership", "claude:b")
	native := &deniedMembershipAPI{fakeAPI: f, user: p.state.Agents["codex:x"].UserID}
	p.API = native
	_, err := p.membership(channel, "join", memberRequest{Actor: "codex:x", OpID: "denied-join"})
	if err == nil || p.state.Operations["denied-join"].Status != "failed" {
		t.Fatal("definitive rejection was not terminal")
	}
	p.reconcile()
	changesFor(t, p, "claude:b")
	if native.calls != 1 || p.lastErr != "" || p.fault != "" {
		t.Fatal("terminal rejected journal retried or blocked unrelated feed")
	}
}

type channelFaultHistory struct {
	base    canonicalHistory
	channel string
}

func (h channelFaultHistory) Posts(channel string) ([]*model.Post, error) {
	if channel == h.channel {
		return nil, fmt.Errorf("one channel unavailable")
	}
	return h.base.Posts(channel)
}
func TestCanonicalRepairFailureAffectsOnlyMemberChannelsAndPersistenceStillFailsClosed(t *testing.T) {
	p, f := fixture(t)
	badChannel := channelConversation(t, p, "bad-canonical", "claude:b")
	value, err := p.conversation(conversationRequest{Actor: "codex:x", OpID: "healthy-own", Kind: "channel", Name: "Healthy", Members: nil})
	if err != nil {
		t.Fatal(err)
	}
	healthy := value["conversation"].(map[string]any)["id"].(string)
	post, _ := f.CreatePost(&model.Post{ChannelId: healthy, UserId: p.state.Agents["codex:a"].UserID, Message: "healthy work"})
	p.history = channelFaultHistory{base: f, channel: badChannel}
	page := changesFor(t, p, "codex:x")
	if !reflect.DeepEqual(changedIDs(page), []string{post.Id}) {
		t.Fatal("unrelated canonical channel blocked work", page)
	}
	navigationStatus(t, p, "changes?attention_version=3&actor=claude:b", 503)
	f.CreatePost(&model.Post{ChannelId: healthy, UserId: p.state.Agents["codex:a"].UserID, Message: "requires durable observation"})
	f.kvFault = true
	p.reconcile()
	navigationStatus(t, p, "changes?attention_version=3&actor=codex:x", 503)
	if p.fault == "" {
		t.Fatal("persistence ambiguity did not fail closed")
	}
}

func TestSystemRootsAreRejectedBeforeNativeSend(t *testing.T) {
	p, f, user := operatorFixture(t)
	channel := channelConversation(t, p, "system-roots", "claude:b")
	system, _ := f.CreatePost(&model.Post{ChannelId: channel, UserId: user.Id, Type: "system_join_channel", Message: "join"})
	creates := f.creates
	_, err := p.send(postRequest{Actor: "codex:a", OpID: "bad-system-reply", ChannelID: channel, RootID: system.Id, Message: "no"})
	status(t, err, 400)
	operatorWant(t, p, user.Id, "POST", "posts", operatorPostRequest{OpID: "operator-system", ChannelID: channel, RootID: system.Id, Message: "no"}, 404)
	if f.creates != creates {
		t.Fatal("system root reached native post creation")
	}
}

func TestOperatorRejectedPostHasDurableImmutableNoDispatchProof(t *testing.T) {
	p, f, user := operatorFixture(t)
	channel := channelConversation(t, p, "rejected-composer", "claude:b")
	root := send(t, p, "codex:a", channel, "original-root", "root", "")
	f.posts[root].DeleteAt = 1
	req := operatorPostRequest{OpID: "rejected-reply", ChannelID: channel, RootID: root, Message: "retained draft"}
	creates := f.creates
	first := operatorWant(t, p, user.Id, "POST", "posts", req, 404)
	if first["status"] != "failed" || first["dispatch"] != "rejected" || first["op_id"] != req.OpID {
		t.Fatal("missing exact terminal no-dispatch proof", first)
	}
	p = reload(t, p, f.fakeAPI)
	p.API = f
	result := operatorWant(t, p, user.Id, "GET", "operations/"+req.OpID, nil, 200)
	if result["dispatch"] != "rejected" || result["op_id"] != req.OpID {
		t.Fatal("rejection proof did not survive restart")
	}
	f.posts[root].DeleteAt = 0
	operatorWant(t, p, user.Id, "POST", "posts", req, 409)
	changed := req
	changed.Message = "new payload"
	conflict := operatorWant(t, p, user.Id, "POST", "posts", changed, 409)
	if conflict["dispatch"] != nil || f.creates != creates {
		t.Fatal("rejected immutable op was reused or conflict claimed no effect")
	}
	missing := operatorWant(t, p, user.Id, "GET", "operations/not-known", nil, 404)
	if missing["dispatch"] != nil {
		t.Fatal("missing operation was confused with definite rejection")
	}
}

func TestOperatorThreadPagingHasExplicitRootAndValidatesCursor(t *testing.T) {
	p, f, user := operatorFixture(t)
	channel := channelConversation(t, p, "operator-thread-pages", "claude:b")
	root := send(t, p, "codex:a", channel, "paged-root", "root", "")
	for i := 0; i < 7; i++ {
		send(t, p, "claude:b", channel, fmt.Sprintf("paged-%d", i), "reply", root)
	}
	before := ""
	seen := map[string]bool{}
	for {
		path := "threads/" + root + "?limit=2"
		if before != "" {
			path += "&before=" + before
		}
		page := operatorWant(t, p, user.Id, "GET", path, nil, 200)
		if page["root"].(map[string]any)["id"] != root || len(page["posts"].([]any)) > 2 {
			t.Fatal("operator thread exceeded bounded explicit-root page")
		}
		for _, raw := range page["posts"].([]any) {
			id := raw.(map[string]any)["id"].(string)
			if seen[id] {
				t.Fatal("duplicate thread page item")
			}
			seen[id] = true
		}
		if page["next_before"] == nil {
			break
		}
		before = page["next_before"].(string)
	}
	if len(seen) != 8 {
		t.Fatal("operator thread paging lost canonical items", seen)
	}
	foreign, _ := f.CreatePost(&model.Post{ChannelId: channel, UserId: user.Id, Message: "another thread"})
	operatorWant(t, p, user.Id, "GET", "threads/"+root+"?before="+foreign.Id, nil, 400)
}

func TestManualEnrollmentDoesNotPublishRootsOrInheritLaterVerifiedAncestry(t *testing.T) {
	p, f := fixture(t)
	private := legacyPrivateChannel(t, p, "manual-ancestor-private", "claude:b")
	request := map[string]any{"actor": "codex:manual", "name": "Independent sidechat", "mode": "manual", "enrollment_id": "manual-local"}
	response := agentRequest(t, p, "POST", "register", request, 200)
	if response["agent"].(map[string]any)["mode"] != "manual" {
		t.Fatal("manual policy was not returned")
	}
	if _, known := p.state.Lineage["codex:manual"]; known || p.state.Agents["codex:manual"].LineageComplete {
		t.Fatal("unknown manual ancestry was published as a verified root")
	}
	// A native child can later prove the manual session's real ancestor without
	// turning that independently enrolled parent into an inherited reader.
	if _, err := p.registerLineage("codex:native-child", "Child", "codex:manual", []LineageEdge{{Actor: "codex:manual", Parent: "codex:a"}}); err != nil {
		t.Fatal(err)
	}
	if _, err := p.access("codex:manual", private, false); err == nil {
		t.Fatal("manual sidechat acquired ancestor access")
	}
	if p.state.Conversations[private].Grants["codex:native-child"].Role != "observer" {
		t.Fatal("native child did not traverse the verified manual node")
	}
	later := legacyPrivateChannel(t, p, "manual-later-private", "claude:b")
	if _, ok := p.state.Conversations[later].Grants["codex:manual"]; ok {
		t.Fatal("later native ancestor grant enrolled manual reader")
	}
	p = reload(t, p, f)
	agentRequest(t, p, "POST", "register", request, 200)
	if _, err := p.access("codex:manual", private, false); err == nil {
		t.Fatal("manual retry/restart changed inheritance policy")
	}
	request["mode"] = "native"
	request["parent"] = "codex:a"
	request["lineage"] = []LineageEdge{{Actor: "codex:manual", Parent: "codex:a"}}
	agentRequest(t, p, "POST", "register", request, 409)
}

func TestWireRegistrationRequiresExplicitModeAndVerifiedCurrentEdge(t *testing.T) {
	p, f := fixture(t)
	before, bots := append([]byte{}, f.kv...), len(f.bots)
	for _, request := range []map[string]any{
		{"actor": "codex:wire-new", "name": "New"},
		{"actor": "codex:wire-new", "name": "New", "mode": "native"},
		{"actor": "codex:wire-new", "name": "New", "mode": "manual", "parent": "codex:a"},
		{"actor": "codex:wire-new", "name": "New", "mode": "manual", "lineage": []LineageEdge{{Actor: "codex:wire-new", Parent: ""}}},
	} {
		agentRequest(t, p, "POST", "register", request, 400)
	}
	if !bytes.Equal(before, f.kv) || len(f.bots) != bots {
		t.Fatal("ambiguous registration reached provisioning")
	}
	legacy := State{}
	if err := json.Unmarshal(f.kv, &legacy); err != nil {
		t.Fatal(err)
	}
	legacy.Lineage = nil
	for _, agent := range legacy.Agents {
		agent.Mode = ""
	}
	legacy.init()
	if _, known := legacy.Lineage["codex:a"]; known || legacy.Agents["codex:a"].LineageComplete {
		t.Fatal("legacy empty parent was guessed to be an explicit root")
	}
	if len(legacy.Conversations) != len(p.state.Conversations) {
		t.Fatal("legacy policy uncertainty deleted retained state")
	}
}

func TestObservationIgnoresOlderAndEqualReplays(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "observation-order", "claude:b")
	post, _ := f.CreatePost(&model.Post{ChannelId: channel, UserId: p.state.Agents["codex:a"].UserID, Message: "canonical"})
	if !p.observe([]*model.Post{post}) {
		t.Fatal("first canonical observation was ignored")
	}
	old := p.state.Observed[post.Id]
	old.Historical = true
	p.state.Observed[post.Id] = old
	replay := post.Clone()
	replay.UpdateAt = post.UpdateAt - 1
	if p.observe([]*model.Post{replay, post}) || p.state.Observed[post.Id] != old {
		t.Fatal("older/equal replay moved canonical metadata or historical boundary")
	}
	next := post.Clone()
	next.UpdateAt++
	if !p.observe([]*model.Post{next}) {
		t.Fatal("new canonical edit was ignored")
	}
	observed := p.state.Observed[post.Id]
	if observed.Seq <= old.Seq || observed.FirstSeenSeq != old.FirstSeenSeq || !observed.Historical || observed.UpdateAt != next.UpdateAt {
		t.Fatal("new edit lost immutable observation fields", observed)
	}
}
