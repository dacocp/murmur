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

type operatorFake struct {
	*fakeAPI
	permission   bool
	beforeCreate func(*model.Post)
	afterCreate  func()
}

func (f *operatorFake) HasPermissionTo(_ string, permission *model.Permission) bool {
	return f.permission && permission.Id == model.PermissionManageSystem.Id
}

func (f *operatorFake) CreatePost(post *model.Post) (*model.Post, *model.AppError) {
	if f.beforeCreate != nil {
		f.beforeCreate(post)
	}
	result, err := f.fakeAPI.CreatePost(post)
	if f.afterCreate != nil {
		f.afterCreate()
	}
	return result, err
}

func operatorFixture(t *testing.T) (*Plugin, *operatorFake, *model.User) {
	t.Helper()
	p, base := fixture(t)
	f := &operatorFake{fakeAPI: base, permission: true}
	p.API = f
	user := &model.User{Id: model.NewId(), Username: "david", Roles: "system_user system_admin"}
	f.users[user.Username] = user
	return p, f, user
}

func operatorCall(t *testing.T, p *Plugin, user, method, path string, body any) (int, map[string]any) {
	t.Helper()
	var data []byte
	if body != nil {
		var err error
		data, err = json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
	}
	r := httptest.NewRequest(method, "/plugins/murmur/operator/v1/"+path, bytes.NewReader(data))
	if body != nil {
		r.Header.Set("Content-Type", "application/json")
	}
	if user != "" {
		r.Header.Set("Mattermost-User-Id", user)
	}
	w := httptest.NewRecorder()
	p.ServeHTTP(nil, w, r)
	result := map[string]any{}
	if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil {
		t.Fatal(err, w.Body.String())
	}
	return w.Code, result
}

func operatorWant(t *testing.T, p *Plugin, user, method, path string, body any, want int) map[string]any {
	t.Helper()
	code, result := operatorCall(t, p, user, method, path, body)
	if code != want {
		t.Fatalf("%s %s: want %d, got %d: %#v", method, path, want, code, result)
	}
	return result
}

func TestOperatorAuthenticationDoesNotUnlockAgentRoutes(t *testing.T) {
	p, f, user := operatorFixture(t)
	operatorWant(t, p, "", "GET", "bootstrap", nil, 401)
	operatorWant(t, p, "unknown", "GET", "bootstrap", nil, 403)
	user.Roles = "system_user"
	operatorWant(t, p, user.Id, "GET", "bootstrap", nil, 403)
	user.Roles = "system_user system_admin"
	f.permission = false
	operatorWant(t, p, user.Id, "GET", "bootstrap", nil, 403)
	f.permission = true
	user.IsBot = true
	operatorWant(t, p, user.Id, "GET", "bootstrap", nil, 403)
	user.IsBot = false
	user.DeleteAt = 1
	operatorWant(t, p, user.Id, "GET", "bootstrap", nil, 403)
	user.DeleteAt = 0
	operatorWant(t, p, user.Id, "GET", "bootstrap", nil, 200)
	for _, path := range []string{"/v1/agents", "/v1/register", "/plugins/murmur/v1/posts"} {
		r := httptest.NewRequest("POST", path, strings.NewReader(`{"actor":"codex:a"}`))
		r.Header.Set("Mattermost-User-Id", user.Id)
		w := httptest.NewRecorder()
		p.ServeHTTP(nil, w, r)
		if w.Code != 401 {
			t.Fatalf("human auth unlocked %s: %d", path, w.Code)
		}
	}
	r := httptest.NewRequest("GET", "/operator/v1/bootstrap", nil)
	r.Header.Set("Authorization", "Bearer test-token")
	w := httptest.NewRecorder()
	p.ServeHTTP(nil, w, r)
	if w.Code != 401 {
		t.Fatal("coordinator token authenticated human operation")
	}
	r = httptest.NewRequest("POST", "/operator/v1/posts", strings.NewReader(`{"message":"cross-site form"}`))
	r.Header.Set("Mattermost-User-Id", user.Id)
	r.Header.Set("Content-Type", "text/plain")
	w = httptest.NewRecorder()
	p.ServeHTTP(nil, w, r)
	if w.Code != 415 {
		t.Fatal("non-JSON operator write accepted")
	}
	for expected, want := range map[string]int{user.Id: 200, "previous-browser-user": 409} {
		r = httptest.NewRequest("GET", "/operator/v1/bootstrap", nil)
		r.Header.Set("Mattermost-User-Id", user.Id)
		r.Header.Set("X-Murmur-User-Id", expected)
		w = httptest.NewRecorder()
		p.ServeHTTP(nil, w, r)
		if w.Code != want {
			t.Fatalf("account binding %q: want %d, got %d", expected, want, w.Code)
		}
	}
}

