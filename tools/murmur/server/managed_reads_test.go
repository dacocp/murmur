package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"net/url"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/mattermost/mattermost/server/public/model"
)

func managedRequest(t *testing.T, p *Plugin, method, path string, payload any, want int) map[string]any {
	t.Helper()
	data, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	r := httptest.NewRequest(method, "/v1/"+path, bytes.NewReader(data))
	r.Header.Set("Authorization", "Bearer test-token")
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	p.ServeHTTP(nil, w, r)
	if w.Code != want {
		t.Fatalf("%s %s: want%d got%d: %s", method, path, want, w.Code, w.Body)
	}
	var result map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	return result
}

func managedDM(t *testing.T, p *Plugin, op string) string {
	t.Helper()
	result, err := p.conversation(conversationRequest{Actor: "codex:a", OpID: op, Kind: "dm", Members: []string{"claude:b"}})
	if err != nil {
		t.Fatal(err)
	}
	return result["conversation"].(map[string]any)["id"].(string)
}

func TestManagedReadSettingDefaultsOffAndConfigurationReload(t *testing.T) {
	p, f := fixture(t)
	for _, enabled := range []bool{false, true} {
		p.config.AllowManagedConversationReads = enabled
		health := call(t, p, "health")
		capability := health["capabilities"].(map[string]any)["managed_conversation_reads"]
		if !reflect.DeepEqual(capability, map[string]any{"version": float64(1), "enabled": enabled}) {
			t.Fatal("wrong capability", capability)
		}
		bootstrap, err := p.operatorBootstrap(&model.User{})
		if err != nil || !reflect.DeepEqual(bootstrap["capabilities"].(map[string]any)["managed_conversation_reads"], p.managedReadCapability()) {
			t.Fatal("operator disclosure differs from effective read policy", bootstrap, err)
		}
	}
	config := &operatorConfigFake{fakeAPI: f, config: map[string]any{"coordinatortoken": "test-token", "allowmanagedconversationreads": true}}
	p.API = config
	if err := p.OnConfigurationChange(); err != nil || !p.config.AllowManagedConversationReads {
		t.Fatal("explicit enabling failed", err)
	}
	delete(config.config, "allowmanagedconversationreads")
	if err := p.OnConfigurationChange(); err != nil || p.config.AllowManagedConversationReads {
		t.Fatal("omitted setting did not return to default off", err)
	}
}

