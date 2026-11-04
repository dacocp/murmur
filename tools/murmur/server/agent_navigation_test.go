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
	"unicode/utf8"

	"github.com/mattermost/mattermost/server/public/model"
)

func navigationStatus(t *testing.T, p *Plugin, path string, want int) {
	t.Helper()
	r := httptest.NewRequest("GET", "/v1/"+path, nil)
	r.Header.Set("Authorization", "Bearer test-token")
	w := httptest.NewRecorder()
	p.ServeHTTP(nil, w, r)
	if w.Code != want || !json.Valid(w.Body.Bytes()) {
		t.Fatalf("GET %s: want %d JSON, got %d: %s", path, want, w.Code, w.Body)
	}
}

func TestAgentDiscoverySearchCompactPagesAndInvalidCursors(t *testing.T) {
	p, f := fixture(t)
	for i := 0; i < 47; i++ {
		id := fmt.Sprintf("codex:review-%02d", i)
		mustRegister(t, p, id, "")
		p.state.Agents[id].Name = fmt.Sprintf("Résumé Review %02d", i)
	}
	before, _ := json.Marshal(p.state)
	kvBefore := append([]byte{}, f.kv...)
	first := call(t, p, "agents")
	if len(first["agents"].([]any)) != 20 || first["next_after"] == nil {
		t.Fatal("blank directory is not bounded with continuation")
	}
	query := " RÉSUMÉ review "
	path := "agents?query=" + url.QueryEscape(query) + "&limit=13"
	ids := []string{}
	for {
		page := call(t, p, path)
		for _, value := range page["agents"].([]any) {
			a := value.(map[string]any)
			if len(a) != 3 || a["name"] == nil || a["username"] == nil {
				t.Fatalf("directory exposes non-compact metadata: %#v", a)
			}
			ids = append(ids, a["id"].(string))
		}
		if page["next_after"] == nil {
			break
		}
		path = "agents?query=" + url.QueryEscape(query) + "&limit=13&after=" + url.QueryEscape(page["next_after"].(string))
	}
	want := []string{}
	for i := 0; i < 47; i++ {
		want = append(want, fmt.Sprintf("codex:review-%02d", i))
	}
	if !reflect.DeepEqual(ids, want) {
		t.Fatalf("search paging omitted/repeated/out-of-order agents: %v", ids)
	}
	last := call(t, p, "agents?query=review&after=codex:review-46")
	if len(last["agents"].([]any)) != 0 || last["next_after"] != nil {
		t.Fatal("last cursor did not terminate")
	}
	byID := call(t, p, "agents?query=CODEX%3AREVIEW-11")
	if len(byID["agents"].([]any)) != 1 {
		t.Fatal("case-insensitive ID search failed")
	}
	empty := call(t, p, "agents?query=no-such-name")
	if values, ok := empty["agents"].([]any); !ok || len(values) != 0 || empty["next_after"] != nil {
		t.Fatal("empty directory page must be an array with no continuation")
	}
	for _, path := range []string{"agents?after=missing", "agents?query=review&after=claude:b", "agents?limit=0", "agents?limit=101", "agents?limit=abc"} {
		navigationStatus(t, p, path, 400)
	}
	after, _ := json.Marshal(p.state)
	if !bytes.Equal(before, after) || !bytes.Equal(kvBefore, f.kv) {
		t.Fatal("discovery changed persistent coordination state")
	}
}

