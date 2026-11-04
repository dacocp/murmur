package main

import (
	"bytes"
	"encoding/json"
	"net/http/httptest"
	"testing"

	"github.com/mattermost/mattermost/server/public/model"
)

func (f *fakeAPI) GetPreferencesForUser(userID string) ([]model.Preference, *model.AppError) {
	if f.preferenceReadFault {
		return nil, unavailable()
	}
	preferences := []model.Preference{}
	for _, pref := range f.preferences[userID] {
		preferences = append(preferences, pref)
	}
	return preferences, nil
}

func (f *fakeAPI) UpdatePreferencesForUser(userID string, preferences []model.Preference) *model.AppError {
	if f.preferenceWriteFault == "before" {
		return unavailable()
	}
	if f.preferences[userID] == nil {
		f.preferences[userID] = map[string]model.Preference{}
	}
	for _, pref := range preferences {
		f.preferences[userID][pref.Category+"|"+pref.Name] = pref
	}
	if f.preferenceWriteFault == "after" {
		return unavailable()
	}
	return nil
}

func (f *fakeAPI) DeletePreferencesForUser(userID string, preferences []model.Preference) *model.AppError {
	if f.preferenceWriteFault == "before" {
		return unavailable()
	}
	for _, pref := range preferences {
		delete(f.preferences[userID], pref.Category+"|"+pref.Name)
	}
	if f.preferenceWriteFault == "after" {
		return unavailable()
	}
	return nil
}

func star(t *testing.T, p *Plugin, actor, id string, value bool) {
	t.Helper()
	result, err := p.setSaved(id, savedRequest{Actor: actor, Starred: &value})
	if err != nil {
		t.Fatal(err)
	}
	if result["post_id"] != id || result["starred"] != value {
		t.Fatalf("unexpected desired-state result: %#v", result)
	}
}

func savedItems(t *testing.T, p *Plugin, actor string) []map[string]any {
	t.Helper()
	result, err := p.saved(actor)
	if err != nil {
		t.Fatal(err)
	}
	return result["saved"].([]map[string]any)
}

func TestSavedPostsUseIndependentNativePreferencesWithoutMembershipOrFeedChanges(t *testing.T) {
	p, f := fixture(t)
	dmResult, err := p.conversation(conversationRequest{Actor: "codex:a", OpID: "saved-dm", Kind: "dm", Members: []string{"claude:b"}})
	if err != nil {
		t.Fatal(err)
	}
	dm := dmResult["conversation"].(map[string]any)["id"].(string)
	root := send(t, p, "codex:a", dm, "saved-root", "Historical parent message", "")
	reply := send(t, p, "claude:b", dm, "saved-reply", "Exact reply", root)
	star(t, p, "codex:a", root, true)
	mustRegister(t, p, "codex:child", "codex:a")
	if len(savedItems(t, p, "codex:child")) != 0 {
		t.Fatal("fork copied parent's saved posts")
	}
	before, _ := json.Marshal(p.state)
	child := p.state.Agents["codex:child"]
	// Native settings outside Saved Posts, false flags and other users' entries
	// must neither appear nor be replaced by this endpoint.
	f.UpdatePreferencesForUser(child.UserID, []model.Preference{
		{UserId: child.UserID, Category: "display_settings", Name: "use_military_time", Value: "true"},
		{UserId: child.UserID, Category: model.PreferenceCategoryFlaggedPost, Name: model.NewId(), Value: "false"},
		{UserId: p.state.Agents["codex:a"].UserID, Category: model.PreferenceCategoryFlaggedPost, Name: model.NewId(), Value: "true"},
	})
	star(t, p, "codex:child", reply, true)
	star(t, p, "codex:child", root, true)
	star(t, p, "codex:child", root, true)
	items := savedItems(t, p, "codex:child")
	if len(items) != 2 || items[0]["post_id"].(string) >= items[1]["post_id"].(string) {
		t.Fatalf("saved IDs not unique and sorted: %#v", items)
	}
	for _, item := range items {
		post := item["post"].(map[string]any)
		if post["id"] == reply && post["root_id"] != root {
			t.Fatal("saving reply substituted thread root")
		}
	}
	if f.members[dm][child.UserID] || p.state.Conversations[dm].Grants[child.ID].Role != "observer" {
		t.Fatal("saving promoted observer membership")
	}
	star(t, p, "codex:child", root, false)
	if len(savedItems(t, p, "codex:a")) != 1 || len(savedItems(t, p, "codex:child")) != 1 {
		t.Fatal("one user's desired state changed another user's bookmarks")
	}
	if f.preferences[child.UserID]["display_settings|use_military_time"].Value != "true" {
		t.Fatal("unrelated preference changed")
	}
	after, _ := json.Marshal(p.state)
	if !bytes.Equal(before, after) {
		t.Fatal("bookmark operation changed plugin state, feed or grant generation")
	}
	p = reload(t, p, f)
	if len(savedItems(t, p, "codex:child")) != 1 {
		t.Fatal("restart lost native saved preference")
	}
}