func TestManagedReadPrivateContentIsQuietAndDoesNotAuthorizeParticipation(t *testing.T) {
	p, f := fixture(t)
	private := legacyPrivateChannel(t, p, "private-read", "claude:b")
	dm := managedDM(t, p, "private-dm")
	public := channelConversation(t, p, "public-read")
	if _, err := p.registerMode("codex:manual", "Manual reader", "", nil, "manual"); err != nil {
		t.Fatal(err)
	}
	roots := map[string]string{}
	for _, channel := range []string{private, dm, public} {
		roots[channel] = send(t, p, "codex:a", channel, "root-"+channel, "private root", "")
		send(t, p, "codex:a", channel, "reply-"+channel, "private reply", roots[channel])
	}
	for _, channel := range []string{private, dm} {
		navigationStatus(t, p, "posts/"+roots[channel]+"?actor=codex:x&read_version=1", 403)
	}
	navigationStatus(t, p, "conversations/directory?actor=codex:x&read_version=1", 403)
	p.config.AllowManagedConversationReads = true
	beforeFeed := changesFor(t, p, "codex:x")
	before, _ := json.Marshal(p.state)
	members, _ := json.Marshal(f.members)
	kv, writes, adds, creates := append([]byte{}, f.kv...), f.kvWrites, f.addCalls, f.creates
	for _, actor := range []string{"codex:x", "codex:manual"} {
		for _, channel := range []string{private, dm, public} {
			root := roots[channel]
			for _, path := range []string{"conversations/" + channel + "/posts", "conversations/" + channel + "/threads", "threads/" + root, "posts/" + root, "posts/" + root + "?as_root=true"} {
				separator := "?"
				if strings.Contains(path, "?") {
					separator = "&"
				}
				page := call(t, p, path+separator+"actor="+actor+"&read_version=1")
				if descriptor, ok := page["conversation"].(map[string]any); ok && descriptor["role"] != nil {
					t.Fatal("quiet read invented a role", descriptor)
				}
			}
		}
	}
	for _, channel := range []string{private, dm} {
		for _, suffix := range []string{"", "&read_version=2", "&read_version=1&read_version=1"} {
			navigationStatus(t, p, "posts/"+roots[channel]+"?actor=codex:x"+suffix, 409)
		}
		managedRequest(t, p, "POST", "conversations/"+channel+"/join", memberRequest{Actor: "codex:x", OpID: "forbidden-join-" + channel}, 403)
		managedRequest(t, p, "POST", "conversations/"+channel+"/leave", memberRequest{Actor: "codex:x", OpID: "forbidden-leave-" + channel}, 403)
		managedRequest(t, p, "POST", "posts?read_version=1", postRequest{Actor: "codex:x", OpID: "forbidden-send-" + channel, ChannelID: channel, RootID: roots[channel], Message: "not mine"}, 403)
	}
	for _, channel := range []string{private, dm, public} {
		for _, version := range []string{"", "&read_version=1"} {
			navigationStatus(t, p, "posts/"+roots[channel]+"?actor=codex:a"+version, 200)
		}
		navigationStatus(t, p, "posts/"+roots[channel]+"?actor=codex:unregistered&read_version=1", 403)
	}
	unmanaged, _ := f.CreateChannel(&model.Channel{Type: "O", TeamId: p.state.TeamID})
	foreign, _ := f.CreatePost(&model.Post{ChannelId: unmanaged.Id, Message: "unmanaged native content"})
	navigationStatus(t, p, "conversations/"+unmanaged.Id+"/posts?actor=codex:x&read_version=1", 404)
	navigationStatus(t, p, "posts/"+foreign.Id+"?actor=codex:x&read_version=1", 404)
	navigationStatus(t, p, "threads/"+roots[dm]+"?actor=codex:x&before="+roots[private]+"&read_version=1", 400)
	navigationStatus(t, p, "conversations/"+private+"/posts?actor=codex:x&before="+roots[dm]+"&read_version=1", 400)
	delete(f.channels, unmanaged.Id)
	delete(f.members, unmanaged.Id)
	delete(f.posts, foreign.Id)
	afterFeed := changesFor(t, p, "codex:x")
	after, _ := json.Marshal(p.state)
	afterMembers, _ := json.Marshal(f.members)
	if !reflect.DeepEqual(beforeFeed, afterFeed) || !bytes.Equal(before, after) || !bytes.Equal(members, afterMembers) || !bytes.Equal(kv, f.kv) || writes != f.kvWrites || adds != f.addCalls || creates+1 != f.creates {
		t.Fatal("read-only policy changed feed, grants, memberships, journals or native effects")
	}
	p.config.AllowManagedConversationReads = false
	navigationStatus(t, p, "posts/"+roots[dm]+"?actor=codex:x&read_version=1", 403)
}