func TestThreadSummariesBoundActivityOrderPreviewsAndCursorACL(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "room", "claude:b")
	mustRegister(t, p, "codex:child", "codex:a")
	other := channelConversation(t, p, "other", "claude:b")
	first := send(t, p, "codex:a", channel, "first", strings.Repeat("é", 200), "")
	second := send(t, p, "claude:b", channel, "second", "newer root", "")
	third := send(t, p, "codex:a", channel, "third", "newest root but no latest reply", "")
	reply := send(t, p, "claude:b", channel, "reply", "latest activity in oldest thread", first)
	f.posts[reply].CreateAt = 9000
	f.CreatePost(&model.Post{ChannelId: channel, RootId: second, Type: "system_join_channel", Message: "not a reply"})
	deleted, _ := f.CreatePost(&model.Post{ChannelId: channel, RootId: first, Message: "deleted reply", DeleteAt: 1})
	if deleted.DeleteAt != 1 {
		t.Fatal("fixture deletion not applied")
	}
	foreign := send(t, p, "codex:a", other, "foreign", "other conversation", "")
	before, _ := json.Marshal(p.state)
	firstPage := call(t, p, "conversations/"+channel+"/threads?actor=codex:child&limit=2")
	summaries := firstPage["threads"].([]any)
	if len(summaries) != 2 || summaries[0].(map[string]any)["id"] != first || summaries[1].(map[string]any)["id"] != third || firstPage["next_before"] != third {
		t.Fatalf("thread list is not sorted by latest activity: %#v", firstPage)
	}
	summary := summaries[0].(map[string]any)
	if summary["reply_count"] != float64(1) || summary["last_activity"] != float64(9000) || utf8.RuneCountInString(summary["preview"].(string)) != 160 || summary["message"] != nil || len(summary) != 7 {
		t.Fatalf("summary body/count/bounds incorrect: %#v", summary)
	}
	last := call(t, p, "conversations/"+channel+"/threads?actor=codex:child&limit=2&before="+third)
	if len(last["threads"].([]any)) != 1 || last["threads"].([]any)[0].(map[string]any)["id"] != second || last["next_before"] != nil {
		t.Fatalf("thread list paging failed: %#v", last)
	}
	// Equal activity must use the stable root ID tie breaker.
	f.posts[first].CreateAt, f.posts[second].CreateAt, f.posts[third].CreateAt, f.posts[reply].CreateAt = 100, 100, 100, 100
	want := []string{first, second, third}
	sort.Strings(want)
	tied := call(t, p, "conversations/"+channel+"/threads?actor=codex:child")["threads"].([]any)
	for i, value := range tied {
		if value.(map[string]any)["id"] != want[i] {
			t.Fatal("activity tie order is unstable")
		}
	}
	navigationStatus(t, p, "conversations/"+channel+"/threads?actor=codex:x", 200)
	for cursor, status := range map[string]int{foreign: 400, reply: 400, "missing": 404} {
		navigationStatus(t, p, "conversations/"+channel+"/threads?actor=codex:child&before="+cursor, status)
	}
	navigationStatus(t, p, "conversations/"+channel+"/threads?actor=codex:child&limit=101", 400)
	after, _ := json.Marshal(p.state)
	if !bytes.Equal(before, after) {
		t.Fatal("thread discovery changed grants, operations or event sequence")
	}
	// Default listing stays bounded as discussions accumulate.
	for i := 0; i < 22; i++ {
		f.CreatePost(&model.Post{ChannelId: channel, UserId: p.state.Agents["codex:a"].UserID, Message: fmt.Sprint("new root ", i)})
	}
	defaultPage := call(t, p, "conversations/"+channel+"/threads?actor=codex:child")
	if len(defaultPage["threads"].([]any)) != 20 || defaultPage["next_before"] == nil {
		t.Fatal("default thread listing is not bounded")
	}
	remainder := call(t, p, "conversations/"+channel+"/threads?actor=codex:child&before="+defaultPage["next_before"].(string))
	if len(remainder["threads"].([]any)) != 5 || remainder["next_before"] != nil {
		t.Fatal("default listing continuation lost or repeated discussions")
	}
	emptyChannel := channelConversation(t, p, "empty", "claude:b")
	empty := call(t, p, "conversations/"+emptyChannel+"/threads?actor=codex:child")
	if values, ok := empty["threads"].([]any); !ok || len(values) != 0 || empty["next_before"] != nil {
		t.Fatal("empty discussion list must be an array with no continuation")
	}
}

func TestThreadPageAnchorAndEqualTimestampPagingWithoutConsumption(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "room", "claude:b")
	mustRegister(t, p, "codex:child", "codex:a")
	root := send(t, p, "codex:a", channel, "root", "important older root", "")
	f.posts[root].CreateAt = 1
	replies := []string{}
	for i := 0; i < 45; i++ {
		id := send(t, p, "claude:b", channel, fmt.Sprint("reply-", i), fmt.Sprint("reply ", i), root)
		f.posts[id].CreateAt = 100
		replies = append(replies, id)
	}
	f.CreatePost(&model.Post{ChannelId: channel, RootId: root, Type: "system_join_channel", Message: "system event"})
	before, _ := json.Marshal(p.state)
	page := call(t, p, "threads/"+root+"?actor=codex:child")
	if len(page["posts"].([]any)) != 20 || page["next_before"] == nil || page["root"].(map[string]any)["id"] != root || page["root"].(map[string]any)["message"] != "important older root" {
		t.Fatalf("bounded page lost root context: %#v", page)
	}
	seen := map[string]bool{}
	for {
		for _, value := range page["posts"].([]any) {
			id := value.(map[string]any)["id"].(string)
			if seen[id] {
				t.Fatal("thread page duplicated post", id)
			}
			seen[id] = true
		}
		if page["next_before"] == nil {
			break
		}
		page = call(t, p, "threads/"+root+"?actor=codex:child&limit=7&before="+page["next_before"].(string))
		if page["root"].(map[string]any)["id"] != root {
			t.Fatal("older page lost separate canonical root anchor")
		}
	}
	if len(seen) != 46 || !seen[root] {
		t.Fatalf("thread paging omitted live posts: %d", len(seen))
	}
	older := call(t, p, "threads/"+root+"?actor=codex:child&before="+root)
	if len(older["posts"].([]any)) != 0 || older["next_before"] != nil || older["root"].(map[string]any)["id"] != root {
		t.Fatal("root boundary or anchor incorrect")
	}
	all := call(t, p, "threads/"+root+"?actor=codex:child&limit=200")["posts"].([]any)
	sort.Strings(replies)
	if len(all) != 46 || all[0].(map[string]any)["id"] != root {
		t.Fatal("explicit larger limit failed")
	}
	for i, id := range replies {
		if all[i+1].(map[string]any)["id"] != id {
			t.Fatal("equal-timestamp replies not ordered by canonical post ID")
		}
	}
	after, _ := json.Marshal(p.state)
	if !bytes.Equal(before, after) {
		t.Fatal("thread paging consumed messages or changed coordination state")
	}
}

