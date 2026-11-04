package main

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/mattermost/mattermost/server/public/model"
)

func TestReadableUsernamesAreValidCompactAndDistinct(t *testing.T) {
	for _, tt := range []struct{ label, base string }{
		{"Murmur vs Other options", "murmur-vs-other-options"},
		{"  Checkout___BUG / review!!", "checkout-bug-review"},
		{"123 migration", "agent-123-migration"},
		{"調査 🤖", "agent"},
		{strings.Repeat("Long ", 20), "long-long-long-long-lon"},
	} {
		t.Run(tt.label, func(t *testing.T) {
			username := readableUsername("codex:one", tt.label)
			if username != tt.base+"-"+digest("codex:one")[:8] || !model.IsValidUsername(username) || len(username) > 32 {
				t.Fatalf("invalid/unexpected readable username: %q", username)
			}
			if readableUsername("codex:two", tt.label) == username {
				t.Fatal("different native identities received the same username")
			}
		})
	}
}

func TestRegisterPersistsUsernameBeforeCreateAndRetainsItAcrossTitleChangeAndRestart(t *testing.T) {
	p, f := fixture(t)
	actor := "codex:readable"
	f.beforeCreateBot = func(bot *model.Bot) {
		var saved State
		if err := json.Unmarshal(f.kv, &saved); err != nil {
			t.Fatal(err)
		}
		intent := saved.Registrations[actor]
		if intent == nil || intent.Username != bot.Username || intent.Parent != "codex:a" || intent.UserID != "" || !intent.CreateAttempted {
			t.Fatal("native creation preceded durable actor/parent/username intent")
		}
	}
	f.createBotFault = "after"
	if _, err := p.register(actor, "First task", "codex:a"); err == nil {
		t.Fatal("lost native response not injected")
	}
	username := readableUsername(actor, "First task")
	userID := f.users[username].Id
	count := len(f.bots)
	f.beforeCreateBot, f.createBotFault = nil, ""
	p = reload(t, p, f)
	a, err := p.register(actor, "A completely different title", "codex:a")
	if err != nil {
		t.Fatal(err)
	}
	if a.UserID != userID || a.Username != username || a.Name != "A completely different title" || len(f.bots) != count {
		t.Fatal("retry created a different identity or changed initial username")
	}
	if len(p.state.Registrations) != 0 {
		t.Fatal("completed registration retained pending intent")
	}
	if _, err := p.register(actor, "Later title", "codex:a"); err != nil || a.Username != username {
		t.Fatalf("ordinary title change changed username: %v", err)
	}
}

func TestExplicitPrefixRenamesSameBotAndRecoversLostPatchResponse(t *testing.T) {
	p, f := fixture(t)
	actor := "codex:prefix"
	f.createBotFault = "after"
	if _, err := p.register(actor, "First title", "", "Checkout bug"); err == nil {
		t.Fatal("lost create response not injected")
	}
	oldUsername := readableUsername(actor, "Checkout bug")
	userID := f.users[oldUsername].Id
	f.createBotFault = ""
	p = reload(t, p, f)
	f.patchBotFault = "after"
	if _, err := p.register(actor, "Current title", "", "API review"); err == nil {
		t.Fatal("lost patch response not injected")
	}
	var saved State
	if err := json.Unmarshal(f.kv, &saved); err != nil {
		t.Fatal(err)
	}
	if saved.Registrations[actor].UserID != userID {
		t.Fatal("username rename happened before durable bot ID pin")
	}
	f.patchBotFault = ""
	p = reload(t, p, f)
	a, err := p.register(actor, "Current title", "", "API review")
	if err != nil {
		t.Fatal(err)
	}
	if a.UserID != userID || a.Username != readableUsername(actor, "API review") || f.users[oldUsername] != nil {
		t.Fatal("lost rename response did not converge on the same bot")
	}
	before, _ := json.Marshal(p.state)
	afterName := "New native title"
	_, err = p.register(actor, afterName, "")
	if err != nil || a.Username != readableUsername(actor, "API review") {
		t.Fatalf("ordinary title sync reset explicit handle: %v", err)
	}
	var expected State
	json.Unmarshal(before, &expected)
	expected.Agents[actor].Name = afterName
	expectedJSON, _ := json.Marshal(expected)
	actualJSON, _ := json.Marshal(p.state)
	if string(expectedJSON) != string(actualJSON) {
		t.Fatal("title sync changed identity, ancestry or membership")
	}
}

func TestRegistrationCollisionsRequireExactActorNotJustPluginOwnership(t *testing.T) {
	for _, tt := range []struct {
		label, owner, description string
		isBot                     bool
	}{
		{"human", "", "", false},
		{"foreign bot", "another-plugin", actorDescription("codex:collision"), true},
		{"other Murmur actor", "murmur", actorDescription("codex:other"), true},
		{"unmarked Murmur bot", "murmur", "", true},
	} {
		t.Run(tt.label, func(t *testing.T) {
			p, f := fixture(t)
			actor := "codex:collision"
			username := readableUsername(actor, "Checkout bug")
			id := model.NewId()
			f.users[username] = &model.User{Id: id, Username: username, IsBot: tt.isBot}
			f.bots[id] = &model.Bot{UserId: id, Username: username, OwnerId: tt.owner, Description: tt.description}
			_, err := p.register(actor, "Checkout bug", "")
			status(t, err, 409)
			if p.state.Agents[actor] != nil || f.patchBotCalls != 0 {
				t.Fatal("collision adopted or modified another account")
			}
			if p.state.Registrations[actor] != nil {
				t.Fatal("definitive pre-create collision froze an unusable initial name")
			}
			if a, err := p.register(actor, "Checkout bug", "", "Available task"); err != nil || a.UserID == id || f.users[username].Id != id {
				t.Fatalf("different prefix could not escape collision without modifying foreign account: %v", err)
			}
		})
	}
}

