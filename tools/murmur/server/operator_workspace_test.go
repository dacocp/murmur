package main

import (
	"encoding/json"
	"net/url"
	"reflect"
	"testing"
	"time"

	"github.com/mattermost/mattermost/server/public/model"
)

type workspaceFake struct {
	*operatorFake
	data       map[string][]byte
	readFault  bool
	writeFault string
}

func (f *workspaceFake) KVGet(key string) ([]byte, *model.AppError) {
	if f.readFault {
		return nil, unavailable()
	}
	return append([]byte{}, f.data[key]...), nil
}
func (f *workspaceFake) KVSet(key string, data []byte) *model.AppError {
	if f.writeFault == "before" {
		return unavailable()
	}
	f.data[key] = append([]byte{}, data...)
	if f.writeFault == "after" {
		return unavailable()
	}
	return nil
}
func workspaceFixture(t *testing.T) (*Plugin, *workspaceFake, *model.User, *int64, string) {
	p, base, user := operatorFixture(t)
	f := &workspaceFake{operatorFake: base, data: map[string][]byte{}}
	p.API = f
	now := int64(10000)
	p.repairClock = func() time.Time { return time.UnixMilli(now) }
	channel := channelConversation(t, p, "human-workspace", "claude:b")
	operatorWant(t, p, user.Id, "GET", "workspace", nil, 200)
	return p, f, user, &now, channel
}
func workspaceRevision(t *testing.T, p *Plugin, user string) float64 {
	return operatorWant(t, p, user, "GET", "workspace", nil, 200)["revision"].(float64)
}
func workspacePost(f *workspaceFake, channel, user, root, text string, at int64) *model.Post {
	post := &model.Post{Id: model.NewId(), ChannelId: channel, UserId: user, RootId: root, Message: text, CreateAt: at, UpdateAt: at}
	f.posts[post.Id] = post
	return post
}
func workspacePending(t *testing.T, p *Plugin, user string) []any {
	return operatorWant(t, p, user, "GET", "workspace/inbox", nil, 200)["items"].([]any)
}
func TestWorkspaceQuietAdmissionStopRepairUndoAndRestart(t *testing.T) {
	p, f, user, now, channel := workspaceFixture(t)
	actor := p.state.Agents["claude:b"].UserID
	old := workspacePost(f, channel, actor, "", "old @david", 9999)
	oldOwn := workspacePost(f, channel, user.Id, "", "historical human root", 9998)
	fresh := workspacePost(f, channel, actor, oldOwn.Id, "fresh answer @david", 10001)
	p.observe([]*model.Post{old, oldOwn, fresh})
	p.observeWorkspacePosts([]*model.Post{old, oldOwn, fresh})
	items := workspacePending(t, p, user.Id)
	if len(items) != 1 {
		t.Fatal("quiet baseline or rootreply admission", items)
	}
	item := items[0].(map[string]any)
	if !reflect.DeepEqual(item["reasons"], []any{"mention", "reply", "participated"}) {
		t.Fatal(item)
	}
	stateBefore, _ := json.Marshal(p.state)
	// Reads do not handle. Several reasons remain one canonical message.
	p.observeWorkspacePosts([]*model.Post{fresh, fresh})
	if len(workspacePending(t, p, user.Id)) != 1 {
		t.Fatal("duplicate admission")
	}
	*now = 10002
	revision := workspaceRevision(t, p, user.Id)
	operatorWant(t, p, user.Id, "PUT", "workspace/follows/"+oldOwn.Id, map[string]any{"revision": revision, "enabled": false}, 200)
	stopped := workspacePost(f, channel, actor, oldOwn.Id, "routine suppressed", 10003)
	mentioned := workspacePost(f, channel, actor, oldOwn.Id, "explicit @david survives stop", 10004)
	*now = 10005
	operatorWant(t, p, user.Id, "PUT", "workspace/follows/"+oldOwn.Id, map[string]any{"revision": workspaceRevision(t, p, user.Id), "enabled": true}, 200)
	resumed := workspacePost(f, channel, actor, oldOwn.Id, "future routine", 10006)
	// Simulate missed hints: all arrive at canonical repair after resume.
	p.observeWorkspacePosts([]*model.Post{stopped, mentioned, resumed})
	items = workspacePending(t, p, user.Id)
	if len(items) != 3 {
		t.Fatal("stop interval replayed backlog or lost mention", items)
	}
	if !reflect.DeepEqual(items[1].(map[string]any)["reasons"], []any{"mention"}) {
		t.Fatal("stopped routine reason was added", items)
	}
	// A later edit adds a mention to an ineligible original; no new attention.
	edited := workspacePost(f, channel, actor, "", "no mention originally", 10007)
	edited.Message = "edited @david"
	edited.UpdateAt = 10008
	edited.EditAt = 10008
	p.observeWorkspacePosts([]*model.Post{edited})
	if len(workspacePending(t, p, user.Id)) != 3 {
		t.Fatal("edit created attention")
	}
	handled := operatorWant(t, p, user.Id, "POST", "workspace/inbox/"+fresh.Id+"/handle", map[string]any{"revision": item["revision"]}, 200)
	later := workspacePost(f, channel, actor, oldOwn.Id, "later arrival", 10009)
	p.observeWorkspacePosts([]*model.Post{later})
	operatorWant(t, p, user.Id, "POST", "workspace/inbox/"+fresh.Id+"/undo", map[string]any{"token": handled["undo_token"]}, 200)
	if len(workspacePending(t, p, user.Id)) != 4 {
		t.Fatal("Undo lost later arrival")
	}
	operatorWant(t, p, user.Id, "POST", "workspace/inbox/"+fresh.Id+"/undo", map[string]any{"token": handled["undo_token"]}, 409)
	operatorWant(t, p, user.Id, "POST", "workspace/inbox/"+fresh.Id+"/handle", map[string]any{"revision": item["revision"]}, 409)
	// Reload index and state in a fresh plugin memory image; repairs preserve
	// captured reasons and stopped intervals, not just the latest toggle.
	restored := *p
	restored.workspaceReady = false
	restored.workspaceUsers = nil
	restored.workspaceErrors = nil
	operatorWant(t, &restored, user.Id, "GET", "workspace", nil, 200)
	restored.observeWorkspacePosts([]*model.Post{old, stopped, mentioned, fresh, later, resumed})
	if len(workspacePending(t, &restored, user.Id)) != 4 {
		t.Fatal("restart/repair created backlog")
	}
	var baseline State
	if err := json.Unmarshal(stateBefore, &baseline); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(baseline.Agents, p.state.Agents) || !reflect.DeepEqual(baseline.Conversations, p.state.Conversations) {
		t.Fatal("human actions mutated agent identities/grants")
	}
}

