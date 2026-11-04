package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/mattermost/mattermost/server/public/model"
)

type dmFake struct {
	*operatorFake
	directCalls  int
	beforeDirect func()
	afterDirect  func()
	beforeSave   func(*State) *model.AppError
	wrongPair    *model.Channel
	botError     *model.AppError
}

func (f *dmFake) GetBot(id string, includeDeleted bool) (*model.Bot, *model.AppError) {
	if f.botError != nil {
		return nil, f.botError
	}
	return f.fakeAPI.GetBot(id, includeDeleted)
}

func (f *dmFake) GetDirectChannel(a, b string) (*model.Channel, *model.AppError) {
	f.directCalls++
	if f.beforeDirect != nil {
		f.beforeDirect()
	}
	channel, err := f.fakeAPI.GetDirectChannel(a, b)
	if f.afterDirect != nil {
		f.afterDirect()
	}
	if f.wrongPair != nil {
		return f.wrongPair, nil
	}
	return channel, err
}

type dmSnapshot struct {
	base  canonicalHistory
	after func()
}

func (h *dmSnapshot) Posts(channel string) ([]*model.Post, error) {
	posts, err := h.base.Posts(channel)
	if err != nil {
		return nil, err
	}
	if h.after != nil {
		after := h.after
		h.after = nil
		after()
	}
	return posts, nil
}

func dmFixture(t *testing.T) (*Plugin, *dmFake, *model.User) {
	t.Helper()
	p, f, user := operatorFixture(t)
	wrapped := &dmFake{operatorFake: f}
	p.API = wrapped
	f.metadata.beforeSave = func(state *State) error {
		if wrapped.beforeSave != nil {
			if appErr := wrapped.beforeSave(state); appErr != nil {
				return appErr
			}
		}
		return nil
	}
	return p, wrapped, user
}

