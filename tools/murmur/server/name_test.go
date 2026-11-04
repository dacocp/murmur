package main

import (
	"bytes"
	"encoding/json"
	"testing"
)

func TestRegisterSynchronizesBotDisplayNameWithoutChangingIdentityOrConversationState(t *testing.T) {
	p, f := fixture(t)
	channelConversation(t, p, "name-room", "claude:b")
	mustRegister(t, p, "codex:child", "codex:a")
	id := "codex:child"
	original := *p.state.Agents[id]
	before, _ := json.Marshal(p.state)
	var expected State
	if err := json.Unmarshal(before, &expected); err != nil {
		t.Fatal(err)
	}
	want := "Murmur vs Other options"
	expected.Agents[id].Name = want
	a, err := p.register(id, want, "codex:a")
	if err != nil {
		t.Fatal(err)
	}
	if a.ID != original.ID || a.UserID != original.UserID || a.Parent != original.Parent || a.RegisteredAt != original.RegisteredAt || a.Generation != original.Generation {
		t.Fatalf("rename changed session identity or grant generation: %#v", a)
	}
	after, _ := json.Marshal(p.state)
	expectedJSON, _ := json.Marshal(expected)
	if !bytes.Equal(after, expectedJSON) || !bytes.Equal(f.kv, expectedJSON) {
		t.Fatal("rename changed state beyond Name or did not persist it")
	}
	bot := f.bots[a.UserID]
	if bot.DisplayName != want || f.users[bot.Username].FirstName != want || f.patchBotCalls != 1 || f.patchedBotID != original.UserID {
		t.Fatal("native bot and corresponding user were not synchronized")
	}
	if f.lastBotPatch.DisplayName == nil || f.lastBotPatch.Username != nil || f.lastBotPatch.Description != nil {
		t.Fatal("rename patched fields besides display name")
	}
	if _, err := p.register(id, want, "codex:a"); err != nil {
		t.Fatal(err)
	}
	if f.patchBotCalls != 1 {
		t.Fatal("idempotent registration repeated native rename")
	}
	p = reload(t, p, f)
	if p.state.Agents[id].Name != want {
		t.Fatal("restart lost synchronized name")
	}
}

func TestRegisterRejectsParentAndOwnershipChangesBeforeRename(t *testing.T) {
	p, f := fixture(t)
	mustRegister(t, p, "codex:child", "codex:a")
	a := p.state.Agents["codex:child"]
	_, err := p.register(a.ID, "Unrelated parent title", "claude:b")
	status(t, err, 409)
	if f.patchBotCalls != 0 || a.Name != "codex:child" {
		t.Fatal("invalid parent changed name")
	}
	f.bots[a.UserID].OwnerId = "other-plugin"
	_, err = p.register(a.ID, "Wrong owner title", "codex:a")
	status(t, err, 409)
	if f.patchBotCalls != 0 || a.Name != "codex:child" {
		t.Fatal("unowned bot was renamed")
	}
}

func TestRegisterNameFailuresNeverClaimSuccessAndRetriesConverge(t *testing.T) {
	for _, fault := range []string{"before", "after"} {
		t.Run("native-response-"+fault, func(t *testing.T) {
			p, f := fixture(t)
			a := p.state.Agents["codex:a"]
			persisted := append([]byte{}, f.kv...)
			f.patchBotFault = fault
			if result, err := p.register(a.ID, "Current native title", ""); err == nil || result != nil {
				t.Fatal("native failure claimed success")
			}
			if a.Name != "codex:a" || !bytes.Equal(f.kv, persisted) {
				t.Fatal("native failure changed plugin name")
			}
			f.patchBotFault = ""
			if _, err := p.register(a.ID, "Current native title", ""); err != nil {
				t.Fatal(err)
			}
			wantCalls := 2
			if fault == "after" {
				wantCalls = 1
			}
			if f.patchBotCalls != wantCalls || a.Name != "Current native title" {
				t.Fatal("retry did not converge on already-renamed native account")
			}
		})
	}
	p, f := fixture(t)
	f.getBotFault = true
	if _, err := p.register("codex:a", "Current native title", ""); err == nil {
		t.Fatal("native lookup failure claimed success")
	}
	if p.state.Agents["codex:a"].Name != "codex:a" || f.patchBotCalls != 0 {
		t.Fatal("lookup failure changed name")
	}
	f.getBotFault = false
	f.kvFault = true
	if result, err := p.register("codex:a", "Current native title", ""); err == nil || result != nil {
		t.Fatal("failed persistence claimed success")
	}
	if p.state.Agents["codex:a"].Name != "codex:a" || f.bots[p.state.Agents["codex:a"].UserID].DisplayName != "Current native title" {
		t.Fatal("failed save was not restored locally after native success")
	}
	f.kvFault = false
	p = reload(t, p, f)
	if _, err := p.register("codex:a", "Current native title", ""); err != nil {
		t.Fatal(err)
	}
	if f.patchBotCalls != 1 {
		t.Fatal("restart repeated native rename already committed before failed save")
	}
	f.kvFault = true
	if _, err := p.register("codex:a", "Current native title", ""); err == nil {
		t.Fatal("same-name registration bypassed persistence failure")
	}
}

func TestRegisterRecoversUncertainNewAccountWithCurrentNativeTitle(t *testing.T) {
	p, f := fixture(t)
	channelConversation(t, p, "uncertain-name-room", "claude:b")
	f.createBotFault = "after"
	if _, err := p.register("codex:new", "Old native title", "codex:a"); err == nil {
		t.Fatal("lost native create response not injected")
	}
	if p.state.Agents["codex:new"] != nil {
		t.Fatal("uncertain creation enrolled identity before success")
	}
	u := f.users[readableUsername("codex:new", "Old native title")]
	if u == nil {
		t.Fatal("native creation did not commit before response loss")
	}
	f.createBotFault = ""
	a, err := p.register("codex:new", "Latest native title", "codex:a")
	if err != nil {
		t.Fatal(err)
	}
	if a.UserID != u.Id || a.Parent != "codex:a" || a.Name != "Latest native title" || f.bots[u.Id].DisplayName != a.Name {
		t.Fatal("recovered account identity or current name differs")
	}
	if f.patchBotCalls != 1 {
		t.Fatal("recovery did not align existing bot display name")
	}
}