func TestWorkspaceOwnDMAndIndependentHumans(t *testing.T) {
	p, f, user, _, channel := workspaceFixture(t)
	peer := p.state.Agents["claude:b"].UserID
	ownDM := model.NewId()
	otherDM := model.NewId()
	for id, pair := range map[string]string{ownDM: user.Id + "__" + peer, otherDM: model.NewId() + "__" + peer} {
		p.state.Conversations[id] = &Conversation{ID: id, Kind: "dm", Grants: map[string]Grant{}}
		f.channels[id] = &model.Channel{Id: id, Name: pair, Type: model.ChannelTypeDirect}
	}
	own := workspacePost(f, ownDM, peer, "", "fresh direct root", 10001)
	observed := workspacePost(f, otherDM, peer, "", "quiet observed DM", 10002)
	historical := workspacePost(f, ownDM, peer, "", "quiet adopted history", 10003)
	p.state.Observed[historical.Id] = Observation{Historical: true}
	p.observeWorkspacePosts([]*model.Post{own, observed, historical})
	items := workspacePending(t, p, user.Id)
	if len(items) != 1 || items[0].(map[string]any)["post_id"] != own.Id || !reflect.DeepEqual(items[0].(map[string]any)["reasons"], []any{"direct"}) {
		t.Fatal(items)
	}
	other := &model.User{Id: model.NewId(), Username: "second", Roles: "system_user system_admin"}
	f.users[other.Username] = other
	second := operatorWant(t, p, other.Id, "GET", "workspace", nil, 200)
	if len(workspacePending(t, p, other.Id)) != 0 {
		t.Fatal("another human inherited Inbox")
	}
	operatorWant(t, p, other.Id, "POST", "workspace/inbox/"+own.Id+"/handle", map[string]any{"revision": 1}, 404)
	rev := workspaceRevision(t, p, user.Id)
	operatorWant(t, p, user.Id, "PUT", "workspace/favorites/"+channel, map[string]any{"revision": rev, "enabled": true}, 200)
	operatorWant(t, p, user.Id, "PUT", "workspace/favorites/"+channel, map[string]any{"revision": rev, "enabled": false}, 409)
	if len(second["favorites"].([]any)) != 0 {
		t.Fatal("favorites crossed human identities")
	}
}

