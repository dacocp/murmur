package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http/httptest"
	"reflect"
	"testing"

	"github.com/mattermost/mattermost/server/public/model"
)

type attentionSnapshotHistory struct {
	canonicalHistory
	reads int
	err   error
	after func()
}

func (h *attentionSnapshotHistory) Posts(channel string) ([]*model.Post, error) {
	h.reads++
	if h.err != nil {
		return nil, h.err
	}
	posts, err := h.canonicalHistory.Posts(channel)
	if h.after != nil {
		after := h.after
		h.after = nil
		after()
	}
	return posts, err
}

func attentionPath(channel, actor string) string {
	return "conversations/" + channel + "/attention-boundary?actor=" + actor
}

func TestAttentionBoundaryCapturesMissedCanonicalPostsDurablyBeforeLaterArrivals(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "attention-room", "claude:b")
	cached := p.state.Sequence
	beforePost, _ := f.CreatePost(&model.Post{ChannelId: channel, Message: "missed before enabling attention"})
	if _, exists := p.state.Observed[beforePost.Id]; exists {
		t.Fatal("fixture unexpectedly delivered the native callback")
	}
	var later *model.Post
	history := &attentionSnapshotHistory{canonicalHistory: f, after: func() {
		later, _ = f.CreatePost(&model.Post{ChannelId: channel, Message: "arrived after the canonical snapshot"})
		// Native timestamps do not define a notification boundary.
		later.CreateAt, later.UpdateAt = 1, 1
	}}
	p.history = history
	grants, _ := json.Marshal(p.state.Conversations)
	agents, _ := json.Marshal(p.state.Agents)
	operations, _ := json.Marshal(p.state.Operations)
	members, _ := json.Marshal(f.members)
	writes, adds := f.kvWrites, f.addCalls
	page := call(t, p, attentionPath(channel, "codex:a"))
	cutoff := int64(page["after_seq"].(float64))
	if len(page) != 2 || page["conversation_id"] != channel || cutoff <= cached || history.reads != 1 {
		t.Fatal("boundary used cached state or returned the wrong scope", page, cached, history.reads)
	}
	if seen := p.state.Observed[beforePost.Id]; seen.FirstSeenSeq == 0 || seen.FirstSeenSeq > cutoff {
		t.Fatal("missed earlier post can leak into a new follow", seen, cutoff)
	}
	var persisted State
	if err := json.Unmarshal(f.kv, &persisted); err != nil {
		t.Fatal(err)
	}
	if persisted.Sequence != cutoff || persisted.Observed[beforePost.Id] != p.state.Observed[beforePost.Id] || f.kvWrites != writes+1 {
		t.Fatal("response preceded durable canonical observation", persisted.Sequence, cutoff)
	}
	if _, exists := persisted.Observed[later.Id]; exists {
		t.Fatal("post arriving after snapshot was included in the earlier cutoff")
	}
	afterGrants, _ := json.Marshal(p.state.Conversations)
	afterAgents, _ := json.Marshal(p.state.Agents)
	afterOperations, _ := json.Marshal(p.state.Operations)
	afterMembers, _ := json.Marshal(f.members)
	if !bytes.Equal(grants, afterGrants) || !bytes.Equal(agents, afterAgents) || !bytes.Equal(operations, afterOperations) || !bytes.Equal(members, afterMembers) || adds != f.addCalls {
		t.Fatal("boundary changed participation, generations or native operations")
	}
	// A restart/backfill must retain the prior cutoff while finding the later
	// post even though its timestamp is older and its callback was also missed.
	p = reload(t, p, f)
	if p.state.Observed[later.Id].FirstSeenSeq <= cutoff {
		t.Fatal("restart lost the strict after-snapshot ordering", p.state.Observed[later.Id], cutoff)
	}
	missedAfterRestart, _ := f.CreatePost(&model.Post{ChannelId: channel, Message: "missed after restart"})
	next := call(t, p, attentionPath(channel, "codex:a"))
	nextCutoff := int64(next["after_seq"].(float64))
	if nextCutoff <= cutoff || p.state.Observed[missedAfterRestart.Id].FirstSeenSeq > nextCutoff {
		t.Fatal("new snapshot missed canonical history after restart", next)
	}
	again := call(t, p, attentionPath(channel, "codex:a"))
	if !reflect.DeepEqual(next, again) {
		t.Fatal("unchanged canonical snapshot moved its sequence boundary", next, again)
	}
}