func TestSavedUnavailableRowsNeverExposeContentAndMayBeRemovedWithoutAccess(t *testing.T) {
	p, f := fixture(t)
	channel := legacyPrivateChannel(t, p, "saved-room", "claude:b")
	secret := send(t, p, "codex:a", channel, "saved-private", "Do not leak this private title or body", "")
	value := true
	_, err := p.setSaved(secret, savedRequest{Actor: "codex:x", Starred: &value})
	status(t, err, 403)
	if len(f.preferences[p.state.Agents["codex:x"].UserID]) != 0 {
		t.Fatal("unauthorized save wrote a preference")
	}
	star(t, p, "codex:a", secret, true)
	_, err = p.membership(channel, "leave", memberRequest{Actor: "codex:a", OpID: "saved-leave"})
	if err != nil {
		t.Fatal(err)
	}
	items := savedItems(t, p, "codex:a")
	if len(items) != 1 || len(items[0]) != 2 || items[0]["post_id"] != secret || items[0]["unavailable"] != true {
		t.Fatalf("unavailable row leaked metadata: %#v", items)
	}
	star(t, p, "codex:a", secret, false)
	star(t, p, "codex:a", secret, false)
	if len(savedItems(t, p, "codex:a")) != 0 {
		t.Fatal("inaccessible preference was not removable")
	}
	// A genuine not-found response is opaque too, including deleted messages.
	missingID := model.NewId()
	userID := p.state.Agents["claude:b"].UserID
	f.UpdatePreferencesForUser(userID, []model.Preference{{UserId: userID, Category: model.PreferenceCategoryFlaggedPost, Name: missingID, Value: "true"}})
	items = savedItems(t, p, "claude:b")
	if len(items) != 1 || len(items[0]) != 2 || items[0]["unavailable"] != true {
		t.Fatal("missing row was not opaque")
	}
}

func TestSavedDesiredStateConvergesAfterLostResponsesAndErrorsStayVisible(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "saved-faults", "claude:b")
	id := send(t, p, "codex:a", channel, "saved-message", "Own message", "")
	value := true
	f.preferenceWriteFault = "after"
	if _, err := p.setSaved(id, savedRequest{Actor: "codex:a", Starred: &value}); err == nil {
		t.Fatal("lost update response not injected")
	}
	f.preferenceWriteFault = ""
	star(t, p, "codex:a", id, true)
	if len(savedItems(t, p, "codex:a")) != 1 {
		t.Fatal("retry duplicated native preference")
	}
	f.preferenceReadFault = true
	if _, err := p.saved("codex:a"); err == nil {
		t.Fatal("preference failure became empty success")
	}
	f.preferenceReadFault = false
	f.getPostErrors[id] = unavailable()
	if _, err := p.saved("codex:a"); err == nil {
		t.Fatal("transient post failure became unavailable row")
	} else if failure, ok := err.(*httpError); ok && (failure.status == 403 || failure.status == 404) {
		t.Fatal("transient post failure became permission/not-found")
	}
	delete(f.getPostErrors, id)
	value = false
	f.preferenceWriteFault = "after"
	if _, err := p.setSaved(id, savedRequest{Actor: "codex:a", Starred: &value}); err == nil {
		t.Fatal("lost delete response not injected")
	}
	f.preferenceWriteFault = ""
	star(t, p, "codex:a", id, false)
	if len(savedItems(t, p, "codex:a")) != 0 {
		t.Fatal("delete desired state did not converge")
	}
}

func TestSavedRequestValidationRejectsMissingMalformedAndSystemPosts(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "saved-validation", "claude:b")
	id := send(t, p, "codex:a", channel, "saved-system", "Join event", "")
	f.posts[id].Type = "system_join_channel"
	value := true
	_, err := p.setSaved(id, savedRequest{Actor: "codex:a", Starred: &value})
	status(t, err, 400)
	for _, body := range []string{`{"actor":"codex:a"}`, `{"actor":"codex:a","starred":null}`, `{"actor":"codex:a","starred":"false"}`, `{"actor":"codex:a","starred":false,"user_id":"other"}`} {
		req := httptest.NewRequest("POST", "/v1/saved/"+id, bytes.NewBufferString(body))
		req.Header.Set("Authorization", "Bearer test-token")
		w := httptest.NewRecorder()
		p.ServeHTTP(nil, w, req)
		if w.Code != 400 {
			t.Fatalf("malformed request accepted: %s => %d", body, w.Code)
		}
	}
	_, err = p.setSaved("not-a-post-id", savedRequest{Actor: "codex:a", Starred: &value})
	status(t, err, 400)
	_, err = p.setSaved(id, savedRequest{Actor: "codex:unenrolled", Starred: &value})
	status(t, err, 403)
}
