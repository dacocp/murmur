package main

import (
	"fmt"
	"reflect"
	"testing"
	"time"

	"github.com/mattermost/mattermost/server/public/model"
)

type hintedPostAPI struct {
	*fakeAPI
	plugin   *Plugin
	getCalls int
}

func (f *hintedPostAPI) CreatePost(post *model.Post) (*model.Post, *model.AppError) {
	result, err := f.fakeAPI.CreatePost(post)
	if result != nil {
		f.plugin.MessageHasBeenPosted(nil, result)
	}
	return result, err
}

func (f *hintedPostAPI) GetPost(id string) (*model.Post, *model.AppError) {
	f.getCalls++
	return f.fakeAPI.GetPost(id)
}

func TestPostHintsCallbackDoesNotLockOrCallNativeAPI(t *testing.T) {
	p := &Plugin{}
	p.initializePostHints()
	post := &model.Post{Id: model.NewId(), ChannelId: model.NewId()}
	p.mu.Lock()
	defer p.mu.Unlock()
	done := make(chan struct{})
	go func() {
		p.MessageHasBeenPosted(nil, post)
		p.MessageHasBeenUpdated(nil, post, nil)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("native callback blocked on the plugin mutex")
	}
	// There is deliberately no API/store on p. Calling either would panic.
	if len(p.postHints) != 2 || len(p.postWake) != 1 {
		t.Fatal("callbacks did not use one bounded/coalesced wakeup")
	}
	for n := 0; n < postHintCapacity*2; n++ {
		p.MessageHasBeenPosted(nil, post)
	}
	if len(p.postHints) != postHintCapacity {
		t.Fatal("full callback queue blocked or exceeded its bound")
	}
}

func TestPostHintsExposeFreshCanonicalPostWithoutHistorySweep(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "hint-room", "claude:b")
	p.initializePostHints()
	now := time.Unix(1000, 0)
	p.repairClock = func() time.Time { return now }
	history := &countingCanonicalHistory{canonicalHistory: f}
	p.history = history
	p.repair(true)
	post, _ := f.CreatePost(&model.Post{ChannelId: channel, UserId: p.state.Agents["claude:b"].UserID, Message: "native UI request"})
	p.MessageHasBeenPosted(nil, post)
	old := *post
	post.Message, post.UpdateAt = "latest canonical edit", post.UpdateAt+1
	p.MessageHasBeenUpdated(nil, post, &old)
	api := &hintedPostAPI{fakeAPI: f, plugin: p}
	p.API = api
	page := changesFor(t, p, "codex:a")
	if !reflect.DeepEqual(changedIDs(page), []string{post.Id}) || history.reads != 1 {
		t.Fatal("fresh native hint required another full history sweep", page, history.reads)
	}
	// One canonical lookup for the coalesced refs, then the normal feed lookup.
	if api.getCalls != 2 || p.state.Observed[post.Id].UpdateAt != post.UpdateAt {
		t.Fatal("hint used stale payload or repeated native lookups", api.getCalls)
	}
	loaded, err := p.metadata.Load()
	if err != nil || loaded.Observed[post.Id] != p.state.Observed[post.Id] {
		t.Fatal("feed returned an observation before durable commit", err)
	}
}

func TestPostHintsFromOwnCreateCannotDeadlockAndDoNotDuplicateArrival(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "synchronous-own-hook", "claude:b")
	p.initializePostHints()
	p.API = &hintedPostAPI{fakeAPI: f, plugin: p}
	done := make(chan error, 1)
	go func() {
		p.mu.Lock()
		defer p.mu.Unlock()
		_, err := p.send(postRequest{Actor: "codex:a", OpID: "own-post", ChannelID: channel, Message: "one post"})
		done <- err
	}()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("native CreatePost callback deadlocked its caller")
	}
	sequence := p.state.Sequence
	p.drainPostHints()
	if p.state.Sequence != sequence || len(p.state.Observed) != 1 || len(f.posts) != 1 {
		t.Fatal("own callback duplicated an already committed arrival")
	}
}

func TestPostHintsDroppedOrUnmanagedRefsRecoverAtFullRepair(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "hint-overflow", "claude:b")
	p.initializePostHints()
	now := time.Unix(1000, 0)
	p.repairClock = func() time.Time { return now }
	history := &countingCanonicalHistory{canonicalHistory: f}
	p.history = history
	p.repair(true)
	for n := 0; n < postHintCapacity; n++ {
		p.MessageHasBeenPosted(nil, &model.Post{Id: model.NewId(), ChannelId: model.NewId()})
	}
	post, _ := f.CreatePost(&model.Post{ChannelId: channel, UserId: p.state.Agents["claude:b"].UserID, Message: "overflowed reference"})
	p.MessageHasBeenPosted(nil, post) // Queue is full, so this is intentionally dropped.
	api := &hintedPostAPI{fakeAPI: f, plugin: p}
	p.API = api
	p.repair(false)
	if _, exists := p.state.Observed[post.Id]; exists || api.getCalls != 0 || len(p.postHints) != postHintCapacity-postHintBatch {
		t.Fatal("bounded drain fetched unmanaged posts or overflowed the queue")
	}
	now = now.Add(canonicalRepairInterval)
	p.repair(false)
	if _, exists := p.state.Observed[post.Id]; !exists || history.reads != 2 {
		t.Fatal("full repair lost a dropped native event")
	}
}

func TestPostHintPersistenceFailureRemainsGlobalFault(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "hint-persistence", "claude:b")
	p.initializePostHints()
	now := time.Unix(1000, 0)
	p.repairClock = func() time.Time { return now }
	p.repair(true)
	post, _ := f.CreatePost(&model.Post{ChannelId: channel, UserId: p.state.Agents["claude:b"].UserID, Message: "must be durable"})
	p.MessageHasBeenPosted(nil, post)
	f.kvFault = true
	p.repair(false)
	if p.fault == "" {
		t.Fatal("failed hinted observation did not fault the plugin")
	}
	navigationStatus(t, p, fmt.Sprintf("changes?attention_version=3&actor=%s", "codex:a"), 503)
}
