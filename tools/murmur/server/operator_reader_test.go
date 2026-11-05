package main

import (
	"encoding/json"
	"fmt"
	"github.com/mattermost/mattermost/server/public/model"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestOperatorReaderContextAndPrivateErrors(t *testing.T) {
	p, f, user := operatorFixture(t)
	channel := channelConversation(t, p, "reader-room", "claude:b")
	root := send(t, p, "codex:a", channel, "reader-root", "root", "")
	reply := send(t, p, "claude:b", channel, "reader-reply", "reply", root)
	for _, id := range []string{root, reply} {
		result := operatorWant(t, p, user.Id, "GET", "context/"+id, nil, 200)
		if result["post"].(map[string]any)["id"] != id || result["root"].(map[string]any)["id"] != root || result["channel_id"] != channel {
			t.Fatalf("wrong context: %#v", result)
		}
	}
	f.posts[root].DeleteAt = 1
	operatorWant(t, p, user.Id, "GET", "context/"+reply, nil, 404)
	f.posts[root].DeleteAt = 0
	foreign := f.posts[root].Clone()
	foreign.Id = model.NewId()
	foreign.ChannelId = model.NewId()
	f.posts[foreign.Id] = foreign
	f.posts[reply].RootId = foreign.Id
	operatorWant(t, p, user.Id, "GET", "context/"+reply, nil, 404)
	p.state.Conversations[foreign.ChannelId] = &Conversation{ID: foreign.ChannelId, Kind: "group", Grants: map[string]Grant{}}
	operatorWant(t, p, user.Id, "GET", "context/"+reply, nil, 409)
	delete(p.state.Conversations, foreign.ChannelId)
	f.posts[reply].RootId = root
	f.posts[reply].Type = "system_join_channel"
	operatorWant(t, p, user.Id, "GET", "context/"+reply, nil, 404)
	for _, tc := range []struct {
		actor, path string
		status      int
	}{
		{"", "bootstrap", 401}, {"unknown", "bootstrap", 403}, {user.Id, "context/missing", 404},
		{user.Id, "context/" + root, 200}, {user.Id, "conversations/" + channel + "/board", 503},
	} {
		r := httptest.NewRequest("GET", "/operator/v1/"+tc.path, nil)
		r.Header.Set("Mattermost-User-Id", tc.actor)
		w := httptest.NewRecorder()
		p.ServeHTTP(nil, w, r)
		if w.Code != tc.status || w.Header().Get("Cache-Control") != "private, no-store" {
			t.Fatalf("%s: %d %v", tc.path, w.Code, w.Header())
		}
	}
	boot := operatorWant(t, p, user.Id, "GET", "bootstrap", nil, 200)
	if !reflect.DeepEqual(boot["capabilities"], map[string]any{"root_board": false, "exact_context": true, "full_history_search": false, "own_messages": false, "agent_activity": false, "personal_workspace": true,
		"attachments":                map[string]any{"enabled": true, "max_file_size": float64(100 * 1024 * 1024), "max_files_per_post": float64(10)},
		"managed_conversation_reads": map[string]any{"version": float64(1), "enabled": false}}) {
		t.Fatal(boot["capabilities"])
	}
}

func TestPostgresOperatorRootBoard(t *testing.T) {
	_, db := metadataDatabase(t)
	_, err := db.Exec(`CREATE TABLE Posts (Id text PRIMARY KEY,ChannelId text NOT NULL,UserId text NOT NULL,Message text NOT NULL,
 RootId text NOT NULL,CreateAt bigint NOT NULL,UpdateAt bigint NOT NULL,DeleteAt bigint NOT NULL,Type text NOT NULL,Props jsonb,FileIds varchar(300))`)
	if err != nil {
		t.Fatal(err)
	}
	insert := func(id, channel, root, kind string, at, deleted int64) {
		t.Helper()
		_, err := db.Exec(`INSERT INTO Posts VALUES ($1,$2,'author',$1,$3,$4,$4,$5,$6,'{}',NULL)`, id, channel, root, at, deleted, kind)
		if err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{"a", "b", "c", "d", "e"} {
		insert(id, "room", "", "", 100, 0)
	}
	for i := 0; i < 205; i++ {
		insert(fmt.Sprintf("reply-%03d", i), "room", "b", "", 200+int64(i), 0)
	}
	insert("custom-reply", "room", "b", "custom_post", 500, 0)
	insert("deleted-reply", "room", "b", "", 501, 1)
	insert("system-reply", "room", "b", "system_join_channel", 502, 0)
	insert("foreign-reply", "elsewhere", "b", "", 503, 0)
	insert("deleted-root", "room", "", "", 600, 1)
	insert("system-root", "room", "", "system_join_channel", 601, 0)
	insert("foreign-root", "elsewhere", "", "", 602, 0)
	h := &postgresHistory{db: db}
	p, _, user := operatorFixture(t)
	p.history = h
	p.state.Conversations["room"] = &Conversation{ID: "room", Kind: "group", Name: "Test board", Grants: map[string]Grant{}}
	stateBefore, _ := json.Marshal(p.state)
	result := operatorWant(t, p, user.Id, "GET", "conversations/room/board?limit=2", nil, 200)
	page := result["posts"].([]any)
	if len(page) != 2 || page[0].(map[string]any)["id"] != "d" || page[1].(map[string]any)["id"] != "e" || result["next_before"] != "d" {
		t.Fatalf("board route did not use complete roots: %#v", result)
	}
	operatorWant(t, p, user.Id, "GET", "conversations/unmanaged/board", nil, 404)
	operatorWant(t, p, user.Id, "GET", "conversations/room/board?limit=101", nil, 400)
	stateAfter, _ := json.Marshal(p.state)
	if string(stateBefore) != string(stateAfter) {
		t.Fatal("reader mutated managed state")
	}
	files := model.StringArray{model.NewId(), model.NewId()}
	encodedFiles, _ := json.Marshal(files)
	if _, err := db.Exec(`UPDATE Posts SET FileIds=$1 WHERE Id='d'`, string(encodedFiles)); err != nil {
		t.Fatal(err)
	}

	before := ""
	got := []string{}
	for pageIndex := 0; pageIndex < 3; pageIndex++ {
		rows, next, err := h.RootBoard("room", before, 2)
		if err != nil {
			t.Fatal(err)
		}
		for _, row := range rows {
			got = append(got, row.post.Id)
			if row.post.Id == "d" && !reflect.DeepEqual(row.post.FileIds, files) {
				t.Fatalf("root board lost attachment IDs/order: %#v", row.post.FileIds)
			}
			expected := int64(0)
			if row.post.Id == "b" {
				expected = 206
			}
			if row.replies != expected {
				t.Fatalf("%s reply count %d, want %d", row.post.Id, row.replies, expected)
			}
		}
		expectedNext := []string{"d", "b", ""}[pageIndex]
		if next != expectedNext {
			t.Fatalf("page%d cursor %q", pageIndex, next)
		}
		before = next
	}
	if !reflect.DeepEqual(got, []string{"d", "e", "b", "c", "a"}) {
		t.Fatal("lost/duplicated roots or wrong tie ordering", got)
	}
	canonical, err := h.Posts("room")
	if err != nil {
		t.Fatal(err)
	}
	foundFiles := false
	for _, post := range canonical {
		if post.Id == "d" {
			foundFiles = true
			if !reflect.DeepEqual(post.FileIds, files) {
				t.Fatalf("canonical history lost attachment IDs/order: %#v", post.FileIds)
			}
		} else if post.FileIds != nil {
			t.Fatalf("legacy NULL FileIds should remain nil: %s %#v", post.Id, post.FileIds)
		}
	}
	if !foundFiles {
		t.Fatal("canonical history omitted attachment-bearing post")
	}
	for _, cursor := range []string{"missing", "foreign-root", "reply-001", "deleted-root", "system-root"} {
		_, _, err := h.RootBoard("room", cursor, 2)
		if e, ok := err.(*httpError); !ok || e.status != 404 {
			t.Fatalf("accepted unavailable/non-root cursor %s: %v", cursor, err)
		}
	}
	rows, next, err := h.RootBoard("empty", "", 2)
	if err != nil || len(rows) != 0 || next != "" {
		t.Fatalf("empty board: %v %s %v", rows, next, err)
	}
}
