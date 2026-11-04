package main

import (
	"encoding/json"
	"fmt"
	"net/url"
	"reflect"
	"strings"
	"testing"

	"github.com/mattermost/mattermost/server/public/model"
)

func TestOperatorSearchValidationAndUnavailableCapability(t *testing.T) {
	p, f, user := operatorFixture(t)
	channel := channelConversation(t, p, "search-scope", "claude:b")
	root := send(t, p, "codex:a", channel, "scope-root", "root", "")
	reply := send(t, p, "claude:b", channel, "scope-reply", "reply", root)
	for _, query := range []string{"", "   ", strings.Repeat("界", 257), string([]byte{0xff})} {
		operatorWant(t, p, user.Id, "GET", "search?q="+url.QueryEscape(query), nil, 400)
	}
	for _, suffix := range []string{"&limit=0", "&limit=101", "&limit=no", "&conversation=bad", "&root=bad", "&before=not-a-cursor", "&before=" + strings.Repeat("a", 513)} {
		operatorWant(t, p, user.Id, "GET", "search?q=word"+suffix, nil, 400)
	}
	operatorWant(t, p, user.Id, "GET", "search?q=word&conversation="+model.NewId(), nil, 404)
	operatorWant(t, p, user.Id, "GET", "search?q=word&root="+model.NewId(), nil, 404)
	operatorWant(t, p, user.Id, "GET", "search?q=word&root="+reply, nil, 400)
	other := channelConversation(t, p, "other-scope", "claude:b")
	operatorWant(t, p, user.Id, "GET", "search?q=word&conversation="+other+"&root="+root, nil, 400)
	f.posts[root].DeleteAt = 1
	operatorWant(t, p, user.Id, "GET", "search?q=word&root="+root, nil, 404)
	operatorWant(t, p, user.Id, "GET", "search?q=word", nil, 503)
	operatorWant(t, p, user.Id, "GET", "search?q="+url.QueryEscape(strings.Repeat("界", 256)), nil, 503)
	operatorWant(t, p, "", "GET", "search?q=private", nil, 401)
	operatorWant(t, p, user.Id, "POST", "search?q=word", nil, 404)
}