func TestWorkspaceSaveIntentAndPersistenceFailureRecovery(t *testing.T) {
	p, f, user, _, channel := workspaceFixture(t)
	post := workspacePost(f, channel, p.state.Agents["claude:b"].UserID, "", "save exact post", 10001)
	revision := workspaceRevision(t, p, user.Id)
	f.preferenceWriteFault = "before"
	req := map[string]any{"revision": revision, "saved": true, "note": "private note"}
	operatorWant(t, p, user.Id, "PUT", "workspace/saved/"+post.Id, req, 502)
	pending := operatorWant(t, p, user.Id, "GET", "workspace", nil, 200)
	if pending["pending_saved"] == nil {
		t.Fatal("lost frozen save intent")
	}
	operatorWant(t, p, user.Id, "PUT", "workspace/favorites/"+channel, map[string]any{"revision": pending["revision"], "enabled": true}, 409)
	f.preferenceWriteFault = "after"
	req["revision"] = pending["revision"]
	operatorWant(t, p, user.Id, "PUT", "workspace/saved/"+post.Id, req, 502)
	f.preferenceWriteFault = ""
	recovered := operatorWant(t, p, user.Id, "GET", "workspace", nil, 200)
	if recovered["pending_saved"] != nil {
		t.Fatal("canonical preference did not resolve save")
	}
	saved := operatorWant(t, p, user.Id, "GET", "workspace/saved", nil, 200)["items"].([]any)
	if len(saved) != 1 || saved[0].(map[string]any)["note"] != "private note" {
		t.Fatal(saved)
	}
	f.posts[post.Id].DeleteAt = 1
	saved = operatorWant(t, p, user.Id, "GET", "workspace/saved", nil, 200)["items"].([]any)
	if saved[0].(map[string]any)["unavailable"] != true {
		t.Fatal("deleted saved post not explicit")
	}
	// Failed KV write commits nothing and blocks dispatch to native preferences.
	f.writeFault = "before"
	operatorWant(t, p, user.Id, "PUT", "workspace/saved/"+post.Id, map[string]any{"revision": recovered["revision"], "saved": false}, 502)
	if len(f.preferences[user.Id]) != 1 {
		t.Fatal("native unsave ran before durable intent")
	}
	f.writeFault = ""
	f.readFault = true
	operatorWant(t, p, user.Id, "PUT", "workspace/favorites/"+channel, map[string]any{"revision": recovered["revision"], "enabled": true}, 502)
	f.readFault = false
	// Lost event write response after commit does not duplicate on canonical repair.
	fresh := workspacePost(f, channel, p.state.Agents["claude:b"].UserID, "", "@david new", 10002)
	f.writeFault = "after"
	p.observeWorkspacePosts([]*model.Post{fresh})
	operatorWant(t, p, user.Id, "GET", "workspace/inbox", nil, 503)
	f.writeFault = ""
	p.reconcile()
	if len(workspacePending(t, p, user.Id)) != 1 {
		t.Fatal("uncertain persistence duplicated/lost admission")
	}
}

func TestWorkspaceInboxSearchRunsBeforePaging(t *testing.T) {
	p, f, user, _, channel := workspaceFixture(t)
	posts := []*model.Post{}
	for i := 0; i < 60; i++ {
		text := "@david common"
		if i == 0 {
			text = "@david oldest unique answer"
		}
		posts = append(posts, workspacePost(f, channel, p.state.Agents["claude:b"].UserID, "", text, 10001+int64(i)))
	}
	p.observeWorkspacePosts(posts)
	page := operatorWant(t, p, user.Id, "GET", "workspace/inbox?limit=10", nil, 200)
	if page["total"] != float64(60) || len(page["items"].([]any)) != 10 || page["next_before"] == nil {
		t.Fatal(page)
	}
	found := operatorWant(t, p, user.Id, "GET", "workspace/inbox?limit=10&q="+url.QueryEscape("UNIQUE"), nil, 200)
	if found["matched_total"] != float64(1) || found["items"].([]any)[0].(map[string]any)["post_id"] != posts[0].Id {
		t.Fatal("searched loaded page only", found)
	}
	// All items remain pending after reading/filtering.
	if operatorWant(t, p, user.Id, "GET", "workspace/inbox", nil, 200)["total"] != float64(60) {
		t.Fatal("viewing handled Inbox")
	}
}