func TestOperatorQuietReadsUseCurrentNamesAndScopedStableHistory(t *testing.T) {
	p, f, user := operatorFixture(t)
	channel := channelConversation(t, p, "operator-room", "claude:b")
	mustRegister(t, p, "codex:child", "codex:a")
	ids := []string{}
	for i := 0; i < 7; i++ {
		id := send(t, p, "codex:a", channel, fmt.Sprint("post-", i), fmt.Sprint("post ", i), "")
		// Equal timestamps must page by canonical ID, not skip a millisecond.
		f.posts[id].CreateAt = 100
		ids = append(ids, id)
	}
	send(t, p, "claude:b", channel, "thread-reply", "thread reply", ids[0])
	f.CreatePost(&model.Post{ChannelId: channel, UserId: user.Id, Message: "joined", Type: "system_join_channel"})
	p.register("codex:a", "Renamed A", "")
	before, _ := json.Marshal(p.state)
	beforeMembers, _ := json.Marshal(f.members)
	beforePreferences, _ := json.Marshal(f.preferences)
	boot := operatorWant(t, p, user.Id, "GET", "bootstrap", nil, 200)
	entry := boot["conversations"].([]any)[0].(map[string]any)
	if entry["kind"] != "channel" || len(entry["members"].([]any)) != 2 || !reflect.DeepEqual(entry["observers"], []any{"codex:child"}) || entry["preview"] != "thread reply" {
		t.Fatalf("incorrect membership/latest normal post: %#v", entry)
	}
	seen := map[string]bool{}
	path := "conversations/" + channel + "/posts?limit=3"
	for {
		page := operatorWant(t, p, user.Id, "GET", path, nil, 200)
		for _, value := range page["posts"].([]any) {
			post := value.(map[string]any)
			id := post["id"].(string)
			if seen[id] {
				t.Fatal("paging duplicated post", id)
			}
			seen[id] = true
			if post["user_id"] == p.state.Agents["codex:a"].UserID && post["author"] != "Renamed A" {
				t.Fatal("history retained stale agent name")
			}
		}
		if page["next_before"] == nil {
			break
		}
		path = "conversations/" + channel + "/posts?limit=3&before=" + page["next_before"].(string)
	}
	if len(seen) != 8 {
		t.Fatalf("paging lost posts: %d", len(seen))
	}
	thread := operatorWant(t, p, user.Id, "GET", "threads/"+ids[0], nil, 200)
	if len(thread["posts"].([]any)) != 2 {
		t.Fatal("thread failed")
	}
	after, _ := json.Marshal(p.state)
	afterMembers, _ := json.Marshal(f.members)
	afterPreferences, _ := json.Marshal(f.preferences)
	if !bytes.Equal(before, after) || !bytes.Equal(beforeMembers, afterMembers) || !bytes.Equal(beforePreferences, afterPreferences) {
		t.Fatal("observation changed plugin state, memberships or preferences")
	}
	if f.members[channel][user.Id] {
		t.Fatal("observer was enrolled in channel")
	}
}