func TestAttentionBoundaryRequiresFullGrantAndCurrentNativeMembership(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "attention-members", "claude:b")
	mustRegister(t, p, "codex:observer", "codex:a")
	p.config.AllowManagedConversationReads = true
	history := &attentionSnapshotHistory{canonicalHistory: f}
	p.history = history
	before, _ := json.Marshal(p.state)
	writes := f.kvWrites
	for _, actor := range []string{"codex:x", "codex:observer", "codex:unenrolled"} {
		navigationStatus(t, p, attentionPath(channel, actor)+"&read_version=1", 403)
	}
	navigationStatus(t, p, attentionPath(model.NewId(), "codex:a"), 404)
	delete(f.members[channel], p.state.Agents["codex:a"].UserID)
	navigationStatus(t, p, attentionPath(channel, "codex:a"), 409)
	after, _ := json.Marshal(p.state)
	if history.reads != 0 || writes != f.kvWrites || !bytes.Equal(before, after) {
		t.Fatal("rejected attention request captured history or changed metadata")
	}
}

func TestAttentionBoundaryRechecksMembershipAfterSlowSnapshot(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "attention-membership-race")
	f.CreatePost(&model.Post{ChannelId: channel, Message: "not yet observed"})
	p.history = &attentionSnapshotHistory{canonicalHistory: f, after: func() {
		delete(f.members[channel], p.state.Agents["codex:a"].UserID)
	}}
	before, _ := json.Marshal(p.state)
	writes := f.kvWrites
	navigationStatus(t, p, attentionPath(channel, "codex:a"), 409)
	after, _ := json.Marshal(p.state)
	if writes != f.kvWrites || !bytes.Equal(before, after) {
		t.Fatal("departed member published a new attention cutoff")
	}
}

func TestAttentionBoundaryNeverReturnsUnconfirmedSnapshot(t *testing.T) {
	for _, failSave := range []bool{false, true} {
		t.Run(fmt.Sprint("save=", failSave), func(t *testing.T) {
			p, f := fixture(t)
			channel := channelConversation(t, p, "attention-failure")
			post, _ := f.CreatePost(&model.Post{ChannelId: channel, Message: "missed callback"})
			if failSave {
				f.kvFault = true
			} else {
				p.history = &attentionSnapshotHistory{canonicalHistory: f, err: errors.New("canonical snapshot unavailable")}
			}
			before := append([]byte{}, f.kv...)
			req := httptest.NewRequest("GET", "/v1/"+attentionPath(channel, "codex:a"), nil)
			req.Header.Set("Authorization", "Bearer test-token")
			w := httptest.NewRecorder()
			p.ServeHTTP(nil, w, req)
			var result map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil {
				t.Fatal(err)
			}
			if w.Code != 503 || result["after_seq"] != nil || !bytes.Equal(before, f.kv) {
				t.Fatal("failed snapshot became a usable boundary", w.Code, result)
			}
			if failSave {
				if p.fault == "" {
					t.Fatal("uncertain metadata commit did not hold the untrusted state")
				}
				navigationStatus(t, p, attentionPath(channel, "codex:a"), 503)
				f.kvFault = false
				p = reload(t, p, f)
			} else {
				p.history = f
			}
			page := call(t, p, attentionPath(channel, "codex:a"))
			if p.state.Observed[post.Id].FirstSeenSeq == 0 || p.state.Observed[post.Id].FirstSeenSeq > int64(page["after_seq"].(float64)) {
				t.Fatal("successful recovery failed to snapshot the original post", page)
			}
		})
	}
}

func TestAttentionBoundaryHealthCapabilityAndMethod(t *testing.T) {
	p, _ := fixture(t)
	capability := call(t, p, "health")["capabilities"].(map[string]any)["attention_boundaries"]
	if !reflect.DeepEqual(capability, map[string]any{"version": float64(1)}) {
		t.Fatal("missing exact attention boundary capability", capability)
	}
	channel := channelConversation(t, p, "attention-method")
	managedRequest(t, p, "POST", attentionPath(channel, "codex:a"), nil, 404)
}