func TestManagedReadDirectoryBindsPagesAndIncludesHumanDMIdentity(t *testing.T) {
	p, f, human := operatorFixture(t)
	p.config.AllowManagedConversationReads = true
	p.config.OperatorUserID = human.Id
	dmResult, err := p.agentOperatorDM(memberRequest{Actor: "codex:a", OpID: "human-dm"})
	if err != nil {
		t.Fatal(err)
	}
	dm := dmResult["conversation"].(map[string]string)["id"]
	for i := 0; i < 23; i++ {
		channelConversation(t, p, fmt.Sprintf("directory-%02d", i), "claude:b")
	}
	private := legacyPrivateChannel(t, p, "directory-private", "claude:b")
	// Rename after DM creation and remove the agent's grant: fixed human/agent
	// identity, names and filtering must not depend on the current grant map.
	delete(f.users, human.Username)
	human.Username = "operator-current-name"
	f.users[human.Username] = human
	delete(p.state.Conversations[dm].Grants, "codex:a")
	before, _ := json.Marshal(p.state)
	members, _ := json.Marshal(f.members)
	kv, writes := append([]byte{}, f.kv...), f.kvWrites
	page := call(t, p, "conversations/directory?actor=codex:x&read_version=1")
	if len(page["conversations"].([]any)) != 20 || page["next_after"] == nil {
		t.Fatal("directory default is unbounded or has no continuation", page)
	}
	cursor := page["next_after"].(string)
	navigationStatus(t, p, "conversations/directory?actor=codex:x&read_version=1&query=Build&after="+url.QueryEscape(cursor), 400)
	navigationStatus(t, p, "conversations/directory?actor=claude:b&read_version=1&after="+url.QueryEscape(cursor), 400)
	ids := []string{}
	for {
		for _, item := range page["conversations"].([]any) {
			entry := item.(map[string]any)
			ids = append(ids, entry["id"].(string))
			if entry["role"] != nil || entry["preview"] != nil || entry["last_activity"] != nil {
				t.Fatal("directory implied tracking or fetched content", entry)
			}
		}
		if page["next_after"] == nil {
			break
		}
		page = call(t, p, "conversations/directory?actor=codex:x&read_version=1&after="+url.QueryEscape(page["next_after"].(string)))
	}
	want := []string{dm, private}
	for id, c := range p.state.Conversations {
		if c.Kind == "group" && id != private {
			want = append(want, id)
		}
	}
	sort.Strings(want)
	if !reflect.DeepEqual(ids, want) {
		t.Fatal("directory lost or duplicated pages", ids, want)
	}
	found := call(t, p, "conversations/directory?actor=codex:x&read_version=1&query=OPERATOR-CURRENT&agent=codex:a&kind=dm")
	entries := found["conversations"].([]any)
	if len(entries) != 1 || entries[0].(map[string]any)["id"] != dm {
		t.Fatal("human identity or departed DM participant was not discoverable", found)
	}
	entry := entries[0].(map[string]any)
	participants := entry["participants"].([]any)
	if len(participants) != 2 || !strings.Contains(entry["name"].(string), human.Username) {
		t.Fatal("wrong native DM participants", entry)
	}
	for _, raw := range participants {
		who := raw.(map[string]any)
		if len(who) != 4 || who["email"] != nil || who["roles"] != nil {
			t.Fatal("directory exposed non-compact personal data", who)
		}
		if who["kind"] == "human" && (who["id"] != human.Id || who["username"] != human.Username) {
			t.Fatal("human participant lacks stable identity", who)
		}
	}
	for _, suffix := range []string{"&limit=0", "&limit=101", "&kind=group", "&agent=missing", "&after=not-base64", "&query=" + url.QueryEscape(strings.Repeat("x", 257))} {
		navigationStatus(t, p, "conversations/directory?actor=codex:x&read_version=1"+suffix, 400)
	}
	navigationStatus(t, p, "conversations/directory?actor=codex:x", 409)
	navigationStatus(t, p, "conversations/directory?actor=unregistered&read_version=1", 403)
	after, _ := json.Marshal(p.state)
	afterMembers, _ := json.Marshal(f.members)
	if !bytes.Equal(before, after) || !bytes.Equal(members, afterMembers) || !bytes.Equal(kv, f.kv) || writes != f.kvWrites {
		t.Fatal("discovery changed enrollment, preference, grant or membership state")
	}
}