func TestOperatorRejectsArbitraryChannelsRootsAndAuthorFields(t *testing.T) {
	p, f, user := operatorFixture(t)
	a := channelConversation(t, p, "a", "claude:b")
	b := channelConversation(t, p, "b", "claude:b")
	root := send(t, p, "codex:a", b, "root", "other managed room", "")
	reply := send(t, p, "codex:a", b, "reply", "not a root", root)
	foreign, _ := f.CreatePost(&model.Post{ChannelId: "not-managed", UserId: user.Id, Message: "unmanaged secret"})
	operatorWant(t, p, user.Id, "GET", "conversations/not-managed/posts", nil, 404)
	operatorWant(t, p, user.Id, "GET", "threads/"+foreign.Id, nil, 404)
	operatorWant(t, p, user.Id, "GET", "threads/"+reply, nil, 400)
	operatorWant(t, p, user.Id, "GET", "conversations/"+a+"/posts?before="+root, nil, 400)
	operatorWant(t, p, user.Id, "GET", "conversations/"+a+"/posts?before="+foreign.Id, nil, 404)
	operatorWant(t, p, user.Id, "GET", "conversations/"+a+"/posts?limit=0", nil, 400)
	operatorWant(t, p, user.Id, "POST", "posts", operatorPostRequest{OpID: "cross", ChannelID: a, RootID: root, Message: "no"}, 400)
	operatorWant(t, p, user.Id, "POST", "posts", operatorPostRequest{OpID: "nested", ChannelID: b, RootID: reply, Message: "no"}, 400)
	operatorWant(t, p, user.Id, "POST", "posts", operatorPostRequest{OpID: "foreign", ChannelID: "not-managed", Message: "no"}, 404)
	for _, field := range []string{"actor", "user_id", "causal"} {
		operatorWant(t, p, user.Id, "POST", "posts", map[string]any{"op_id": "spoof", "channel_id": a, "message": "no", field: "codex:a"}, 400)
	}
	operatorWant(t, p, user.Id, "GET", "operations/a", nil, 403)
	if len(p.state.Operations) != 7 {
		t.Fatal("pre-effect rejections did not retain their immutable identities")
	}
	for _, op := range []string{"cross", "nested", "foreign"} {
		if o := p.state.Operations[op]; o.Status != "failed" || o.Stage != "rejected" {
			t.Fatal("rejected request remained dispatchable", op)
		}
	}
}

func TestOperatorHumanDMPostPersistsBeforeNativeEffectAndRecoversLostResponse(t *testing.T) {
	p, f, user := operatorFixture(t)
	room, err := p.conversation(conversationRequest{Actor: "codex:a", OpID: "dm", Kind: "dm", Members: []string{"claude:b"}})
	if err != nil {
		t.Fatal(err)
	}
	channel := room["conversation"].(map[string]any)["id"].(string)
	root := send(t, p, "codex:a", channel, "root", "shared assumption", "")
	memberBefore, _ := json.Marshal(f.members)
	agentsBefore, _ := json.Marshal(p.state.Agents)
	f.beforeCreate = func(post *model.Post) {
		var saved State
		if err := json.Unmarshal(f.kv, &saved); err != nil {
			t.Fatal(err)
		}
		op := saved.Operations["human-correction"]
		if op == nil || op.Status != "pending" || op.Actor != user.Id || post.UserId != user.Id {
			t.Fatal("human post not durably bound to real author before native call")
		}
		if bytes.Contains(f.kv, []byte("correct assumption")) {
			t.Fatal("metadata journal mirrored message content")
		}
	}
	f.postFault = "after"
	req := operatorPostRequest{OpID: "human-correction", ChannelID: channel, RootID: root, Message: "correct assumption"}
	first := operatorWant(t, p, user.Id, "POST", "posts", req, 200)["post"].(map[string]any)
	creates := f.creates
	f.beforeCreate = nil
	p = reload(t, p, f.fakeAPI)
	p.API = f
	second := operatorWant(t, p, user.Id, "POST", "posts", req, 200)["post"].(map[string]any)
	if first["id"] != second["id"] || first["user_id"] != user.Id || first["root_id"] != root || f.creates != creates {
		t.Fatal("lost response reposted or changed author/thread")
	}
	memberAfter, _ := json.Marshal(f.members)
	agentsAfter, _ := json.Marshal(p.state.Agents)
	if !bytes.Equal(memberBefore, memberAfter) || !bytes.Equal(agentsBefore, agentsAfter) || len(f.members[channel]) != 2 {
		t.Fatal("human intervention enrolled an actor or changed DM membership")
	}
	op := operatorWant(t, p, user.Id, "GET", "operations/human-correction", nil, 200)
	if op["status"] != "complete" || op["result"].(map[string]any)["post_id"] != first["id"] {
		t.Fatal("operation recovery failed")
	}
	req.Message = "different payload"
	operatorWant(t, p, user.Id, "POST", "posts", req, 409)
	other := &model.User{Id: model.NewId(), Username: "other-admin", Roles: "system_admin"}
	f.users[other.Username] = other
	operatorWant(t, p, other.Id, "GET", "operations/human-correction", nil, 403)
	operatorWant(t, p, other.Id, "POST", "posts", req, 409)
}