func TestUncertainCreateNeverReallocatesAnUnprovenCollision(t *testing.T) {
	p, f := fixture(t)
	actor := "codex:held"
	f.createBotFault = "after"
	if _, err := p.register(actor, "Original task", ""); err == nil {
		t.Fatal("lost create response not injected")
	}
	username := readableUsername(actor, "Original task")
	bot := f.bots[f.users[username].Id]
	bot.Description = actorDescription("codex:somebody-else")
	count := len(f.bots)
	f.createBotFault = ""
	p = reload(t, p, f)
	_, err := p.register(actor, "Changed title", "", "Different task")
	status(t, err, 409)
	if len(f.bots) != count || p.state.Registrations[actor] == nil || p.state.Agents[actor] != nil {
		t.Fatal("uncertain creation was replaced or its identity intent discarded")
	}
}

func TestUsernameIntentFailuresAndLookupErrorsDoNotCreateAccounts(t *testing.T) {
	p, f := fixture(t)
	count := len(f.bots)
	f.kvFault = true
	if _, err := p.register("codex:new", "First task", ""); err == nil || len(f.bots) != count {
		t.Fatal("failed durable intent created an account")
	}
	f.kvFault = false
	p = reload(t, p, f)
	f.usernameLookupFault = true
	if _, err := p.register("codex:new", "First task", ""); err == nil || len(f.bots) != count {
		t.Fatal("uncertain username lookup created an account")
	}
	f.usernameLookupFault = false
	if _, err := p.register("codex:new", "First task", "codex:a"); err == nil {
		t.Fatal("pending identity was reparented")
	}
}

func TestEachRegistrationSaveFailureRecoversWithoutDuplicateBots(t *testing.T) {
	for _, point := range []struct {
		name string
		save int
	}{
		{"initial intent", 1}, {"before create", 2}, {"bot ID pin", 3}, {"completed enrollment", 4},
	} {
		t.Run(point.name, func(t *testing.T) {
			p, f := fixture(t)
			actor, count := "codex:persistence", len(f.bots)
			f.kvFaultAt = f.kvWrites + point.save
			if result, err := p.register(actor, "Initial task", ""); err == nil || result != nil {
				t.Fatal("failed persistence claimed registration success")
			}
			if point.save <= 2 && len(f.bots) != count {
				t.Fatal("native creation preceded durable intent/attempt")
			}
			f.kvFaultAt = 0
			p = reload(t, p, f)
			a, err := p.register(actor, "Latest title", "", "Explicit task")
			if err != nil || a == nil || a.Username != readableUsername(actor, "Explicit task") || len(f.bots) != count+1 {
				t.Fatalf("restart did not converge on one bot: %v", err)
			}
		})
	}
}

func TestExistingLegacyHandleStaysUntilExplicitRename(t *testing.T) {
	p, f := fixture(t)
	a := p.state.Agents["codex:a"]
	bot := f.bots[a.UserID]
	user := f.users[bot.Username]
	delete(f.users, bot.Username)
	bot.Username, user.Username, bot.Description = "murmur-old-opaque", "murmur-old-opaque", ""
	f.users[bot.Username] = user
	a.Username = "" // Persisted older versions did not have this field.
	if _, err := p.register(a.ID, "Current title", ""); err != nil || a.Username != "murmur-old-opaque" {
		t.Fatalf("legacy account changed without explicit request: %v", err)
	}
	userID := a.UserID
	if _, err := p.register(a.ID, "Current title", "", "Build review"); err != nil || a.UserID != userID || a.Username != readableUsername(a.ID, "Build review") {
		t.Fatalf("explicit legacy rename did not preserve identity: %v", err)
	}
}

func TestRegisterRoutePrefixValidationAndReturnedUsername(t *testing.T) {
	p, _ := fixture(t)
	for _, body := range []string{
		`{"actor":"codex:bad","name":"Title","mode":"native","lineage":[{"actor":"codex:bad","parent":null}],"username_prefix":"  "}`,
		`{"actor":"codex:bad","name":"Title","mode":"native","lineage":[{"actor":"codex:bad","parent":null}],"username_prefix":false}`,
	} {
		r := httptest.NewRequest("POST", "/v1/register", strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer test-token")
		w := httptest.NewRecorder()
		p.ServeHTTP(nil, w, r)
		if w.Code != 400 || !strings.Contains(w.Body.String(), "username_prefix") {
			t.Fatalf("invalid prefix got %d: %s", w.Code, w.Body)
		}
	}
	r := httptest.NewRequest("POST", "/v1/register", strings.NewReader(`{"actor":"codex:route","name":"Native title","mode":"native","lineage":[{"actor":"codex:route","parent":null}],"username_prefix":"Task handle"}`))
	r.Header.Set("Authorization", "Bearer test-token")
	w := httptest.NewRecorder()
	p.ServeHTTP(nil, w, r)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"username":"task-handle-`) {
		t.Fatalf("route did not expose actual username: %d %s", w.Code, w.Body)
	}
}