func TestThreadNavigationRejectsOtherRootsRepliesDeletedAndForeignCursors(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "room", "claude:b")
	other := channelConversation(t, p, "other", "claude:b")
	root := send(t, p, "codex:a", channel, "root", "root", "")
	reply := send(t, p, "claude:b", channel, "reply", "reply", root)
	another := send(t, p, "codex:a", channel, "another", "other root in same channel", "")
	otherReply := send(t, p, "claude:b", channel, "other-reply", "other thread", another)
	foreign := send(t, p, "codex:a", other, "foreign", "other channel", "")
	system, _ := f.CreatePost(&model.Post{ChannelId: channel, Type: "system_join_channel", Message: "not a discussion"})
	navigationStatus(t, p, "threads/"+root+"?actor=codex:x", 200)
	navigationStatus(t, p, "threads/"+reply+"?actor=codex:a", 400)
	navigationStatus(t, p, "threads/"+system.Id+"?actor=codex:a", 400)
	for _, cursor := range []string{another, otherReply, foreign, system.Id} {
		navigationStatus(t, p, "threads/"+root+"?actor=codex:a&before="+cursor, 400)
	}
	navigationStatus(t, p, "threads/"+root+"?actor=codex:a&before=missing", 404)
	for _, limit := range []string{"0", "201", "x"} {
		navigationStatus(t, p, "threads/"+root+"?actor=codex:a&limit="+limit, 400)
	}
	f.posts[reply].DeleteAt = 1
	navigationStatus(t, p, "threads/"+root+"?actor=codex:a&before="+reply, 404)
	f.posts[root].DeleteAt = 1
	navigationStatus(t, p, "threads/"+root+"?actor=codex:a", 404)
	navigationStatus(t, p, "conversations/"+channel+"/threads?actor=codex:a&before="+root, 404)
}

func TestSinglePostRootValidationRetainsShowAndActualChannelAccess(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "room", "claude:b")
	mustRegister(t, p, "codex:child", "codex:a")
	root := send(t, p, "codex:a", channel, "root", "discussion root", "")
	reply := send(t, p, "claude:b", channel, "reply", "thread reply", root)
	system, _ := f.CreatePost(&model.Post{ChannelId: channel, Type: "system_join_channel", Message: "not a discussion"})
	before, _ := json.Marshal(p.state)
	checked := call(t, p, "posts/"+root+"?actor=codex:child&as_root=true")["post"].(map[string]any)
	if checked["id"] != root || checked["message"] != "discussion root" {
		t.Fatal("root validation did not return the single canonical root")
	}
	for _, id := range []string{reply, system.Id} {
		navigationStatus(t, p, "posts/"+id+"?actor=codex:child&as_root=true", 400)
		// Ordinary show still accepts non-root posts, with or without false.
		navigationStatus(t, p, "posts/"+id+"?actor=codex:child", 200)
		navigationStatus(t, p, "posts/"+id+"?actor=codex:child&as_root=false", 200)
	}
	for _, value := range []string{"", "yes", "1", "TRUE", "true&as_root=true"} {
		navigationStatus(t, p, "posts/"+root+"?actor=codex:child&as_root="+value, 400)
	}
	navigationStatus(t, p, "posts/"+root+"?actor=codex:x&as_root=true", 200)
	f.posts[root].DeleteAt = 1
	navigationStatus(t, p, "posts/"+root+"?actor=codex:child&as_root=true", 404)
	after, _ := json.Marshal(p.state)
	if !bytes.Equal(before, after) {
		t.Fatal("root selector validation changed coordination state")
	}
}