func TestOperatorUncertaintyNeverRepostsAndRejectsForgedCanonicalEchoes(t *testing.T) {
	p, f, user := operatorFixture(t)
	channel := channelConversation(t, p, "room", "claude:b")
	f.postFault = "before"
	req := operatorPostRequest{OpID: "uncertain-human", ChannelID: channel, Message: "hold uncertainty"}
	operatorWant(t, p, user.Id, "POST", "posts", req, 409)
	f.postFault = ""
	o := p.state.Operations[req.OpID]
	for i, candidate := range []struct{ author, message string }{
		{p.state.Agents["codex:a"].UserID, req.Message}, {user.Id, "different content"},
	} {
		f.posts[fmt.Sprint("forged", i)] = &model.Post{Id: fmt.Sprint("forged", i), UserId: candidate.author,
			ChannelId: channel, Message: candidate.message, Props: model.StringInterface{
				"murmur_operator_op": o.ID, "murmur_operator_user": user.Id, "murmur_operator_hash": o.Hash}}
	}
	p = reload(t, p, f.fakeAPI)
	p.API = f
	operatorWant(t, p, user.Id, "POST", "posts", req, 409)
	result := operatorWant(t, p, user.Id, "GET", "operations/"+req.OpID, nil, 200)
	if result["status"] != "uncertain" || f.creates != 1 {
		t.Fatal("uncertainty was reposted or forged props proved success")
	}
	// A real late echo resolves the same operation, without another native call.
	post, _ := f.CreatePost(&model.Post{UserId: user.Id, ChannelId: channel, Message: req.Message, Props: model.StringInterface{
		"murmur_operator_op": o.ID, "murmur_operator_user": user.Id, "murmur_operator_hash": o.Hash}})
	p = reload(t, p, f.fakeAPI)
	p.API = f
	result = operatorWant(t, p, user.Id, "GET", "operations/"+req.OpID, nil, 200)
	if result["status"] != "complete" || result["result"].(map[string]any)["post_id"] != post.Id || f.creates != 2 {
		t.Fatal("late canonical echo was not recovered")
	}
}

func TestOperatorJournalFailureStopsPostAndRestartRecoversCommittedEffect(t *testing.T) {
	t.Run("intent failure", func(t *testing.T) {
		p, f, user := operatorFixture(t)
		channel := channelConversation(t, p, "room", "claude:b")
		f.kvFault = true
		operatorWant(t, p, user.Id, "POST", "posts", operatorPostRequest{OpID: "failed", ChannelID: channel, Message: "never create"}, 502)
		if f.creates != 0 {
			t.Fatal("post preceded durable journal")
		}
		operatorWant(t, p, user.Id, "GET", "bootstrap", nil, 503)
	})
	t.Run("completion failure", func(t *testing.T) {
		p, f, user := operatorFixture(t)
		channel := channelConversation(t, p, "room", "claude:b")
		f.afterCreate = func() { f.kvFault = true }
		req := operatorPostRequest{OpID: "committed", ChannelID: channel, Message: "create exactly once"}
		operatorWant(t, p, user.Id, "POST", "posts", req, 502)
		f.kvFault, f.afterCreate = false, nil
		p = reload(t, p, f.fakeAPI)
		p.API = f
		operatorWant(t, p, user.Id, "POST", "posts", req, 200)
		if f.creates != 1 {
			t.Fatal("restart repeated native effect after completion save failure")
		}
	})
}

func TestOperatorDuplicateEchoesHoldOnlyTheirOperation(t *testing.T) {
	p, f, user := operatorFixture(t)
	channel := channelConversation(t, p, "room", "claude:b")
	f.postFault = "before"
	req := operatorPostRequest{OpID: "duplicates", ChannelID: channel, Message: "one intended post"}
	operatorWant(t, p, user.Id, "POST", "posts", req, 409)
	f.postFault = ""
	o := p.state.Operations[req.OpID]
	for i := 0; i < 2; i++ {
		f.CreatePost(&model.Post{UserId: user.Id, ChannelId: channel, Message: req.Message,
			Props: model.StringInterface{"murmur_operator_op": o.ID, "murmur_operator_user": user.Id, "murmur_operator_hash": o.Hash}})
	}
	p = reload(t, p, f.fakeAPI)
	p.API = f
	result := operatorWant(t, p, user.Id, "GET", "operations/"+req.OpID, nil, 200)
	if result["status"] != "uncertain" || !strings.Contains(result["error"].(string), "duplicate") {
		t.Fatal("duplicate echoes were treated as one completed post")
	}
	operatorWant(t, p, user.Id, "POST", "posts", req, 409)
	operatorWant(t, p, user.Id, "POST", "posts", operatorPostRequest{OpID: "independent", ChannelID: channel, Message: "independent new request"}, 200)
	if f.creates != 4 {
		t.Fatal("duplicate operation was reposted or independent work was blocked")
	}
}

