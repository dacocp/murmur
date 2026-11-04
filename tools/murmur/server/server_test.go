package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/mattermost/mattermost/server/public/model"
	"github.com/mattermost/mattermost/server/public/plugin"
)

// The fake models canonical Mattermost persistence separately from plugin KV.
// Faults commit on one side and lose the response on the other, as real process
// interruption can. Production handlers, ACLs and reconciliation run unchanged.
type fakeAPI struct {
	plugin.API
	kv                   []byte
	metadata             *memoryMetadataStore
	users                map[string]*model.User
	bots                 map[string]*model.Bot
	channels             map[string]*model.Channel
	members              map[string]map[string]bool
	posts                map[string]*model.Post
	team                 *model.Team
	clock                int64
	postFault            string
	addFault             bool
	addCalls             int
	addFaultAt           int
	directFault          bool
	deleteMemberFault    bool
	kvFault              bool
	kvWrites             int
	kvFaultAt            int
	creates              int
	preferences          map[string]map[string]model.Preference
	preferenceReadFault  bool
	preferenceWriteFault string
	getPostErrors        map[string]*model.AppError
	getBotFault          bool
	createBotFault       string
	patchBotFault        string
	patchBotCalls        int
	patchedBotID         string
	lastBotPatch         *model.BotPatch
	beforeCreateBot      func(*model.Bot)
	usernameLookupFault  bool
}

func newFake() *fakeAPI {
	f := &fakeAPI{users: map[string]*model.User{}, bots: map[string]*model.Bot{}, channels: map[string]*model.Channel{},
		members: map[string]map[string]bool{}, posts: map[string]*model.Post{}, clock: 1000,
		preferences: map[string]map[string]model.Preference{}, getPostErrors: map[string]*model.AppError{}}
	f.metadata = newMemoryMetadataStore(f)
	return f
}

// The explicit fake store models committed metadata independently of native
// messages. kv is only a serialized test snapshot for existing assertions.
type memoryMetadataStore struct {
	owner      *fakeAPI
	beforeSave func(*State) error
}

func newMemoryMetadataStore(owner *fakeAPI) *memoryMetadataStore {
	return &memoryMetadataStore{owner: owner}
}
func (s *memoryMetadataStore) Save(state *State) error {
	if s.beforeSave != nil {
		if err := s.beforeSave(state); err != nil {
			return err
		}
	}
	f := s.owner
	f.kvWrites++
	if f.kvFault || f.kvFaultAt == f.kvWrites {
		return unavailable()
	}
	data, err := json.Marshal(state)
	if err == nil {
		f.kv = append([]byte{}, data...)
	}
	return err
}
func (s *memoryMetadataStore) Load() (*State, error) {
	state := &State{}
	if len(s.owner.kv) > 0 {
		if err := json.Unmarshal(s.owner.kv, state); err != nil {
			return nil, err
		}
	}
	state.init()
	return state, nil
}
func (s *memoryMetadataStore) Close() error { return nil }

