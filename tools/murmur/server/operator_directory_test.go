package main

import (
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"testing"

	"github.com/mattermost/mattermost/server/public/model"
)

// Legacy operator fixtures model only the old state-v1 blob. Directory activity
// also reads a separate optional workspace key; an absent key must stay absent.
type directoryAPI struct{ *operatorFake }

func (f *directoryAPI) KVGet(key string) ([]byte, *model.AppError) {
	if strings.HasPrefix(key, "operator-workspace-v1-") {
		return nil, nil
	}
	return f.operatorFake.KVGet(key)
}

func TestOperatorPersonalDirectoryValidation(t *testing.T) {
	p, f, user := operatorFixture(t)
	for _, suffix := range []string{"?limit=0", "?limit=101", "?limit=no", "?before=broken", "?before=" + strings.Repeat("x", 513), "?q=" + url.QueryEscape(strings.Repeat("界", 257))} {
		_, err := p.operatorYourMessages(user, httptest.NewRequest("GET", "/your-messages"+suffix, nil))
		if failure, ok := err.(*httpError); !ok || failure.status != 400 {
			t.Fatalf("%s: %v", suffix, err)
		}
	}
	for _, path := range []string{"your-messages", "agent-activity"} {
		operatorWant(t, p, "", "GET", path, nil, 401)
		f.permission = false
		operatorWant(t, p, user.Id, "GET", path, nil, 403)
		f.permission = true
		operatorWant(t, p, user.Id, "POST", path, nil, 404)
	}
	_, err := p.operatorYourMessages(user, httptest.NewRequest("GET", "/your-messages", nil))
	if failure, ok := err.(*httpError); !ok || failure.status != 503 {
		t.Fatal("unsupported history must be explicit", err)
	}
	_, err = p.operatorAgentActivity(user, nil)
	if failure, ok := err.(*httpError); !ok || failure.status != 503 {
		t.Fatal("unsupported activity must be explicit", err)
	}
}