func TestManagedReadBookmarksStayPersonalWithoutTracking(t *testing.T) {
	p, f := fixture(t)
	channel := managedDM(t, p, "saved-dm")
	root := send(t, p, "codex:a", channel, "saved-root", "save privately", "")
	p.config.AllowManagedConversationReads = true
	yes, no := true, false
	human := &model.User{Id: model.NewId(), Username: "human-saved-owner", Email: "private@example.invalid"}
	f.users[human.Username] = human
	for _, userID := range []string{human.Id, p.state.Agents["codex:a"].UserID} {
		pref := model.Preference{UserId: userID, Category: model.PreferenceCategoryFlaggedPost, Name: root, Value: "true"}
		if err := f.UpdatePreferencesForUser(userID, []model.Preference{pref}); err != nil {
			t.Fatal(err)
		}
	}
	otherPreferences, _ := json.Marshal(f.preferences)
	before, _ := json.Marshal(p.state)
	members, _ := json.Marshal(f.members)
	managedRequest(t, p, "POST", "saved/"+root+"?read_version=1", savedRequest{Actor: "codex:x", Starred: &yes}, 200)
	page := call(t, p, "saved?actor=codex:x&read_version=1")
	items := page["saved"].([]any)
	if len(items) != 1 || items[0].(map[string]any)["conversation"].(map[string]any)["role"] != nil {
		t.Fatal("saved reference implied membership", page)
	}
	if len(call(t, p, "saved?actor=codex:a&read_version=1")["saved"].([]any)) != 1 {
		t.Fatal("reader changed another agent's saved work")
	}
	otherTarget := call(t, p, "saved?actor=claude:b&read_version=1&user_id="+human.Id)
	if len(otherTarget["saved"].([]any)) != 0 {
		t.Fatal("saved query selected another user's personal preferences", otherTarget)
	}
	operatorRequest := httptest.NewRequest("GET", "/operator/v1/bootstrap?actor=codex:x&read_version=1", nil)
	operatorRequest.Header.Set("Authorization", "Bearer test-token")
	operatorResponse := httptest.NewRecorder()
	p.ServeHTTP(nil, operatorResponse, operatorRequest)
	if operatorResponse.Code != 401 {
		t.Fatal("agent content access bypassed human operator authentication", operatorResponse.Code)
	}
	// An older client must see a version error, not a fabricated unavailable row.
	navigationStatus(t, p, "saved?actor=codex:x", 409)
	managedRequest(t, p, "POST", "saved/"+root, savedRequest{Actor: "codex:x", Starred: &yes}, 409)
	managedRequest(t, p, "POST", "saved/"+root+"?read_version=1", savedRequest{Actor: "codex:x", Starred: &no}, 200)
	after, _ := json.Marshal(p.state)
	afterMembers, _ := json.Marshal(f.members)
	delete(f.preferences, p.state.Agents["codex:x"].UserID)
	afterPreferences, _ := json.Marshal(f.preferences)
	if !bytes.Equal(before, after) || !bytes.Equal(members, afterMembers) || !bytes.Equal(otherPreferences, afterPreferences) {
		t.Fatal("bookmark modified shared state or was not removed")
	}
}

func TestManagedReadFilesRetainBindingAndDoNotAuthorizeUploads(t *testing.T) {
	p, f, _, _ := attachmentFixture(t)
	channel := legacyPrivateChannel(t, p, "quiet-file-room", "claude:b")
	file := uploadedFile(t, p, "codex:a", "", "quiet-file", channel)
	root := attachmentPost(t, p, "codex:a", "", "quiet-post", channel, "", []string{file}, 200)["post"].(map[string]any)["id"].(string)
	p.config.AllowManagedConversationReads = true
	before, _ := json.Marshal(p.state)
	members, _ := json.Marshal(f.members)
	get := func(info bool, want int) {
		r := postedFileHTTP("codex:x", "", root, file, info)
		r.URL.RawQuery += "&read_version=1"
		w := attachmentRequest(t, p, r, want)
		if want != 200 && bytes.Contains(w.Body.Bytes(), f.data[file]) {
			t.Fatal("unauthorized attachment bytes were returned")
		}
	}
	get(true, 200)
	get(false, 200)
	attachmentRequest(t, p, postedFileHTTP("codex:x", "", root, file, false), 409)
	upload := uploadHTTP("codex:x", "", "forbidden-upload", channel, "no.txt", []byte("must not upload"))
	upload.URL.RawQuery += "&read_version=1"
	attachmentRequest(t, p, upload, 403)
	f.files[file].ChannelId = model.NewId()
	get(false, 404)
	f.files[file].ChannelId = channel
	f.afterGet = func(string) {
		p.mu.Lock()
		p.config.AllowManagedConversationReads = false
		p.mu.Unlock()
	}
	get(false, 403)
	after, _ := json.Marshal(p.state)
	afterMembers, _ := json.Marshal(f.members)
	if !bytes.Equal(before, after) || !bytes.Equal(members, afterMembers) || f.createSessionCalls != 1 {
		t.Fatal("quiet file read changed state/membership or permitted another upload")
	}
}