func TestPostgresOperatorSearchScopePagingAndLiteralMatching(t *testing.T) {
	_, db := metadataDatabase(t)
	_, err := db.Exec(`CREATE TABLE Posts (Id text PRIMARY KEY,ChannelId text NOT NULL,UserId text NOT NULL,Message text NOT NULL,
 RootId text NOT NULL,CreateAt bigint NOT NULL,UpdateAt bigint NOT NULL,DeleteAt bigint NOT NULL,Type text NOT NULL,Props jsonb,FileIds varchar(300))`)
	if err != nil {
		t.Fatal(err)
	}
	p, f, user := operatorFixture(t)
	h := &postgresHistory{db: db}
	p.history = h
	channel := channelConversation(t, p, "search-public", "claude:b")
	dm := channelConversation(t, p, "search-observed-dm", "claude:b")
	p.state.Conversations[dm].Kind = "dm" // Search uses managed IDs, never human membership/grants.
	outsider := model.NewId()
	id := func(n int) string { return fmt.Sprintf("%026d", n) }
	insert := func(n int, room, root, text, kind string, at, deleted int64) string {
		t.Helper()
		postID := id(n)
		_, err := db.Exec(`INSERT INTO Posts VALUES ($1,$2,$3,$4,$5,$6,$6,$7,$8,'{}',NULL)`, postID, room, user.Id, text, root, at, deleted, kind)
		if err != nil {
			t.Fatal(err)
		}
		f.posts[postID] = &model.Post{Id: postID, ChannelId: room, UserId: user.Id, Message: text, RootId: root, CreateAt: at, UpdateAt: at, DeleteAt: deleted, Type: kind}
		return postID
	}
	for n := 1; n <= 120; n++ {
		insert(n, channel, "", "Needle root", "", 100, 0)
	}
	old := insert(121, dm, "", "An OLD needle in the observed DM", "", 1, 0)
	thread := insert(122, channel, "", "Thread needle", "", 101, 0)
	reply := insert(123, channel, thread, "Needle reply", "", 102, 0)
	literal := insert(124, channel, thread, `literal 50%_\ token`, "", 103, 0)
	insert(125, channel, thread, "literal 50abc token", "", 104, 0)
	insert(126, outsider, "", "Needle secret unmanaged", "", 999, 0)
	insert(127, channel, "", "Needle deleted", "", 999, 1)
	insert(128, channel, "", "Needle system", "system_join_channel", 999, 0)
	deletedRoot := insert(129, channel, "", "gone root", "", 999, 1)
	insert(130, channel, deletedRoot, "Needle orphan deleted root", "", 999, 0)
	insert(131, channel, id(900), "Needle orphan missing root", "", 999, 0)
	foreignRoot := insert(132, outsider, "", "foreign root", "", 999, 0)
	insert(133, channel, foreignRoot, "Needle malformed crosschannel reply", "", 999, 0)
	systemRoot := insert(134, channel, "", "system root", "system_join_channel", 999, 0)
	insert(135, channel, systemRoot, "Needle reply to system root", "", 999, 0)
	unicodePost := insert(136, channel, "", "MÉXICO", "", 110, 0)
	stateBefore, _ := json.Marshal(p.state)
	path := "search?q=NeEdLe&limit=50"
	got := []string{}
	cursors := []string{}
	for page := 0; page < 3; page++ {
		result := operatorWant(t, p, user.Id, "GET", path, nil, 200)
		posts := result["posts"].([]any)
		expectedLen := 50
		if page == 2 {
			expectedLen = 23
		}
		if len(posts) != expectedLen {
			t.Fatalf("page %d has %d posts", page, len(posts))
		}
		for _, entry := range posts {
			got = append(got, entry.(map[string]any)["id"].(string))
		}
		if page < 2 {
			cursor, ok := result["next_before"].(string)
			if !ok || cursor == "" {
				t.Fatal("missing continuation", result)
			}
			cursors = append(cursors, cursor)
			path = "search?q=NeEdLe&limit=50&before=" + url.QueryEscape(cursor)
		} else if result["next_before"] != nil {
			t.Fatal("unexpected final continuation")
		}
	}
	expected := []string{reply, thread}
	for n := 120; n >= 1; n-- {
		expected = append(expected, id(n))
	}
	expected = append(expected, old)
	if !reflect.DeepEqual(got, expected) {
		t.Fatal("search omitted old/tied posts, leaked invalid posts, or reordered results", got)
	}
	recent, _, err := h.RootBoard(channel, "", 50)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range recent {
		if entry.post.Id == id(1) {
			t.Fatal("old-hit fixture did not cross loaded page")
		}
	}
	dmOnly := operatorWant(t, p, user.Id, "GET", "search?q=needle&conversation="+dm, nil, 200)
	if len(dmOnly["posts"].([]any)) != 1 || dmOnly["posts"].([]any)[0].(map[string]any)["id"] != old {
		t.Fatal(dmOnly)
	}
	scoped := operatorWant(t, p, user.Id, "GET", "search?q=needle&root="+thread, nil, 200)
	if len(scoped["posts"].([]any)) != 2 {
		t.Fatal(scoped)
	}
	for _, entry := range scoped["posts"].([]any) {
		post := entry.(map[string]any)
		if post["id"] != thread && post["id"] != reply {
			t.Fatal("thread scope widened", post)
		}
	}
	for _, query := range []string{`50%_\`, "%", "_", `\`} {
		result := operatorWant(t, p, user.Id, "GET", "search?q="+url.QueryEscape(query), nil, 200)
		posts := result["posts"].([]any)
		if len(posts) != 1 || posts[0].(map[string]any)["id"] != literal {
			t.Fatalf("nonliteral query %q: %#v", query, result)
		}
	}
	unicodeResult := operatorWant(t, p, user.Id, "GET", "search?q="+url.QueryEscape("méxico"), nil, 200)
	if posts := unicodeResult["posts"].([]any); len(posts) != 1 || posts[0].(map[string]any)["id"] != unicodePost {
		t.Fatal("Unicode case matching", unicodeResult)
	}
	noHits := operatorWant(t, p, user.Id, "GET", "search?q=does-not-exist", nil, 200)
	if len(noHits["posts"].([]any)) != 0 || noHits["next_before"] != nil {
		t.Fatal("empty search", noHits)
	}
	for _, suffix := range []string{"search?q=other", "search?q=NeEdLe&conversation=" + channel, "search?q=NeEdLe&root=" + thread} {
		operatorWant(t, p, user.Id, "GET", suffix+"&before="+url.QueryEscape(cursors[0]), nil, 400)
	}
	anchor := id(73) // first page: reply, root, then 120..73
	if _, err = db.Exec("UPDATE Posts SET DeleteAt=1 WHERE Id=$1", anchor); err != nil {
		t.Fatal(err)
	}
	operatorWant(t, p, user.Id, "GET", "search?q=NeEdLe&before="+url.QueryEscape(cursors[0]), nil, 404)
	stateAfter, _ := json.Marshal(p.state)
	if string(stateBefore) != string(stateAfter) {
		t.Fatal("search changed managed state or attention")
	}
	if _, err = db.Exec("UPDATE Posts SET DeleteAt=1 WHERE Id=$1", thread); err != nil {
		t.Fatal(err)
	}
	_, _, err = h.SearchPosts([]string{channel}, thread, "needle", "", 50)
	if failure, ok := err.(*httpError); !ok || failure.status != 404 {
		t.Fatal("deleted thread scope accepted", err)
	}
	files := model.StringArray{model.NewId(), model.NewId()}
	encodedFiles, _ := json.Marshal(files)
	if _, err := db.Exec(`UPDATE Posts SET FileIds=$1 WHERE Id=$2`, string(encodedFiles), old); err != nil {
		t.Fatal(err)
	}
	hits, _, err := h.SearchPosts([]string{dm}, "", "needle", "", 50)
	if err != nil || len(hits) != 1 || hits[0].Id != old || !reflect.DeepEqual(hits[0].FileIds, files) {
		t.Fatalf("search lost attachment IDs/order: %#v %v", hits, err)
	}
	hits, _, err = h.SearchPosts([]string{dm}, "", files[0], "", 50)
	if err != nil || len(hits) != 0 {
		t.Fatalf("attachment projection changed message-only search: %#v %v", hits, err)
	}
}