func reloadDM(t *testing.T, p *Plugin, f *dmFake) *Plugin {
	t.Helper()
	next := &Plugin{config: p.config}
	next.API, next.history = f, f.fakeAPI
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

func openDM(t *testing.T, p *Plugin, user, op, agent string) string {
	t.Helper()
	result := operatorWant(t, p, user, "POST", "dms", operatorDMRequest{OpID: op, AgentID: agent}, 200)
	conversation := result["conversation"].(map[string]any)
	if conversation["kind"] != "dm" || len(conversation) != 3 {
		t.Fatalf("unexpected opening response: %#v", result)
	}
	return conversation["id"].(string)
}

func TestOperatorDMAdoptionQuietExactIDsAndCoherentFeed(t *testing.T) {
	p, f, user := dmFixture(t)
	mustRegister(t, p, "codex:child", "codex:a")
	mustRegister(t, p, "claude:grandchild", "codex:child")
	other := channelConversation(t, p, "working-group", "claude:b")
	native, err := f.fakeAPI.GetDirectChannel(user.Id, p.state.Agents["codex:a"].UserID)
	if err != nil {
		t.Fatal(err)
	}
	old := []string{}
	for i := 0; i < 35; i++ {
		post, _ := f.CreatePost(&model.Post{ChannelId: native.Id, UserId: user.Id, Message: fmt.Sprint("old history ", i)})
		post.CreateAt = 100
		old = append(old, post.Id)
	}
	f.beforeDirect = func() {
		var disk State
		if err := json.Unmarshal(f.kv, &disk); err != nil {
			t.Fatal(err)
		}
		o := disk.Operations["open"]
		if o == nil || o.Kind != "operator_dm" || o.Actor != user.Id || o.BotUserID != p.state.Agents["codex:a"].UserID {
			t.Fatal("native creation preceded durable pinned intent")
		}
	}
	var fresh *model.Post
	p.history = &dmSnapshot{base: f.fakeAPI, after: func() {
		fresh, _ = f.CreatePost(&model.Post{ChannelId: native.Id, UserId: user.Id, Message: "new equal-time request", Props: model.StringInterface{"notification_eligible": false, "historical": true}})
		delete(f.posts, fresh.Id)
		fresh.Id = strings.Repeat("0", 26)
		fresh.CreateAt = 100
		f.posts[fresh.Id] = fresh
	}}
	channel := openDM(t, p, user.Id, "open", "codex:a")
	if channel != native.Id || f.creates != 36 || len(f.members[channel]) != 2 || len(p.state.Agents) != 5 {
		t.Fatal("adoption cloned history, sent text, changed pair or enrolled human")
	}
	if p.state.Conversations[channel].Grants["codex:a"].Role != "member" || p.state.Conversations[channel].Grants["codex:child"].Role != "observer" || p.state.Conversations[channel].Grants["claude:grandchild"].Role != "observer" {
		t.Fatal("normal transitive inheritance missing")
	}
	// Edits before the first recipient sync cannot turn historical IDs fresh.
	f.posts[old[0]].UpdateAt++
	f.posts[old[0]].Message = "edited historical content"
	p = reloadDM(t, p, f)
	path := "changes?attention_version=3&actor=codex:a&limit=7&generation=0"
	seen := map[string]bool{}
	for {
		page := call(t, p, path)
		if page["attention_version"] != float64(3) {
			t.Fatal("missing attention protocol echo")
		}
		convs := page["conversations"].([]any)
		if len(convs) != 2 {
			t.Fatalf("page did not carry complete authorized access: %#v", convs)
		}
		for _, value := range page["changes"].([]any) {
			entry := value.(map[string]any)
			post := entry["post"].(map[string]any)
			id := post["id"].(string)
			if seen[id] {
				t.Fatal("feed duplicated within one cursor walk")
			}
			seen[id] = true
			eligible, ok := entry["notification_eligible"].(bool)
			if !ok || eligible != (id == fresh.Id) {
				t.Fatalf("incorrect immutable attention hint for %s: %#v", id, entry)
			}
		}
		if page["more"] != true {
			break
		}
		path = fmt.Sprintf("changes?attention_version=3&actor=codex:a&limit=7&cursor=%.0f&generation=%.0f", page["cursor"], page["generation"])
	}
	if len(seen) != 36 || !seen[fresh.Id] {
		t.Fatalf("canonical snapshot boundary lost history/new lower-ID arrival: %d", len(seen))
	}
	// Reuse, even under a new operation ID, never rebases or regrants.
	before, _ := json.Marshal(p.state.Conversations)
	gen := p.state.Agents["codex:a"].Generation
	if openDM(t, p, user.Id, "open-again", "codex:a") != channel {
		t.Fatal("same pair split")
	}
	after, _ := json.Marshal(p.state.Conversations)
	if !bytes.Equal(before, after) || gen != p.state.Agents["codex:a"].Generation || p.state.Observed[fresh.Id].Historical {
		t.Fatal("reuse changed grants or recaptured baseline")
	}
	mustRegister(t, p, "codex:later-child", "codex:a")
	if _, err := p.access("codex:later-child", channel, false); err != nil {
		t.Fatal(err)
	}
	if _, err := p.access("claude:b", channel, false); err == nil {
		t.Fatal("unrelated peer gained human DM access")
	}
	if _, err := p.access("codex:a", other, true); err != nil {
		t.Fatal("unrelated channel lost access", err)
	}
}

func TestOperatorDMProtocolGuardAndEmptyEnvelope(t *testing.T) {
	p, f, user := dmFixture(t)
	before := append([]byte{}, f.kv...)
	for _, suffix := range []string{"", "&attention_version=0", "&attention_version=1", "&attention_version=2", "&attention_version=true", "&attention_version=3&attention_version=3", "&attention_version="} {
		navigationStatus(t, p, "changes?actor=codex:a"+suffix, 400)
	}
	if !bytes.Equal(before, f.kv) {
		t.Fatal("invalid protocol triggered reconciliation/mutation")
	}
	page := call(t, p, "changes?actor=codex:a&attention_version=3")
	if page["attention_version"] != float64(3) || !reflect.DeepEqual(page["changes"], []any{}) || !reflect.DeepEqual(page["conversations"], []any{}) {
		t.Fatalf("invalid empty envelope: %#v", page)
	}
	for _, field := range []string{"user_id", "bot_user_id", "actor", "channel_id", "message", "causal"} {
		operatorWant(t, p, user.Id, "POST", "dms", map[string]any{"op_id": "injected", "agent_id": "codex:a", field: "anything"}, 400)
	}
	if f.directCalls != 0 || p.state.Operations["injected"] != nil {
		t.Fatal("rejected request created a pair or intent")
	}
	// Protocol rejection must happen before recovery can publish a queued grant.
	f.directFault = true
	operatorWant(t, p, user.Id, "POST", "dms", operatorDMRequest{OpID: "queued", AgentID: "codex:a"}, 502)
	before = append([]byte{}, f.kv...)
	navigationStatus(t, p, "changes?actor=codex:a", 400)
	if !bytes.Equal(before, f.kv) || f.directCalls != 1 {
		t.Fatal("unqualified feed request recovered an opening")
	}
	page = call(t, p, "changes?actor=codex:a&attention_version=3&generation=0")
	if page["generation"] != float64(1) || page["reset"] != true || len(page["conversations"].([]any)) != 1 {
		t.Fatal("feed snapshot/generation preceded reconciled grant publication")
	}
}

func TestOperatorDMLostPairResponseRestartAndStableCompletedRetry(t *testing.T) {
	p, f, user := dmFixture(t)
	f.directFault = true
	operatorWant(t, p, user.Id, "POST", "dms", operatorDMRequest{OpID: "lost", AgentID: "codex:a"}, 502)
	if p.state.Operations["lost"].ChannelID != "" || len(p.state.Conversations) != 0 || len(f.channels) != 1 {
		t.Fatal("response-loss fixture did not create an unpublished native pair")
	}
	p = reloadDM(t, p, f)
	result := operatorWant(t, p, user.Id, "GET", "operations/lost", nil, 200)
	channel := result["result"].(map[string]any)["channel_id"].(string)
	if result["status"] != "complete" || f.directCalls != 2 || len(f.channels) != 1 {
		t.Fatal("restart did not recover canonical pair")
	}
	beforeCalls := f.directCalls
	_, err := p.membership(channel, "leave", memberRequest{Actor: "codex:a", OpID: "leave"})
	if err != nil {
		t.Fatal(err)
	}
	delete(f.bots, p.state.Agents["codex:a"].UserID)
	before, _ := json.Marshal(p.state)
	if openDM(t, p, user.Id, "lost", "codex:a") != channel {
		t.Fatal("completed retry lost result")
	}
	after, _ := json.Marshal(p.state)
	if !bytes.Equal(before, after) || f.directCalls != beforeCalls {
		t.Fatal("completed retry revalidated target, regranted or repeated pair lookup")
	}
	operatorWant(t, p, user.Id, "POST", "dms", operatorDMRequest{OpID: "lost", AgentID: "claude:b"}, 409)
	operatorWant(t, p, user.Id, "GET", "operations/leave", nil, 403)
}

func TestOperatorDMPublicationSaveFailureRecapturesWithoutLeakingStub(t *testing.T) {
	p, f, user := dmFixture(t)
	f.beforeSave = func(s *State) *model.AppError {
		if o := s.Operations["publish"]; o != nil && o.Status == "complete" {
			return unavailable()
		}
		return nil
	}
	operatorWant(t, p, user.Id, "POST", "dms", operatorDMRequest{OpID: "publish", AgentID: "codex:a"}, 502)
	var disk State
	if err := json.Unmarshal(f.kv, &disk); err != nil {
		t.Fatal(err)
	}
	if len(disk.Conversations) != 0 || len(disk.Observed) != 0 || disk.Agents["codex:a"].Generation != 0 || disk.Operations["publish"].Status != "pending" {
		t.Fatal("failed atomic publication persisted stub/grants/baseline")
	}
	operatorWant(t, p, user.Id, "GET", "bootstrap", nil, 503)
	channel := disk.Operations["publish"].ChannelID
	late, _ := f.CreatePost(&model.Post{ChannelId: channel, UserId: user.Id, Message: "arrived before replacement publication"})
	f.beforeSave = nil
	p = reloadDM(t, p, f)
	if !p.state.Observed[late.Id].Historical || p.state.Conversations[channel].Grants["codex:a"].Role != "member" {
		t.Fatal("unpublished snapshot recapture did not establish one durable new boundary")
	}
	newPost, _ := f.CreatePost(&model.Post{ChannelId: channel, UserId: user.Id, Message: "after publication"})
	p = reloadDM(t, p, f)
	if p.state.Observed[newPost.Id].Historical || p.state.Operations["publish"].Status != "complete" {
		t.Fatal("restart rebased committed snapshot")
	}
}

func TestOperatorDMAuthorityConflictsFailOnlyTheirIntent(t *testing.T) {
	for _, change := range []string{"demote", "delete-human", "delete-bot", "wrong-owner", "mapping-drift"} {
		t.Run(change, func(t *testing.T) {
			p, f, user := dmFixture(t)
			channelConversation(t, p, "unrelated", "claude:b")
			op := operatorDMRequest{OpID: "held", AgentID: "codex:a"}
			// Revocation during the snapshot must be rechecked before grants.
			p.history = &dmSnapshot{base: f.fakeAPI, after: func() {
				switch change {
				case "demote":
					user.Roles = "system_user"
				case "delete-human":
					user.DeleteAt = 1
				case "delete-bot":
					f.bots[p.state.Agents["codex:a"].UserID].DeleteAt = 1
				case "wrong-owner":
					f.bots[p.state.Agents["codex:a"].UserID].OwnerId = "foreign"
				case "mapping-drift":
					p.state.Agents["codex:a"].UserID = p.state.Agents["claude:b"].UserID
				}
			}}
			operatorWant(t, p, user.Id, "POST", "dms", op, 409)
			intent := p.state.Operations["held"]
			if intent.Status != "failed" || p.state.Conversations[intent.ChannelID] != nil {
				t.Fatal("revoked intent published access or remained replayable")
			}
			p.reconcile()
			if p.lastErr != "" {
				t.Fatal("policy conflict poisoned unrelated reconciliation", p.lastErr)
			}
			call(t, p, "changes?actor=claude:b&attention_version=3")
			calls := f.directCalls
			user.Roles, user.DeleteAt = "system_user system_admin", 0
			result := operatorWant(t, p, user.Id, "GET", "operations/held", nil, 200)
			if result["status"] != "failed" || f.directCalls != calls {
				t.Fatal("terminal conflict replayed after authority restored")
			}
		})
	}
}

func TestOperatorDMRejectsManagedDriftAndOrdersPendingOpenBeforeLeave(t *testing.T) {
	for _, drift := range []string{"left", "missing", "observer", "third-member"} {
		t.Run(drift, func(t *testing.T) {
			p, f, user := dmFixture(t)
			channel := openDM(t, p, user.Id, "first", "codex:a")
			c := p.state.Conversations[channel]
			switch drift {
			case "left":
				p.membership(channel, "leave", memberRequest{Actor: "codex:a", OpID: "depart"})
			case "missing":
				delete(c.Grants, "codex:a")
			case "observer":
				g := c.Grants["codex:a"]
				g.Role = "observer"
				c.Grants["codex:a"] = g
			case "third-member":
				f.members[channel][p.state.Agents["claude:b"].UserID] = true
			}
			before, _ := json.Marshal(c)
			operatorWant(t, p, user.Id, "POST", "dms", operatorDMRequest{OpID: "no-repair", AgentID: "codex:a"}, 409)
			after, _ := json.Marshal(c)
			if !bytes.Equal(before, after) || p.state.Operations["no-repair"].Status != "failed" {
				t.Fatal("opening repaired access/membership")
			}
		})
	}
	for _, matching := range []bool{true, false} {
		t.Run(fmt.Sprint("ordering-", matching), func(t *testing.T) {
			p, f, user := dmFixture(t)
			channel := openDM(t, p, user.Id, "first", "codex:a")
			target := "codex:a"
			if !matching {
				target = "claude:b"
			}
			f.directFault = true
			operatorWant(t, p, user.Id, "POST", "dms", operatorDMRequest{OpID: "pending", AgentID: target}, 502)
			if p.state.Operations["pending"].ChannelID != "" {
				t.Fatal("fixture did not lose pair reference")
			}
			if matching {
				f.directFault = true
				_, err := p.membership(channel, "leave", memberRequest{Actor: "codex:a", OpID: "leave"})
				status(t, err, 409)
				if p.state.Operations["leave"] != nil {
					t.Fatal("leave admitted before older opening settled")
				}
			}
			_, err := p.membership(channel, "leave", memberRequest{Actor: "codex:a", OpID: "leave"})
			if err != nil {
				t.Fatal(err)
			}
			want := "pending"
			if matching {
				want = "complete"
			}
			if p.state.Operations["pending"].Status != want {
				t.Fatal("leave settled wrong pair or failed to settle matching blank reference")
			}
			p = reloadDM(t, p, f)
			if !p.state.Conversations[channel].Left["codex:a"] || p.state.Conversations[channel].Grants["codex:a"].Role != "" {
				t.Fatal("pending opening resurrected departed grant")
			}
		})
	}
}

func TestOperatorDMRecoveryRechecksAuthorityBeforeAnyNewNativeEffect(t *testing.T) {
	for _, change := range []string{"demote", "delete-bot", "mapping-drift"} {
		t.Run(change, func(t *testing.T) {
			p, f, user := dmFixture(t)
			f.directFault = true
			operatorWant(t, p, user.Id, "POST", "dms", operatorDMRequest{OpID: "old-intent", AgentID: "codex:a"}, 502)
			switch change {
			case "demote":
				user.Roles = "system_user"
			case "delete-bot":
				delete(f.bots, p.state.Agents["codex:a"].UserID)
			case "mapping-drift":
				p.state.Agents["codex:a"].UserID = p.state.Agents["claude:b"].UserID
			}
			p.reconcile()
			if p.lastErr != "" || f.directCalls != 1 || p.state.Operations["old-intent"].Status != "failed" || len(p.state.Conversations) != 0 {
				t.Fatal("revoked recovery retried native effect, published access or poisoned feed")
			}
		})
	}
}

func TestOperatorDMFreshIntentSettlesPriorPairAndRejectsWrongNativePair(t *testing.T) {
	p, f, user := dmFixture(t)
	f.directFault = true
	operatorWant(t, p, user.Id, "POST", "dms", operatorDMRequest{OpID: "older", AgentID: "codex:a"}, 502)
	f.directFault = true
	operatorWant(t, p, user.Id, "POST", "dms", operatorDMRequest{OpID: "newer", AgentID: "codex:a"}, 409)
	if p.state.Operations["newer"] != nil {
		t.Fatal("fresh intent admitted before accepted older pair settled")
	}
	channel := openDM(t, p, user.Id, "newer", "codex:a")
	if p.state.Operations["older"].Status != "complete" || len(f.channels) != 1 || p.state.Agents["codex:a"].Generation != 1 {
		t.Fatal("prior-pair settlement did not converge")
	}
	other := &model.User{Id: model.NewId(), Username: "other-admin", Roles: "system_admin"}
	f.users[other.Username] = other
	operatorWant(t, p, other.Id, "GET", "operations/newer", nil, 403)
	operatorWant(t, p, other.Id, "POST", "dms", operatorDMRequest{OpID: "newer", AgentID: "codex:a"}, 409)
	wrong, _ := f.fakeAPI.GetDirectChannel(user.Id, p.state.Agents["claude:b"].UserID)
	f.wrongPair = wrong
	operatorWant(t, p, user.Id, "POST", "dms", operatorDMRequest{OpID: "wrong-pair", AgentID: "codex:a"}, 409)
	if p.state.Operations["wrong-pair"].Status != "failed" || p.state.Conversations[wrong.Id] != nil || len(p.state.Conversations) != 1 || p.state.Conversations[channel] == nil {
		t.Fatal("unverified native pair adopted")
	}
}

func TestOperatorDMConcurrentClicksConvergeAndIntentSavePrecedesCreation(t *testing.T) {
	p, f, user := dmFixture(t)
	var wg sync.WaitGroup
	results := make(chan *httptest.ResponseRecorder, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			body, _ := json.Marshal(operatorDMRequest{OpID: fmt.Sprint("tab-", i), AgentID: "codex:a"})
			r := httptest.NewRequest(http.MethodPost, "/operator/v1/dms", bytes.NewReader(body))
			r.Header.Set("Content-Type", "application/json")
			r.Header.Set("Mattermost-User-Id", user.Id)
			w := httptest.NewRecorder()
			p.ServeHTTP(nil, w, r)
			results <- w
		}(i)
	}
	wg.Wait()
	close(results)
	ids := map[string]bool{}
	for w := range results {
		if w.Code != 200 {
			t.Fatal(w.Code, w.Body.String())
		}
		var result map[string]any
		json.Unmarshal(w.Body.Bytes(), &result)
		ids[result["conversation"].(map[string]any)["id"].(string)] = true
	}
	if len(ids) != 1 || len(p.state.Conversations) != 1 || len(f.channels) != 1 || p.state.Agents["codex:a"].Generation != 1 || f.creates != 0 {
		t.Fatal("concurrent opens split pair, regranted or sent text")
	}
	p, f, user = dmFixture(t)
	f.kvFault = true
	operatorWant(t, p, user.Id, "POST", "dms", operatorDMRequest{OpID: "failed-intent", AgentID: "codex:a"}, 502)
	if f.directCalls != 0 || len(f.channels) != 0 {
		t.Fatal("pair creation preceded durable intent")
	}
}

func TestOperatorDMTransientDependencyErrorsDoNotRetireAcceptedIntent(t *testing.T) {
	for _, code := range []int{408, 409, 429, 503} {
		t.Run(fmt.Sprint(code), func(t *testing.T) {
			p, f, user := dmFixture(t)
			f.directFault = true
			operatorWant(t, p, user.Id, "POST", "dms", operatorDMRequest{OpID: "retryable", AgentID: "codex:a"}, 502)
			f.botError = &model.AppError{Id: "transient", Message: "dependency temporarily unavailable", StatusCode: code}
			operatorWant(t, p, user.Id, "GET", "operations/retryable", nil, 502)
			if p.state.Operations["retryable"].Status != "pending" {
				t.Fatal("dependency uncertainty terminally retired accepted intent")
			}
			f.botError = nil
			p = reloadDM(t, p, f)
			if p.state.Operations["retryable"].Status != "complete" || len(f.channels) != 1 {
				t.Fatal("retryable intent did not recover same pair")
			}
		})
	}
}