func TestWorkspaceSameMillisecondFollowStopResume(t *testing.T) {
	p, f, user, now, channel := workspaceFixture(t)
	actor := p.state.Agents["claude:b"].UserID
	root := workspacePost(f, channel, actor, "", "remote root", 9990)
	beforeFollow := workspacePost(f, channel, actor, root.Id, "before follow", 10001)
	*now = 10001
	operatorWant(t, p, user.Id, "PUT", "workspace/follows/"+root.Id, map[string]any{"revision": workspaceRevision(t, p, user.Id), "enabled": true}, 200)
	afterFollow := workspacePost(f, channel, actor, root.Id, "after follow", 10001)
	beforeStop := workspacePost(f, channel, actor, root.Id, "before stop", 10002)
	*now = 10002
	operatorWant(t, p, user.Id, "PUT", "workspace/follows/"+root.Id, map[string]any{"revision": workspaceRevision(t, p, user.Id), "enabled": false}, 200)
	afterStop := workspacePost(f, channel, actor, root.Id, "after stop", 10002)
	beforeResume := workspacePost(f, channel, actor, root.Id, "before resume", 10003)
	*now = 10003
	operatorWant(t, p, user.Id, "PUT", "workspace/follows/"+root.Id, map[string]any{"revision": workspaceRevision(t, p, user.Id), "enabled": true}, 200)
	afterResume := workspacePost(f, channel, actor, root.Id, "after resume", 10003)
	p.observeWorkspacePosts([]*model.Post{beforeFollow, afterFollow, beforeStop, afterStop, beforeResume, afterResume})
	pending := workspacePending(t, p, user.Id)
	ids := map[string]bool{}
	for _, entry := range pending {
		ids[entry.(map[string]any)["post_id"].(string)] = true
	}
	if !reflect.DeepEqual(ids, map[string]bool{afterFollow.Id: true, beforeStop.Id: true, afterResume.Id: true}) {
		t.Fatal("equal-millisecond action boundary was lost", ids)
	}
}