func missing() *model.AppError {
	return &model.AppError{Id: "missing", Message: "missing", StatusCode: 404}
}
func unavailable() *model.AppError {
	return &model.AppError{Id: "unavailable", Message: "injected response loss", StatusCode: 503}
}
func (f *fakeAPI) GetConfig() *model.Config {
	limit := int64(100 * 1024 * 1024)
	enabled, driver := true, "local"
	return &model.Config{FileSettings: model.FileSettings{MaxFileSize: &limit, EnableFileAttachments: &enabled, DriverName: &driver}}
}
func (f *fakeAPI) KVSet(_ string, data []byte) *model.AppError {
	panic("domain persistence must use the explicit metadata store")
}
func (f *fakeAPI) KVGet(_ string) ([]byte, *model.AppError) { return f.kv, nil }
func (f *fakeAPI) GetTeamByName(_ string) (*model.Team, *model.AppError) {
	if f.team == nil {
		return nil, missing()
	}
	return f.team, nil
}
func (f *fakeAPI) CreateTeam(team *model.Team) (*model.Team, *model.AppError) {
	team.Id = model.NewId()
	f.team = team
	return team, nil
}
func (f *fakeAPI) GetUserByUsername(name string) (*model.User, *model.AppError) {
	if f.usernameLookupFault {
		return nil, unavailable()
	}
	if u := f.users[name]; u != nil {
		return u, nil
	}
	return nil, missing()
}
func (f *fakeAPI) GetUser(id string) (*model.User, *model.AppError) {
	for _, u := range f.users {
		if u.Id == id {
			return u, nil
		}
	}
	return nil, missing()
}
func (f *fakeAPI) CreateBot(bot *model.Bot) (*model.Bot, *model.AppError) {
	if f.beforeCreateBot != nil {
		f.beforeCreateBot(bot)
	}
	if f.users[bot.Username] != nil {
		return nil, &model.AppError{StatusCode: 409, Message: "username exists"}
	}
	if f.createBotFault == "before" {
		return nil, unavailable()
	}
	bot.UserId = model.NewId()
	f.bots[bot.UserId] = bot
	f.users[bot.Username] = &model.User{Id: bot.UserId, Username: bot.Username, FirstName: bot.DisplayName, IsBot: true}
	if f.createBotFault == "after" {
		return nil, unavailable()
	}
	return bot, nil
}
func (f *fakeAPI) GetBot(id string, _ bool) (*model.Bot, *model.AppError) {
	if f.getBotFault {
		return nil, unavailable()
	}
	if b := f.bots[id]; b != nil {
		return b, nil
	}
	return nil, missing()
}
func (f *fakeAPI) PatchBot(id string, patch *model.BotPatch) (*model.Bot, *model.AppError) {
	f.patchBotCalls++
	f.patchedBotID, f.lastBotPatch = id, patch
	if f.patchBotFault == "before" {
		return nil, unavailable()
	}
	bot := f.bots[id]
	if bot == nil {
		return nil, missing()
	}
	if patch.DisplayName != nil {
		bot.DisplayName = *patch.DisplayName
		f.users[bot.Username].FirstName = *patch.DisplayName
	}
	if patch.Username != nil {
		user := f.users[bot.Username]
		delete(f.users, bot.Username)
		bot.Username = *patch.Username
		user.Username = bot.Username
		f.users[bot.Username] = user
	}
	if f.patchBotFault == "after" {
		return nil, unavailable()
	}
	return bot, nil
}
func (f *fakeAPI) CreateTeamMember(team, user string) (*model.TeamMember, *model.AppError) {
	return &model.TeamMember{TeamId: team, UserId: user}, nil
}
func (f *fakeAPI) CreateChannel(c *model.Channel) (*model.Channel, *model.AppError) {
	c.Id = model.NewId()
	f.channels[c.Id] = c
	f.members[c.Id] = map[string]bool{}
	return c, nil
}
func (f *fakeAPI) GetChannelByName(_ string, name string, _ bool) (*model.Channel, *model.AppError) {
	for _, c := range f.channels {
		if c.Name == name {
			return c, nil
		}
	}
	return nil, missing()
}
func (f *fakeAPI) GetChannel(id string) (*model.Channel, *model.AppError) {
	if c := f.channels[id]; c != nil {
		return c, nil
	}
	return nil, missing()
}
func (f *fakeAPI) GetDirectChannel(a, b string) (*model.Channel, *model.AppError) {
	ids := []string{a, b}
	sort.Strings(ids)
	name := strings.Join(ids, "__")
	for _, c := range f.channels {
		if c.Name == name {
			if f.directFault {
				f.directFault = false
				return nil, unavailable()
			}
			return c, nil
		}
	}
	c, _ := f.CreateChannel(&model.Channel{Name: name, Type: "D"})
	f.members[c.Id][a] = true
	f.members[c.Id][b] = true
	if f.directFault {
		f.directFault = false
		return nil, unavailable()
	}
	return c, nil
}
func (f *fakeAPI) AddChannelMember(channel, user string) (*model.ChannelMember, *model.AppError) {
	f.members[channel][user] = true
	f.addCalls++
	if f.addFault || f.addFaultAt == f.addCalls {
		f.addFault = false
		f.addFaultAt = 0
		return nil, unavailable()
	}
	return &model.ChannelMember{ChannelId: channel, UserId: user}, nil
}
func (f *fakeAPI) GetChannelMember(channel, user string) (*model.ChannelMember, *model.AppError) {
	if !f.members[channel][user] {
		return nil, missing()
	}
	return &model.ChannelMember{ChannelId: channel, UserId: user}, nil
}
func (f *fakeAPI) GetChannelMembers(channel string, page, perPage int) (model.ChannelMembers, *model.AppError) {
	if f.channels[channel] == nil {
		return nil, missing()
	}
	ids := []string{}
	for id := range f.members[channel] {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	start, end := page*perPage, (page+1)*perPage
	if start > len(ids) {
		start = len(ids)
	}
	if end > len(ids) {
		end = len(ids)
	}
	members := model.ChannelMembers{}
	for _, id := range ids[start:end] {
		members = append(members, model.ChannelMember{ChannelId: channel, UserId: id})
	}
	return members, nil
}
func (f *fakeAPI) DeleteChannelMember(channel, user string) *model.AppError {
	delete(f.members[channel], user)
	if f.deleteMemberFault {
		f.deleteMemberFault = false
		return unavailable()
	}
	return nil
}
func (f *fakeAPI) CreatePost(post *model.Post) (*model.Post, *model.AppError) {
	f.creates++
	if f.postFault == "before" {
		return nil, unavailable()
	}
	f.clock++
	post.Id = model.NewId()
	post.CreateAt = f.clock
	post.UpdateAt = f.clock
	f.posts[post.Id] = post
	if f.postFault == "after" {
		return nil, unavailable()
	}
	return post, nil
}
func (f *fakeAPI) GetPost(id string) (*model.Post, *model.AppError) {
	if err := f.getPostErrors[id]; err != nil {
		return nil, err
	}
	if post := f.posts[id]; post != nil {
		return post, nil
	}
	return nil, missing()
}
func (f *fakeAPI) Posts(channel string) ([]*model.Post, error) {
	posts := []*model.Post{}
	for _, post := range f.posts {
		if post.ChannelId == channel && post.DeleteAt == 0 {
			posts = append(posts, post)
		}
	}
	sort.Slice(posts, func(i, j int) bool {
		if posts[i].CreateAt == posts[j].CreateAt {
			return posts[i].Id < posts[j].Id
		}
		return posts[i].CreateAt < posts[j].CreateAt
	})
	return posts, nil
}

func fixture(t *testing.T) (*Plugin, *fakeAPI) {
	t.Helper()
	f := newFake()
	p := &Plugin{config: configuration{CoordinatorToken: "test-token"}}
	p.API = f
	p.metadata = f.metadata
	p.history = f
	tick := time.Unix(100, 0)
	p.repairClock = func() time.Time { tick = tick.Add(canonicalRepairInterval); return tick }
	p.state.init()
	for _, id := range []string{"codex:a", "claude:b", "codex:x"} {
		mustRegister(t, p, id, "")
	}
	return p, f
}
func mustRegister(t *testing.T, p *Plugin, id, parent string) {
	t.Helper()
	if _, err := p.register(id, id, parent); err != nil {
		t.Fatal(err)
	}
}
func channelConversation(t *testing.T, p *Plugin, op string, members ...string) string {
	t.Helper()
	response, err := p.conversation(conversationRequest{Actor: "codex:a", OpID: op, Kind: "channel", Name: "Build review", Members: members})
	if err != nil {
		t.Fatal(err)
	}
	return response["conversation"].(map[string]any)["id"].(string)
}

// Replay a creation intent from before public channels existed. No open field was
// persisted, so even current code must create and retain a private channel.
func legacyPrivateChannel(t *testing.T, p *Plugin, op string, members ...string) string {
	t.Helper()
	o, _, err := p.newOp("codex:a", op, "conversation", map[string]any{"legacy": op})
	if err != nil {
		t.Fatal(err)
	}
	o.Name, o.Stage, o.Members = "Legacy private", "group", append([]string{"codex:a"}, members...)
	if err = p.save(); err != nil {
		t.Fatal(err)
	}
	if err = p.finishConversation(o); err != nil {
		t.Fatal(err)
	}
	return o.ChannelID
}
func send(t *testing.T, p *Plugin, actor, channel, op, text, root string) string {
	t.Helper()
	response, err := p.send(postRequest{Actor: actor, ChannelID: channel, OpID: op, Message: text, RootID: root})
	if err != nil {
		t.Fatal(err)
	}
	return response["post"].(map[string]any)["id"].(string)
}
func status(t *testing.T, err error, want int) {
	t.Helper()
	actual, ok := err.(*httpError)
	if !ok || actual.status != want {
		t.Fatalf("want HTTP %d, got %v", want, err)
	}
}
func call(t *testing.T, p *Plugin, path string) map[string]any {
	t.Helper()
	req := httptest.NewRequest("GET", "/v1/"+path, nil)
	req.Header.Set("Authorization", "Bearer test-token")
	w := httptest.NewRecorder()
	p.ServeHTTP(nil, w, req)
	if w.Code != 200 {
		t.Fatalf("GET %s: %d %s", path, w.Code, w.Body)
	}
	var response map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	return response
}
func reload(t *testing.T, p *Plugin, f *fakeAPI) *Plugin {
	t.Helper()
	next := &Plugin{config: p.config}
	next.API = f
	next.history = f
	next.metadata = f.metadata
	state, err := next.metadata.Load()
	if err != nil {
		t.Fatal(err)
	}
	next.state = *state
	next.repairClock = p.repairClock
	next.reconcile()
	if next.lastErr != "" {
		t.Fatal(next.lastErr)
	}
	return next
}

func TestObserverInheritancePersistsAndParentMayEnrollLater(t *testing.T) {
	p, f := fixture(t)
	first := channelConversation(t, p, "first", "claude:b")
	mustRegister(t, p, "codex:child", "codex:a")
	mustRegister(t, p, "claude:grandchild", "codex:child")
	later := channelConversation(t, p, "later", "claude:b")
	for _, id := range []string{"codex:child", "claude:grandchild"} {
		for _, c := range []string{first, later} {
			if p.state.Conversations[c].Grants[id].Role != "observer" {
				t.Fatalf("%s missed inherited %s", id, c)
			}
		}
	}
	_, err := p.membership(first, "leave", memberRequest{Actor: "codex:a", OpID: "leave"})
	if err != nil {
		t.Fatal(err)
	}
	p = reload(t, p, f)
	if _, err := p.access("claude:grandchild", first, false); err != nil {
		t.Fatal("departure revoked durable descendant grant", err)
	}
	_, err = p.membership(first, "join", memberRequest{Actor: "codex:a", OpID: "leave"})
	status(t, err, 409)
	_, err = p.membership(first, "join", memberRequest{Actor: "codex:a", OpID: "rejoin"})
	if err != nil {
		t.Fatal("explicit rejoin with prior access denied", err)
	}
	_, err = p.membership(first, "join", memberRequest{Actor: "codex:x", OpID: "arbitrary-join"})
	if err != nil {
		t.Fatal("enrolled visitor could not freely join public channel", err)
	}
	_, err = p.membership(first, "join", memberRequest{Actor: "codex:child", OpID: "join"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := p.access("codex:child", first, true); err != nil {
		t.Fatal(err)
	}
	if p.state.Conversations[first].Grants["claude:grandchild"].Role != "observer" {
		t.Fatal("join enrolled child descendants as members")
	}
	mustRegister(t, p, "codex:orphan", "codex:unknown")
	if p.state.Agents["codex:unknown"] != nil {
		t.Fatal("ancestry discovery provisioned parent")
	}
	_, err = p.register("codex:unknown", "Parent", "codex:orphan")
	status(t, err, 400)
	mustRegister(t, p, "codex:unknown", "codex:a")
	if _, err := p.access("codex:orphan", later, false); err != nil {
		t.Fatal("late parent enrollment failed to propagate", err)
	}
}

func TestActualChannelAuthorizationAndFreshChildDM(t *testing.T) {
	p, _ := fixture(t)
	response, err := p.conversation(conversationRequest{Actor: "codex:a", OpID: "dm", Kind: "dm", Members: []string{"claude:b"}})
	if err != nil {
		t.Fatal(err)
	}
	dm := response["conversation"].(map[string]any)["id"].(string)
	root := send(t, p, "codex:a", dm, "root", "private parent conversation", "")
	mustRegister(t, p, "codex:child", "codex:a")
	if _, err := p.authorizedPost("codex:child", root); err != nil {
		t.Fatal(err)
	}
	_, err = p.send(postRequest{Actor: "codex:child", OpID: "observer-post", ChannelID: dm, Message: "forbidden"})
	status(t, err, 403)
	_, err = p.membership(dm, "join", memberRequest{Actor: "codex:child", OpID: "dm-join"})
	status(t, err, 403)
	_, err = p.authorizedPost("codex:x", root)
	status(t, err, 403)
	response, err = p.conversation(conversationRequest{Actor: "codex:child", OpID: "fresh", Kind: "dm", Members: []string{"claude:b"}})
	if err != nil {
		t.Fatal(err)
	}
	fresh := response["conversation"].(map[string]any)["id"].(string)
	if fresh == dm {
		t.Fatal("fork reused parent's native DM")
	}
	posts, _ := p.canonical(fresh)
	if len(posts) != 0 {
		t.Fatal("parent messages copied into child DM")
	}
	_, err = p.send(postRequest{Actor: "codex:child", OpID: "cross-root", ChannelID: fresh, RootID: root, Message: "wrong channel"})
	status(t, err, 400)
	reply := send(t, p, "codex:a", dm, "reply", "real threaded reply", root)
	_, err = p.send(postRequest{Actor: "codex:a", OpID: "nested", ChannelID: dm, RootID: reply, Message: "wrong root"})
	status(t, err, 400)
	thread := call(t, p, "threads/"+root+"?actor=codex:child")["posts"].([]any)
	if len(thread) != 2 {
		t.Fatalf("thread returned %d posts", len(thread))
	}
}

func TestCanonicalBackfillPaginationAndGrantGeneration(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "feed", "claude:b")
	for i := 0; i < 207; i++ {
		_, err := f.CreatePost(&model.Post{ChannelId: channel, UserId: p.state.Agents["codex:a"].UserID, Message: fmt.Sprint(i)})
		if err != nil {
			t.Fatal(err)
		}
	}
	f.CreatePost(&model.Post{ChannelId: channel, UserId: p.state.Agents["codex:a"].UserID, Message: "join system event", Type: "system_join_channel"})
	feed := call(t, p, "changes?attention_version=3&actor=claude:b&cursor=0&limit=1000")
	if len(feed["changes"].([]any)) != 207 {
		t.Fatal("missed canonical pages or included system event")
	}
	cursor := int64(feed["cursor"].(float64))
	generation := int64(feed["generation"].(float64))
	p = reload(t, p, f)
	feed = call(t, p, fmt.Sprintf("changes?attention_version=3&actor=claude:b&cursor=%d&generation=%d", cursor, generation))
	if len(feed["changes"].([]any)) != 0 {
		t.Fatal("restart duplicated unchanged observations")
	}
	mustRegister(t, p, "codex:child", "claude:b")
	feed = call(t, p, fmt.Sprintf("changes?attention_version=3&actor=codex:child&cursor=%d&generation=0&limit=1000", cursor))
	if feed["reset"] != true || len(feed["changes"].([]any)) != 0 || int64(feed["cursor"].(float64)) != p.state.Sequence {
		t.Fatal("inherited reference created feed arrivals or failed to advance")
	}
	page := call(t, p, "conversations/"+channel+"/posts?actor=claude:b&limit=20")
	if len(page["posts"].([]any)) != 20 || page["next_before"] == nil {
		t.Fatal("limited page failed")
	}
	older := call(t, p, "conversations/"+channel+"/posts?actor=claude:b&limit=1000&before="+page["next_before"].(string))
	if len(older["posts"].([]any)) != 187 {
		t.Fatal("before pagination lost or duplicated records")
	}
}

func TestLostResponsesAndJournalReplayDoNotBlindlyRepeat(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "faults", "claude:b")
	mustRegister(t, p, "codex:child", "codex:a")
	f.addFault = true
	_, err := p.membership(channel, "join", memberRequest{Actor: "codex:child", OpID: "join-interrupted"})
	if err == nil {
		t.Fatal("fault did not occur")
	}
	if !f.members[channel][p.state.Agents["codex:child"].UserID] {
		t.Fatal("native effect did not commit before response loss")
	}
	p = reload(t, p, f)
	if p.state.Operations["join-interrupted"].Status != "complete" || p.state.Conversations[channel].Grants["codex:child"].Role != "member" {
		t.Fatal("membership intent was not reconciled")
	}
	f.postFault = "after"
	first := send(t, p, "codex:a", channel, "lost-response", "ONE canonical message", "")
	second := send(t, p, "codex:a", channel, "lost-response", "ONE canonical message", "")
	if first != second || f.creates != 1 {
		t.Fatal("lost response retried native create")
	}
	_, err = p.send(postRequest{Actor: "codex:a", ChannelID: channel, OpID: "lost-response", Message: "different immutable body"})
	status(t, err, 409)
	f.postFault = "before"
	_, err = p.send(postRequest{Actor: "codex:a", ChannelID: channel, OpID: "uncertain", Message: "do not blindly repost"})
	status(t, err, 409)
	p = reload(t, p, f)
	_, err = p.send(postRequest{Actor: "codex:a", ChannelID: channel, OpID: "uncertain", Message: "do not blindly repost"})
	status(t, err, 409)
	if f.creates != 2 {
		t.Fatal("uncertain request was reposted on restart or retry")
	}
	if bytes.Contains(f.kv, []byte("ONE canonical message")) || bytes.Contains(f.kv, []byte("do not blindly repost")) {
		t.Fatal("plugin metadata retained message bodies")
	}
}

func TestAuthenticationAndPersistenceFailureFailClosed(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "secure", "claude:b")
	w := httptest.NewRecorder()
	p.ServeHTTP(nil, w, httptest.NewRequest("GET", "/v1/agents", nil))
	if w.Code != 401 {
		t.Fatal("unauthenticated request accepted")
	}
	f.kvFault = true
	_, err := p.send(postRequest{Actor: "codex:a", ChannelID: channel, OpID: "failed-journal", Message: "must not reach Mattermost"})
	if err == nil || f.creates != 0 {
		t.Fatal("native post executed before durable intent")
	}
	req := httptest.NewRequest("GET", "/v1/agents", nil)
	req.Header.Set("Authorization", "Bearer test-token")
	w = httptest.NewRecorder()
	p.ServeHTTP(nil, w, req)
	if w.Code != 503 {
		t.Fatal("state ambiguity did not close the API")
	}
}