func TestOperatorDMNameUsesFixedNativePairAfterRenameAndDeparture(t *testing.T) {
	p, f, user := operatorFixture(t)
	room, err := p.conversation(conversationRequest{Actor: "codex:a", OpID: "dm", Kind: "dm", Members: []string{"claude:b"}})
	if err != nil {
		t.Fatal(err)
	}
	channel := room["conversation"].(map[string]any)["id"].(string)
	if _, err := p.register("codex:a", "Current A", ""); err != nil {
		t.Fatal(err)
	}
	if _, err := p.register("claude:b", "Current B", ""); err != nil {
		t.Fatal(err)
	}
	for i, actor := range []string{"codex:a", "claude:b"} {
		if _, err := p.membership(channel, "leave", memberRequest{Actor: actor, OpID: fmt.Sprint("leave-", i)}); err != nil {
			t.Fatal(err)
		}
		before, _ := json.Marshal(p.state)
		memberBefore, _ := json.Marshal(f.members)
		boot := operatorWant(t, p, user.Id, "GET", "bootstrap", nil, 200)
		entry := boot["conversations"].([]any)[0].(map[string]any)
		name := entry["name"].(string)
		if !strings.Contains(name, "Current A") || !strings.Contains(name, "Current B") || len(entry["members"].([]any)) != 1-i {
			t.Fatalf("native pair and current grants conflated: %#v", entry)
		}
		pair := entry["dm_user_ids"].([]any)
		wantPair := map[string]bool{p.state.Agents["codex:a"].UserID: true, p.state.Agents["claude:b"].UserID: true}
		if len(pair) != 2 || pair[0] == pair[1] || !wantPair[pair[0].(string)] || !wantPair[pair[1].(string)] {
			t.Fatalf("native DM identities were lost after grant departure: %#v", pair)
		}
		after, _ := json.Marshal(p.state)
		memberAfter, _ := json.Marshal(f.members)
		if !bytes.Equal(before, after) || !bytes.Equal(memberBefore, memberAfter) || len(f.members[channel]) != 2 {
			t.Fatal("reading DM directory changed grants or native pair")
		}
	}
	// Unavailable native identity must be visible, never guessed from old names.
	delete(f.channels, channel)
	operatorWant(t, p, user.Id, "GET", "bootstrap", nil, 502)
}

func TestOperatorBootstrapIdentifiesHumanDMPairWithoutAddingHumanToGrants(t *testing.T) {
	p, f, user := dmFixture(t)
	channel := openDM(t, p, user.Id, "human-dm", "codex:a")
	before, _ := json.Marshal(p.state)
	memberBefore, _ := json.Marshal(f.members)
	boot := operatorWant(t, p, user.Id, "GET", "bootstrap", nil, 200)
	entry := boot["conversations"].([]any)[0].(map[string]any)
	pair := entry["dm_user_ids"].([]any)
	wantPair := map[string]bool{user.Id: true, p.state.Agents["codex:a"].UserID: true}
	if entry["id"] != channel || len(pair) != 2 || pair[0] == pair[1] || !wantPair[pair[0].(string)] || !wantPair[pair[1].(string)] {
		t.Fatalf("human DM native pair missing or incorrect: %#v", entry)
	}
	if !reflect.DeepEqual(entry["members"], []any{"codex:a"}) {
		t.Fatalf("native participant was conflated with agent grants: %#v", entry["members"])
	}
	after, _ := json.Marshal(p.state)
	memberAfter, _ := json.Marshal(f.members)
	if !bytes.Equal(before, after) || !bytes.Equal(memberBefore, memberAfter) {
		t.Fatal("reading DM identities mutated state or membership")
	}
}