func TestPostgresWorkspaceOriginalMentionsRecoverAfterFailureEditAndRestart(t *testing.T) {
	_, db := metadataDatabase(t)
	_, err := db.Exec(`CREATE TABLE Posts (Id text PRIMARY KEY,ChannelId text NOT NULL,UserId text NOT NULL,Message text NOT NULL,
 RootId text NOT NULL,CreateAt bigint NOT NULL,UpdateAt bigint NOT NULL,DeleteAt bigint NOT NULL,Type text NOT NULL,Props jsonb,
 OriginalId text NOT NULL DEFAULT '',EditAt bigint NOT NULL DEFAULT 0,FileIds varchar(300))`)
	if err != nil {
		t.Fatal(err)
	}
	p, f, user, _, channel := workspaceFixture(t)
	p.history = &postgresHistory{db: db}
	actor := p.state.Agents["claude:b"].UserID
	insert := func(post *model.Post) {
		t.Helper()
		_, err := db.Exec(`INSERT INTO Posts VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,'{}',$10,$11,NULL)`, post.Id, post.ChannelId, post.UserId, post.Message, post.RootId, post.CreateAt, post.UpdateAt, post.DeleteAt, post.Type, post.OriginalId, post.EditAt)
		if err != nil {
			t.Fatal(err)
		}
	}
	edit := func(post *model.Post, newText string, at int64, archive bool) {
		t.Helper()
		old := post.Clone()
		if archive {
			old.OriginalId = post.Id
			old.Id = model.NewId()
			old.DeleteAt = at
			old.UpdateAt = at
			insert(old)
		}
		post.Message = newText
		post.UpdateAt = at
		post.EditAt = at
		_, err := db.Exec(`UPDATE Posts SET Message=$2,UpdateAt=$3,EditAt=$3 WHERE Id=$1`, post.Id, newText, at)
		if err != nil {
			t.Fatal(err)
		}
	}
	lost := workspacePost(f, channel, actor, "", "original @david request", 10001)
	insert(lost)
	f.writeFault = "before"
	p.observeWorkspacePosts([]*model.Post{lost})
	operatorWant(t, p, user.Id, "GET", "workspace/inbox", nil, 503)
	f.writeFault = ""
	edit(lost, "removed mention after failed capture", 10002, true)
	missed := workspacePost(f, channel, actor, "", "@david missed callback", 10003)
	insert(missed)
	edit(missed, "also removed", 10004, true)
	notMentioned := workspacePost(f, channel, actor, "", "original has no mention", 10005)
	insert(notMentioned)
	edit(notMentioned, "later added @david", 10006, true)
	// Reloading a recovered index forces canonical catch-up before complete:true.
	restored := *p
	restored.workspaceReady = false
	restored.workspaceUsers = nil
	restored.workspaceErrors = nil
	operatorWant(t, &restored, user.Id, "GET", "workspace", nil, 200)
	result := operatorWant(t, &restored, user.Id, "GET", "workspace/inbox", nil, 200)
	if result["complete"] != true || len(result["items"].([]any)) != 2 {
		t.Fatal("creation mention evidence not recovered", result)
	}
	for _, entry := range result["items"].([]any) {
		if entry.(map[string]any)["post_id"] == notMentioned.Id {
			t.Fatal("edit manufactured a mention")
		}
	}
	// A genuinely missing first version must stay explicitly incomplete, even
	// after restart; a later archive repair can restore exact eligibility.
	gap := workspacePost(f, channel, actor, "", "missing original @david", 10007)
	insert(gap)
	edit(gap, "no original row yet", 10008, false)
	restored.reconcile()
	result = operatorWant(t, &restored, user.Id, "GET", "workspace/inbox?q=no-match", nil, 200)
	if result["complete"] != false || result["coverage_warning"] == "" {
		t.Fatal("unreconstructable history was reported complete", result)
	}
	old := gap.Clone()
	old.Id = model.NewId()
	old.OriginalId = gap.Id
	old.EditAt = 0
	old.DeleteAt = 10008
	old.Message = "missing original @david"
	insert(old)
	restored.reconcile()
	result = operatorWant(t, &restored, user.Id, "GET", "workspace/inbox", nil, 200)
	if result["complete"] != true || len(result["items"].([]any)) != 3 {
		t.Fatal("repaired original did not recover gap", result)
	}
	// Participation reads current ordinary author evidence; deleting that last
	// contribution removes the derived reason, not existing Inbox admissions.
	human := workspacePost(f, channel, user.Id, "", "human root", 10009)
	insert(human)
	reply := workspacePost(f, channel, actor, human.Id, "answer", 10010)
	insert(reply)
	yes, err := p.history.(*postgresHistory).HumanParticipated(user.Id, reply)
	if err != nil || !yes {
		t.Fatal(yes, err)
	}
	_, err = db.Exec("UPDATE Posts SET DeleteAt=1 WHERE Id=$1", human.Id)
	if err != nil {
		t.Fatal(err)
	}
	yes, err = p.history.(*postgresHistory).HumanParticipated(user.Id, reply)
	if err != nil || yes {
		t.Fatal(yes, err)
	}
}

func TestWorkspaceInitialSameMillisecondAndIndexRecovery(t *testing.T) {
	p, base, user := operatorFixture(t)
	f := &workspaceFake{operatorFake: base, data: map[string][]byte{}}
	p.API = f
	now := int64(20000)
	p.repairClock = func() time.Time { return time.UnixMilli(now) }
	channel := channelConversation(t, p, "initial-boundary", "claude:b")
	actor := p.state.Agents["claude:b"].UserID
	before := workspacePost(f, channel, actor, "", "@david before adoption", now)
	operatorWant(t, p, user.Id, "GET", "workspace", nil, 200)
	after := workspacePost(f, channel, actor, "", "@david after adoption", now)
	p.observeWorkspacePosts([]*model.Post{before, after})
	items := workspacePending(t, p, user.Id)
	if len(items) != 1 || items[0].(map[string]any)["post_id"] != after.Id {
		t.Fatal("equal-ms initial adoption dropped new message or admitted old one", items)
	}
	// A failed startup index read disables capture. After recovery, the first
	// workspace read must run canonical repair before reporting a complete Inbox.
	restored := *p
	restored.workspaceReady = false
	restored.workspaceUsers = nil
	restored.workspaceErrors = nil
	f.readFault = true
	if err := restored.loadWorkspaceUsers(); err == nil {
		t.Fatal("index load failure not exercised")
	}
	now++
	missed := workspacePost(f, channel, actor, "", "@david during failed index load", now)
	restored.observeWorkspacePosts([]*model.Post{missed})
	f.readFault = false
	result := operatorWant(t, &restored, user.Id, "GET", "workspace/inbox", nil, 200)
	if result["complete"] != true || len(result["items"].([]any)) != 2 {
		t.Fatal("index recovery exposed an incomplete queue", result)
	}
}