func TestPostgresOperatorPersonalDirectory(t *testing.T) {
	_, db := metadataDatabase(t)
	_, err := db.Exec(`CREATE TABLE Posts (Id text PRIMARY KEY,ChannelId text NOT NULL,UserId text NOT NULL,Message text NOT NULL,
 RootId text NOT NULL,CreateAt bigint NOT NULL,UpdateAt bigint NOT NULL,DeleteAt bigint NOT NULL,Type text NOT NULL,Props jsonb,FileIds varchar(300))`)
	if err != nil {
		t.Fatal(err)
	}
	p, f, user := operatorFixture(t)
	p.history = &postgresHistory{db: db}
	p.API = &directoryAPI{operatorFake: f}
	channel := channelConversation(t, p, "personal-board", "claude:b")
	dm := channelConversation(t, p, "observed-dm", "claude:b")
	p.state.Conversations[dm].Kind = "dm"
	a, b := p.state.Agents["codex:a"].UserID, p.state.Agents["claude:b"].UserID
	outsider := model.NewId()
	id := func(n int) string { return fmt.Sprintf("%026d", n) }
	insert := func(n int, room, author, root, message string, at int64, kind string, deleted int64) string {
		t.Helper()
		postID := id(n)
		_, err := db.Exec(`INSERT INTO Posts VALUES($1,$2,$3,$4,$5,$6,$6,$7,$8,'{}',NULL)`, postID, room, author, message, root, at, deleted, kind)
		if err != nil {
			t.Fatal(err)
		}
		f.posts[postID] = &model.Post{Id: postID, ChannelId: room, UserId: author, RootId: root, Message: message, CreateAt: at, UpdateAt: at, DeleteAt: deleted, Type: kind}
		return postID
	}
	// Native-authored rows have no Murmur operation props and still belong in Your messages.
	ownRoot := insert(1, channel, user.Id, "", "Human unanswered root", 100, "", 0)
	for n := 2; n <= 122; n++ {
		insert(n, channel, user.Id, "", "Human tied root", 100, "", 0)
	}
	thread := insert(123, dm, a, "", "Observed agent thread", 1, "", 0)
	ownReply := insert(124, dm, user.Id, thread, `México literal 50%_\`, 2, "", 0)
	replyA := insert(125, channel, a, ownRoot, "reply to human", 101, "", 0)
	replyB := insert(126, dm, b, thread, "reply in participated", 102, "", 0)
	followed := insert(127, channel, a, "", "follow this root", 103, "", 0)
	followedReply := insert(128, channel, b, followed, "followed reply", 104, "", 0)
	latestA := insert(129, channel, a, "", "unrelated latest", 105, "", 0)
	insert(130, channel, b, "", "unrelated latest B", 106, "", 0)
	latestB := insert(136, channel, b, "", "same time, later ID", 106, "", 0)
	p.state.Agents["codex:idle"] = &Agent{ID: "codex:idle", UserID: model.NewId(), Name: "Idle"}
	insert(131, outsider, user.Id, "", "unmanaged human post", 999, "", 0)
	gone := insert(132, channel, a, "", "deleted root", 999, "", 1)
	insert(133, channel, user.Id, gone, "orphan human reply", 999, "", 0)
	insert(134, channel, user.Id, "", "deleted human", 999, "", 1)
	insert(135, channel, user.Id, "", "system human", 999, "system_join_channel", 0)
	httpPage := operatorWant(t, p, user.Id, "GET", "your-messages?limit=1", nil, 200)
	if posts := httpPage["posts"].([]any); len(posts) != 1 || posts[0].(map[string]any)["id"] != id(122) {
		t.Fatal("HTTP route must select bound author", httpPage)
	}
	activityPage := operatorWant(t, p, user.Id, "GET", "agent-activity", nil, 200)
	if len(activityPage["agents"].([]any)) != len(p.state.Agents) {
		t.Fatal("HTTP activity route", activityPage)
	}
	beforeState, _ := json.Marshal(p.state)
	got := []string{}
	cursor := ""
	firstCursor := ""
	for page := 0; page < 3; page++ {
		request := httptest.NewRequest("GET", "/your-messages?limit=50&before="+url.QueryEscape(cursor), nil)
		result, err := p.operatorYourMessages(user, request)
		if err != nil {
			t.Fatal(err)
		}
		rows := result["posts"].([]map[string]any)
		want := 50
		if page == 2 {
			want = 23
		}
		if len(rows) != want {
			t.Fatalf("page%d: got%d want%d", page, len(rows), want)
		}
		for _, row := range rows {
			got = append(got, row["id"].(string))
		}
		if page < 2 {
			cursor = result["next_before"].(string)
			if firstCursor == "" {
				firstCursor = cursor
			}
		} else if result["next_before"] != nil {
			t.Fatal("unexpected continuation")
		}
	}
	want := []string{}
	for n := 122; n >= 1; n-- {
		want = append(want, id(n))
	}
	want = append(want, ownReply)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("own-author ordering/scope: %#v", got)
	}
	for _, query := range []string{`50%_\`, "méxico"} {
		result, err := p.operatorYourMessages(user, httptest.NewRequest("GET", "/your-messages?q="+url.QueryEscape(query), nil))
		if err != nil {
			t.Fatal(err)
		}
		rows := result["posts"].([]map[string]any)
		if len(rows) != 1 || rows[0]["id"] != ownReply {
			t.Fatal("literal/Unicode recall", result)
		}
	}
	other := *user
	other.Id = model.NewId()
	for _, tc := range []struct {
		who   *model.User
		query string
	}{{user, "different"}, {&other, ""}} {
		_, err := p.operatorYourMessages(tc.who, httptest.NewRequest("GET", "/your-messages?before="+url.QueryEscape(firstCursor)+"&q="+tc.query, nil))
		if failure, ok := err.(*httpError); !ok || failure.status != 400 {
			t.Fatal("cursor must bind query and author", err)
		}
	}
	facts, err := p.history.(operatorDirectoryHistory).AgentActivity([]string{channel, dm}, []string{a, b}, user.Id, []string{followed})
	if err != nil {
		t.Fatal(err)
	}
	if facts[a].latest.Id != latestA || facts[a].personal.Id != followed || facts[b].latest.Id != latestB || facts[b].personal.Id != followedReply {
		t.Fatalf("activity/follow facts: %#v %#v", facts[a], facts[b])
	}
	noFollows, err := p.history.(operatorDirectoryHistory).AgentActivity([]string{channel, dm}, []string{a, b}, user.Id, nil)
	if err != nil {
		t.Fatal(err)
	}
	if noFollows[a].personal.Id != replyA || noFollows[b].personal.Id != replyB {
		t.Fatal("participation must use exact root/channel human authorship", noFollows)
	}
	directory, err := p.operatorAgentActivity(user, []string{followed})
	if err != nil {
		t.Fatal(err)
	}
	if len(directory["agents"].([]map[string]any)) != len(p.state.Agents) {
		t.Fatal("directory must preserve enrolled identities", directory)
	}
	for _, entry := range directory["agents"].([]map[string]any) {
		if entry["id"] == "codex:idle" && (entry["latest"] != nil || entry["latest_personal"] != nil) {
			t.Fatal("enrollment must not fabricate activity", entry)
		}
	}
	if _, err := db.Exec(`UPDATE Posts SET DeleteAt=1 WHERE Id=$1`, ownReply); err != nil {
		t.Fatal(err)
	}
	noParticipation, err := p.history.(operatorDirectoryHistory).AgentActivity([]string{channel, dm}, []string{a, b}, user.Id, nil)
	if err != nil {
		t.Fatal(err)
	}
	if noParticipation[b].personal != nil || noParticipation[b].latest.Id != latestB {
		t.Fatal("deleted contribution must not imply participation", noParticipation[b])
	}
	afterState, _ := json.Marshal(p.state)
	if string(beforeState) != string(afterState) {
		t.Fatal("personal history/activity changed state")
	}
	if _, err := db.Exec(`UPDATE Posts SET DeleteAt=1 WHERE Id=$1`, id(73)); err != nil {
		t.Fatal(err)
	}
	_, err = p.operatorYourMessages(user, httptest.NewRequest("GET", "/your-messages?before="+url.QueryEscape(firstCursor), nil))
	if failure, ok := err.(*httpError); !ok || failure.status != 404 {
		t.Fatal("deleted cursor must explicitly restart", err)
	}
	filesByPost := map[string]model.StringArray{}
	for _, post := range []string{ownRoot, latestA, latestB, followed, followedReply} {
		files := model.StringArray{model.NewId(), model.NewId()}
		encodedFiles, _ := json.Marshal(files)
		if _, err := db.Exec(`UPDATE Posts SET FileIds=$1 WHERE Id=$2`, string(encodedFiles), post); err != nil {
			t.Fatal(err)
		}
		filesByPost[post] = files
	}
	directoryHistory := p.history.(operatorDirectoryHistory)
	own, _, err := directoryHistory.OwnPosts([]string{channel, dm}, user.Id, "Human unanswered", "", 50)
	if err != nil || len(own) != 1 || own[0].Id != ownRoot || !reflect.DeepEqual(own[0].FileIds, filesByPost[ownRoot]) {
		t.Fatalf("personal messages lost attachment IDs/order: %#v %v", own, err)
	}
	facts, err = directoryHistory.AgentActivity([]string{channel, dm}, []string{a, b}, user.Id, []string{followed})
	if err != nil {
		t.Fatal(err)
	}
	for _, post := range []*model.Post{facts[a].latest, facts[a].personal, facts[b].latest, facts[b].personal} {
		if post == nil || !reflect.DeepEqual(post.FileIds, filesByPost[post.Id]) || len(post.FileIds) != 2 {
			t.Fatalf("agent activity lost attachment IDs/order: %#v", post)
		}
	}
}