func TestCompletedOperationRecoveryAfterDepartureReturnsOnlyOwnedMetadata(t *testing.T) {
	p, f := fixture(t)
	channel := legacyPrivateChannel(t, p, "create-room", "claude:b")
	f.postFault = "after"
	postID := send(t, p, "codex:a", channel, "lost-response", "private body must remain protected", "")
	_, err := p.membership(channel, "leave", memberRequest{Actor: "codex:a", OpID: "leave-room"})
	if err != nil {
		t.Fatal(err)
	}
	p = reload(t, p, f)
	response := call(t, p, "operations/lost-response?actor=codex:a")
	metadata := response["result"].(map[string]any)
	if response["status"] != "complete" || len(metadata) != 2 || metadata["post_id"] != postID || metadata["channel_id"] != channel {
		t.Fatalf("unexpected post recovery metadata: %#v", response)
	}
	room := call(t, p, "operations/create-room?actor=codex:a")["result"].(map[string]any)
	if len(room) != 1 || room["channel_id"] != channel {
		t.Fatalf("unexpected conversation recovery metadata: %#v", room)
	}
	_, err = p.authorizedPost("codex:a", postID)
	status(t, err, 403)
	req := httptest.NewRequest("GET", "/v1/operations/lost-response?actor=claude:b", nil)
	req.Header.Set("Authorization", "Bearer test-token")
	w := httptest.NewRecorder()
	p.ServeHTTP(nil, w, req)
	if w.Code != 403 {
		t.Fatal("another actor inspected an owned operation")
	}
}
