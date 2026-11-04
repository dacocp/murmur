package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/mattermost/mattermost/server/public/model"
)

func changesFor(t *testing.T, p *Plugin, actor string) map[string]any {
	t.Helper()
	return call(t, p, "changes?attention_version=3&actor="+actor+"&limit=1000")
}

func changedIDs(page map[string]any) []string {
	ids := []string{}
	for _, entry := range page["changes"].([]any) {
		ids = append(ids, entry.(map[string]any)["post"].(map[string]any)["id"].(string))
	}
	sort.Strings(ids)
	return ids
}

func TestChannelContractRejectsRetiredRouteAndKindBeforeEffects(t *testing.T) {
	p, f := fixture(t)
	before := append([]byte{}, f.kv...)
	adds, writes, channels := f.addCalls, f.kvWrites, len(f.channels)
	navigationStatus(t, p, "groups?actor=codex:a", 404)
	r := httptest.NewRequest("POST", "/v1/conversations", strings.NewReader(`{"actor":"codex:a","op_id":"retired-kind","kind":"group","name":"Rejected"}`))
	r.Header.Set("Authorization", "Bearer test-token")
	w := httptest.NewRecorder()
	p.ServeHTTP(nil, w, r)
	if w.Code != 400 || !strings.Contains(w.Body.String(), "kind must be dm or channel") {
		t.Fatalf("retired creation kind accepted: %d %s", w.Code, w.Body)
	}
	if !bytes.Equal(before, f.kv) || adds != f.addCalls || writes != f.kvWrites || channels != len(f.channels) || p.state.Operations["retired-kind"] != nil {
		t.Fatal("rejected legacy spelling changed metadata or native state")
	}
}

func TestChannelSpellingReplaysLegacyCreationIntentWithoutDuplicateEffects(t *testing.T) {
	for _, open := range []bool{false, true} {
		for _, pending := range []bool{false, true} {
			t.Run(fmt.Sprintf("public=%t/pending=%t", open, pending), func(t *testing.T) {
				p, f := fixture(t)
				// Seed the exact normalized request and journal used by the old
				// public spelling, including private creation intents from before
				// public channels were introduced.
				legacy := conversationRequest{Actor: "codex:a", OpID: "old-create", Kind: "group", Name: "Original room", Members: []string{"claude:b", "codex:a"}}
				o, _, err := p.newOp(legacy.Actor, legacy.OpID, "conversation", legacy)
				if err != nil {
					t.Fatal(err)
				}
				o.Name, o.Stage, o.Members, o.Open, o.Tracking = legacy.Name, "group", legacy.Members, open, open
				if err = p.save(); err != nil {
					t.Fatal(err)
				}
				f.addFault = pending
				err = p.finishConversation(o)
				if (err != nil) != pending || o.ChannelID == "" {
					t.Fatalf("legacy native effect fixture failed: %v %#v", err, o)
				}
				id, hash := o.ChannelID, o.Hash
				p = reload(t, p, f)
				before := append([]byte{}, f.kv...)
				adds, writes, channels := f.addCalls, f.kvWrites, len(f.channels)
				public := legacy
				public.Kind, public.Members = "channel", []string{"claude:b"}
				result, err := p.conversation(public)
				if err != nil {
					t.Fatal(err)
				}
				descriptor := result["conversation"].(map[string]any)
				if descriptor["id"] != id || descriptor["kind"] != "channel" || descriptor["role"] != "member" || descriptor["open"] != open {
					t.Fatalf("legacy replay returned the wrong public identity: %#v", descriptor)
				}
				boot, err := p.operatorBootstrap(&model.User{})
				if err != nil || boot["conversations"].([]map[string]any)[0]["kind"] != "channel" {
					t.Fatalf("operator bootstrap leaked legacy kind: %#v %v", boot, err)
				}
				replayed, stored := p.state.Operations[legacy.OpID], p.state.Conversations[id]
				if replayed.Status != "complete" || replayed.Hash != hash || replayed.Stage != "group" || stored.Kind != "group" || stored.Open != open || len(stored.Grants) != 2 {
					t.Fatalf("legacy encoding or grants changed: %#v %#v", replayed, stored)
				}
				wantType := model.ChannelTypePrivate
				if open {
					wantType = model.ChannelTypeOpen
				}
				if f.channels[id].Type != wantType || len(f.members[id]) != 2 || adds != f.addCalls || writes != f.kvWrites || channels != len(f.channels) || !bytes.Equal(before, f.kv) {
					t.Fatal("new spelling repeated effects or changed legacy visibility")
				}
			})
		}
	}
}

func TestPublicChannelCatalogueAndVisitorPreviewsAreReadOnly(t *testing.T) {
	p, f := fixture(t)
	result, err := p.conversation(conversationRequest{Actor: "codex:a", OpID: "solo", Kind: "channel", Name: "Release planning", Purpose: "Deployment scope"})
	if err != nil {
		t.Fatal(err)
	}
	channel := result["conversation"].(map[string]any)["id"].(string)
	if !p.state.Conversations[channel].Open || f.channels[channel].Type != "O" || f.channels[channel].Purpose != "Deployment scope" {
		t.Fatal("creator-only channel did not publish a native public channel with purpose")
	}
	legacy := legacyPrivateChannel(t, p, "old-private", "claude:b")
	if p.state.Conversations[legacy].Open || f.channels[legacy].Type != "P" {
		t.Fatal("legacy intent became public")
	}
	unmanaged, _ := f.CreateChannel(&model.Channel{Type: "O", TeamId: p.state.TeamID, Name: "town-square"})
	root, _ := f.CreatePost(&model.Post{ChannelId: channel, UserId: p.state.Agents["codex:a"].UserID, Message: "Plan root"})
	reply, _ := f.CreatePost(&model.Post{ChannelId: channel, UserId: p.state.Agents["codex:a"].UserID, RootId: root.Id, Message: "Latest genuine activity"})
	f.CreatePost(&model.Post{ChannelId: channel, CreateAt: reply.CreateAt + 99999, Type: "system_join_channel", Message: "system"})
	before, _ := json.Marshal(p.state)
	kv, writes, adds := append([]byte{}, f.kv...), f.kvWrites, f.addCalls
	page := call(t, p, "channels?actor=codex:x&query=DEPLOYMENT")
	channels := page["channels"].([]any)
	if len(channels) != 1 {
		t.Fatalf("unexpected catalogue: %#v", page)
	}
	item := channels[0].(map[string]any)
	if item["id"] != channel || item["kind"] != "channel" || item["role"] != nil || item["visibility"] != "public" || item["purpose"] != "Deployment scope" || item["last_activity"] != float64(reply.CreateAt) || len(item["members"].([]any)) != 1 {
		t.Fatalf("incorrect channel metadata: %#v", item)
	}
	if _, retiredKey := page["groups"]; retiredKey {
		t.Fatal("directory retained the retired collection key")
	}
	for _, path := range []string{"conversations/" + channel + "/posts", "conversations/" + channel + "/threads", "threads/" + root.Id, "posts/" + reply.Id} {
		response := call(t, p, path+"?actor=codex:x")
		if descriptor, ok := response["conversation"].(map[string]any); ok && (descriptor["id"] != channel || descriptor["role"] != nil) {
			t.Fatal("preview fabricated membership")
		}
	}
	for _, id := range []string{legacy, unmanaged.Id} {
		navigationStatus(t, p, "conversations/"+id+"/posts?actor=codex:x", map[bool]int{true: 403, false: 404}[id == legacy])
	}
	navigationStatus(t, p, "channels?actor=codex:unenrolled", 403)
	_, err = p.send(postRequest{Actor: "codex:x", OpID: "visitor-send", ChannelID: channel, Message: "not joined"})
	status(t, err, 403)
	after, _ := json.Marshal(p.state)
	if !bytes.Equal(before, after) || !bytes.Equal(kv, f.kv) || writes != f.kvWrites || adds != f.addCalls {
		t.Fatal("preview changed grants, feed, operations or native membership")
	}
}

func TestPublicChannelCataloguePagingVisibilityAndPurposeValidation(t *testing.T) {
	p, f := fixture(t)
	for i := 0; i < 24; i++ {
		channelConversation(t, p, fmt.Sprintf("catalogue-%02d", i))
	}
	first := call(t, p, "channels?actor=codex:x")
	if len(first["channels"].([]any)) != 20 || first["next_after"] == nil {
		t.Fatal("catalogue default is not bounded")
	}
	second := call(t, p, "channels?actor=codex:x&after="+first["next_after"].(string))
	if len(second["channels"].([]any)) != 4 || second["next_after"] != nil {
		t.Fatal("catalogue continuation lost channels")
	}
	ids := []string{}
	for _, page := range []map[string]any{first, second} {
		for _, item := range page["channels"].([]any) {
			ids = append(ids, item.(map[string]any)["id"].(string))
		}
	}
	if !sort.StringsAreSorted(ids) {
		t.Fatal("catalogue is not in stable ID order")
	}
	for _, query := range []string{"limit=0", "limit=101", "limit=no", "after=missing", "query=absent&after=" + ids[0]} {
		navigationStatus(t, p, "channels?actor=codex:x&"+query, 400)
	}
	for _, drift := range []string{"private", "team", "deleted"} {
		t.Run(drift, func(t *testing.T) {
			channel := channelConversation(t, p, "drift-"+drift)
			switch drift {
			case "private":
				f.channels[channel].Type = "P"
			case "team":
				f.channels[channel].TeamId = "other"
			case "deleted":
				f.channels[channel].DeleteAt = 1
			}
			page := call(t, p, "channels?actor=codex:x&query="+channel)
			if len(page["channels"].([]any)) != 0 {
				t.Fatal("native visibility drift exposed channel")
			}
			navigationStatus(t, p, "conversations/"+channel+"/posts?actor=codex:x", 403)
		})
	}
	_, err := p.conversation(conversationRequest{Actor: "codex:a", OpID: "purpose-long", Kind: "channel", Name: "Purpose", Purpose: strings.Repeat("é", 251)})
	status(t, err, 400)
	if p.state.Operations["purpose-long"] != nil {
		t.Fatal("invalid purpose allocated an operation")
	}
	_, err = p.conversation(conversationRequest{Actor: "codex:a", OpID: "purpose-valid", Kind: "channel", Name: "Purpose", Purpose: strings.Repeat("é", 250)})
	if err != nil {
		t.Fatal(err)
	}
}

func TestInheritedDMAndChannelsAreLiveReferencesWithoutFeedArrivals(t *testing.T) {
	p, _ := fixture(t)
	channel := channelConversation(t, p, "reference-channel", "claude:b")
	dmResult, err := p.conversation(conversationRequest{Actor: "codex:a", OpID: "reference-dm", Kind: "dm", Members: []string{"claude:b"}})
	if err != nil {
		t.Fatal(err)
	}
	dm := dmResult["conversation"].(map[string]any)["id"].(string)
	old := send(t, p, "claude:b", channel, "before-child", "old channel history", "")
	mustRegister(t, p, "codex:child", "codex:a")
	newChannel := send(t, p, "claude:b", channel, "after-child", "live channel history", old)
	newDM := send(t, p, "claude:b", dm, "dm-after-child", "live parent DM", "")
	page := changesFor(t, p, "codex:child")
	if len(changedIDs(page)) != 0 || page["cursor"] != float64(p.state.Sequence) || len(page["conversations"].([]any)) != 2 {
		t.Fatal("observer grants became tracking subscriptions")
	}
	for _, id := range []string{old, newChannel, newDM} {
		if _, err := p.authorizedPost("codex:child", id); err != nil {
			t.Fatal("quiet observer lost live reference access", err)
		}
	}
	if _, err = p.membership(channel, "join", memberRequest{Actor: "codex:child", OpID: "reference-join"}); err != nil {
		t.Fatal(err)
	}
	if len(changedIDs(changesFor(t, p, "codex:child"))) != 0 {
		t.Fatal("promotion imported unsynced inherited history")
	}
	fresh := send(t, p, "claude:b", channel, "joined-fresh", "new work", old)
	if got := changedIDs(changesFor(t, p, "codex:child")); !reflect.DeepEqual(got, []string{fresh}) {
		t.Fatalf("wrong joined feed: %v", got)
	}
}

func TestJoinTrackingSurvivesLostResponseEditsAndRejoin(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "join-boundary", "claude:b")
	old := send(t, p, "claude:b", channel, "baseline", "old root", "")
	f.addFault = true
	_, err := p.membership(channel, "join", memberRequest{Actor: "codex:x", OpID: "join-lost"})
	if err == nil || !f.members[channel][p.state.Agents["codex:x"].UserID] {
		t.Fatal("native add response loss was not reached")
	}
	cutoff := *p.state.Operations["join-lost"].TrackAfterSeq
	if _, ok := p.state.Conversations[channel].Grants["codex:x"]; ok {
		t.Fatal("lost native response prematurely published grant")
	}
	first := p.state.Observed[old].FirstSeenSeq
	f.posts[old].Message = "edited after joining"
	f.posts[old].UpdateAt++
	newPost, _ := f.CreatePost(&model.Post{ChannelId: channel, UserId: p.state.Agents["claude:b"].UserID, RootId: old, Message: "during uncertain join", CreateAt: f.posts[old].CreateAt})
	p = reload(t, p, f)
	if *p.state.Conversations[channel].Grants["codex:x"].TrackAfterSeq != cutoff || p.state.Observed[old].FirstSeenSeq != first {
		t.Fatal("recovery moved cutoff or edited post origin")
	}
	if got := changedIDs(changesFor(t, p, "codex:x")); !reflect.DeepEqual(got, []string{newPost.Id}) {
		t.Fatalf("old edit woke or new reply lost: %v", got)
	}
	_, err = p.membership(channel, "join", memberRequest{Actor: "codex:x", OpID: "member-repeat"})
	if err != nil || *p.state.Conversations[channel].Grants["codex:x"].TrackAfterSeq != cutoff {
		t.Fatal("already-member join rebased tracking")
	}
	_, err = p.membership(channel, "leave", memberRequest{Actor: "codex:x", OpID: "boundary-leave"})
	if err != nil {
		t.Fatal(err)
	}
	absent := send(t, p, "claude:b", channel, "while-absent", "not tracked", old)
	if _, err = p.authorizedPost("codex:x", absent); err != nil {
		t.Fatal("public leave revoked readable history")
	}
	_, err = p.membership(channel, "join", memberRequest{Actor: "codex:x", OpID: "boundary-rejoin"})
	if err != nil {
		t.Fatal(err)
	}
	empty := changesFor(t, p, "codex:x")
	if len(changedIDs(empty)) != 0 || empty["more"] != false || empty["cursor"] != float64(p.state.Sequence) {
		t.Fatal("filtered history stalled cursor or imported absence backlog")
	}
	fresh := send(t, p, "claude:b", channel, "after-rejoin", "tracked again", old)
	if got := changedIDs(changesFor(t, p, "codex:x")); !reflect.DeepEqual(got, []string{fresh}) {
		t.Fatalf("wrong rejoin feed: %v", got)
	}
}

func TestBoundaryPersistenceBeforeAddAndAtomicPublication(t *testing.T) {
	for _, phase := range []string{"before-add", "after-add"} {
		t.Run(phase, func(t *testing.T) {
			p, f := fixture(t)
			channel := channelConversation(t, p, "atomic-"+phase, "claude:b")
			send(t, p, "claude:b", channel, "old-"+phase, "baseline", "")
			adds := f.addCalls
			// Bound journal, snapshot boundary, final publication.
			f.kvFaultAt = f.kvWrites + 2
			if phase == "after-add" {
				f.kvFaultAt = f.kvWrites + 3
			}
			_, err := p.membership(channel, "join", memberRequest{Actor: "codex:x", OpID: "atomic-join"})
			if err == nil || p.fault == "" {
				t.Fatal("expected persistent publication fault")
			}
			if phase == "before-add" && f.addCalls != adds {
				t.Fatal("native addition preceded durable cutoff")
			}
			if phase == "after-add" && !f.members[channel][p.state.Agents["codex:x"].UserID] {
				t.Fatal("fault did not follow native commit")
			}
			var saved State
			if err := json.Unmarshal(f.kv, &saved); err != nil {
				t.Fatal(err)
			}
			if _, ok := saved.Conversations[channel].Grants["codex:x"]; ok {
				t.Fatal("failed save partially published grant")
			}
			f.kvFaultAt = 0
			p = reload(t, p, f)
			if p.state.Operations["atomic-join"].Status != "complete" || p.state.Conversations[channel].Grants["codex:x"].TrackAfterSeq == nil {
				t.Fatal("journal could not recover join")
			}
		})
	}
}

func TestPublicSavedMetadataSurvivesLeavingWithoutSubscription(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "public-saved", "claude:b")
	root := send(t, p, "claude:b", channel, "bookmark-root", "save without subscribing", "")
	before, _ := json.Marshal(p.state)
	writes, adds := f.kvWrites, f.addCalls
	star(t, p, "codex:x", root, true)
	items := savedItems(t, p, "codex:x")
	descriptor := items[0]["conversation"].(map[string]any)
	if descriptor["id"] != channel || descriptor["role"] != nil || descriptor["visibility"] != "public" {
		t.Fatal("saved visitor metadata implied membership")
	}
	after, _ := json.Marshal(p.state)
	if !bytes.Equal(before, after) || writes != f.kvWrites || adds != f.addCalls {
		t.Fatal("public bookmark changed tracking or membership")
	}
	star(t, p, "codex:a", root, true)
	if _, err := p.membership(channel, "leave", memberRequest{Actor: "codex:a", OpID: "saved-public-leave"}); err != nil {
		t.Fatal(err)
	}
	items = savedItems(t, p, "codex:a")
	if len(items) != 1 || items[0]["conversation"].(map[string]any)["role"] != nil || items[0]["unavailable"] != nil {
		t.Fatal("public bookmark disappeared after leave")
	}
}

func TestPublicCreationStaysUnpublishedAcrossLostMembershipResponse(t *testing.T) {
	p, f := fixture(t)
	f.addFault = true
	_, err := p.conversation(conversationRequest{Actor: "codex:a", OpID: "unpublished", Kind: "channel", Name: "Not published yet"})
	if err == nil {
		t.Fatal("native add did not lose its response")
	}
	o := p.state.Operations["unpublished"]
	cutoff := *o.TrackAfterSeq
	if p.state.Conversations[o.ChannelID].Open || f.channels[o.ChannelID].Type != "O" {
		t.Fatal("creation did not reach unpublished native O stage")
	}
	page := call(t, p, "channels?actor=codex:x")
	if len(page["channels"].([]any)) != 0 {
		t.Fatal("pending create entered catalogue")
	}
	navigationStatus(t, p, "conversations/"+o.ChannelID+"/posts?actor=codex:x", 403)
	post, _ := f.CreatePost(&model.Post{ChannelId: o.ChannelID, UserId: p.state.Agents["claude:b"].UserID, Message: "after original creation boundary"})
	p = reload(t, p, f)
	c := p.state.Conversations[o.ChannelID]
	if !c.Open || p.state.Operations["unpublished"].Status != "complete" || *c.Grants["codex:a"].TrackAfterSeq != cutoff {
		t.Fatal("creation retry moved cutoff or failed publication")
	}
	if got := changedIDs(changesFor(t, p, "codex:a")); !reflect.DeepEqual(got, []string{post.Id}) {
		t.Fatalf("uncertain-creation arrival lost: %v", got)
	}
}

func TestTrackingFilterPrecedesPaginationAndFirstSeenSurvivesLegacyBackfill(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "paged-tracking", "claude:b")
	old := ""
	for i := 0; i < 31; i++ {
		old = send(t, p, "claude:b", channel, fmt.Sprintf("old-%d", i), "old", "")
	}
	item := p.state.Observed[old]
	item.FirstSeenSeq = 0
	p.state.Observed[old] = item
	if err := p.save(); err != nil {
		t.Fatal(err)
	}
	p = reload(t, p, f)
	first := p.state.Observed[old].FirstSeenSeq
	if first == 0 {
		t.Fatal("legacy observation did not acquire immutable origin")
	}
	if _, err := p.membership(channel, "join", memberRequest{Actor: "codex:x", OpID: "paged-join"}); err != nil {
		t.Fatal(err)
	}
	a := send(t, p, "claude:b", channel, "new-one", "new one", old)
	b := send(t, p, "claude:b", channel, "new-two", "new two", old)
	f.posts[old].Message = "current edited body"
	f.posts[old].UpdateAt++
	page := call(t, p, "changes?attention_version=3&actor=codex:x&limit=1")
	if got := changedIDs(page); !reflect.DeepEqual(got, []string{a}) || page["more"] != true {
		t.Fatalf("untracked history occupied first page: %#v", page)
	}
	page = call(t, p, fmt.Sprintf("changes?attention_version=3&actor=codex:x&limit=1&cursor=%.0f&generation=%.0f", page["cursor"], page["generation"]))
	if got := changedIDs(page); !reflect.DeepEqual(got, []string{b}) || page["more"] != false || page["cursor"] != float64(p.state.Sequence) {
		t.Fatalf("filtered final history stalled cursor: %#v", page)
	}
	if p.state.Observed[old].FirstSeenSeq != first {
		t.Fatal("edit changed legacy first-seen identity")
	}
	if post, err := p.authorizedPost("codex:x", old); err != nil || post.Message != "current edited body" {
		t.Fatal("omitted feed edit hid current canonical text")
	}
}

type wrongChannelHistory struct{ post *model.Post }

func (h wrongChannelHistory) Posts(string) ([]*model.Post, error) { return []*model.Post{h.post}, nil }

func TestPublicPreviewAndJoinRejectWrongCanonicalChannelBeforeEffects(t *testing.T) {
	p, f := fixture(t)
	channel := channelConversation(t, p, "canonical-guard", "claude:b")
	foreign := &model.Post{Id: model.NewId(), ChannelId: "foreign", Message: "must not leak"}
	p.history = wrongChannelHistory{post: foreign}
	before, _ := json.Marshal(p.state)
	adds := f.addCalls
	navigationStatus(t, p, "conversations/"+channel+"/posts?actor=codex:x", 503)
	_, err := p.membership(channel, "join", memberRequest{Actor: "codex:x", OpID: "wrong-snapshot"})
	status(t, err, 503)
	if f.addCalls != adds || p.state.Operations["wrong-snapshot"].TrackAfterSeq != nil {
		t.Fatal("invalid snapshot reached native membership or prepared boundary")
	}
	// Only the accepted pending operation may have been added; preview itself
	// did not publish any returned foreign post or grant.
	delete(p.state.Operations, "wrong-snapshot")
	after, _ := json.Marshal(p.state)
	if !bytes.Equal(before, after) {
		t.Fatal("foreign history changed managed coordination state")
	}
}

type spoofPostAPI struct {
	*fakeAPI
	target string
	value  *model.Post
}

func (f spoofPostAPI) GetPost(id string) (*model.Post, *model.AppError) {
	if id == f.target {
		return f.value, nil
	}
	return f.fakeAPI.GetPost(id)
}

func TestFeedRejectsWrongPostIdentityEvenWithinAuthorizedChannel(t *testing.T) {
	for _, nilPost := range []bool{false, true} {
		t.Run(fmt.Sprint(nilPost), func(t *testing.T) {
			p, f := fixture(t)
			channel := channelConversation(t, p, "feed-identity", "claude:b")
			target := send(t, p, "codex:a", channel, "identity-root", "original", "")
			var value *model.Post
			if !nilPost {
				value = &model.Post{Id: model.NewId(), ChannelId: channel, Message: "wrong same-channel post"}
			}
			p.API = spoofPostAPI{fakeAPI: f, target: target, value: value}
			navigationStatus(t, p, "changes?attention_version=3&actor=claude:b", 503)
		})
	}
}

func TestLegacyPendingNativeAddsDoNotAcquireALaterTrackingBoundary(t *testing.T) {
	for _, kind := range []string{"join", "conversation"} {
		t.Run(kind, func(t *testing.T) {
			p, f := fixture(t)
			receiver, channel := "codex:a", ""
			var o *Operation
			if kind == "join" {
				channel = legacyPrivateChannel(t, p, "legacy-pending-room", "claude:b")
				mustRegister(t, p, "codex:child", "codex:a")
				receiver = "codex:child"
				var err error
				o, _, err = p.newOp(receiver, "old-pending", kind, struct{ Legacy bool }{true})
				if err != nil {
					t.Fatal(err)
				}
				o.ChannelID = channel
				if err = p.save(); err != nil {
					t.Fatal(err)
				}
				f.addFault = true
				if _, err = f.AddChannelMember(channel, p.state.Agents[receiver].UserID); err == nil {
					t.Fatal("legacy native response loss not reached")
				}
			} else {
				var err error
				o, _, err = p.newOp("codex:a", "old-pending", kind, struct{ Legacy bool }{true})
				if err != nil {
					t.Fatal(err)
				}
				o.Name, o.Stage, o.Members = "Legacy interrupted creation", "group", []string{"claude:b", "codex:a"}
				if err = p.save(); err != nil {
					t.Fatal(err)
				}
				f.addFault = true
				if err = p.finishConversation(o); err == nil {
					t.Fatal("legacy create native response loss not reached")
				}
				channel = o.ChannelID
			}
			if o.Tracking || o.TrackAfterSeq != nil {
				t.Fatal("fixture was not a pre-upgrade journal")
			}
			post, _ := f.CreatePost(&model.Post{ChannelId: channel, UserId: p.state.Agents["claude:b"].UserID, Message: "arrived after original native add"})
			p = reload(t, p, f)
			if p.state.Operations["old-pending"].Status != "complete" || p.state.Conversations[channel].Grants[receiver].TrackAfterSeq != nil || p.state.Operations["old-pending"].TrackAfterSeq != nil {
				t.Fatal("upgrade invented a late boundary for a prior native effect")
			}
			if f.channels[channel].Type != "P" || p.state.Conversations[channel].Open {
				t.Fatal("legacy native channel changed visibility")
			}
			if got := changedIDs(changesFor(t, p, receiver)); !reflect.DeepEqual(got, []string{post.Id}) {
				t.Fatalf("upgrade lost intervening work: %v", got)
			}
		})
	}
}

type substitutedChannelAPI struct {
	*fakeAPI
	direct, lookup, created, canonical *model.Channel
}

func (f substitutedChannelAPI) GetDirectChannel(a, b string) (*model.Channel, *model.AppError) {
	if f.direct != nil {
		return f.direct, nil
	}
	return f.fakeAPI.GetDirectChannel(a, b)
}
func (f substitutedChannelAPI) GetChannelByName(team, name string, deleted bool) (*model.Channel, *model.AppError) {
	if f.lookup != nil {
		return f.lookup, nil
	}
	return f.fakeAPI.GetChannelByName(team, name, deleted)
}
func (f substitutedChannelAPI) CreateChannel(c *model.Channel) (*model.Channel, *model.AppError) {
	if f.created != nil {
		return f.created, nil
	}
	return f.fakeAPI.CreateChannel(c)
}
func (f substitutedChannelAPI) GetChannel(id string) (*model.Channel, *model.AppError) {
	if f.canonical != nil {
		return f.canonical, nil
	}
	return f.fakeAPI.GetChannel(id)
}

func TestConversationCreationRejectsSubstitutedNativeChannelsBeforePublication(t *testing.T) {
	for _, source := range []string{"lookup", "create", "canonical", "wrong-team", "wrong-type", "direct", "third-member", "recovery"} {
		t.Run(source, func(t *testing.T) {
			p, f := fixture(t)
			req := conversationRequest{Actor: "codex:a", OpID: "substituted", Kind: "channel", Name: "Intended", Members: []string{"claude:b"}}
			foreign, _ := f.CreateChannel(&model.Channel{Name: "foreign", Type: "O", TeamId: p.state.TeamID})
			api := substitutedChannelAPI{fakeAPI: f}
			switch source {
			case "lookup":
				api.lookup = foreign
			case "create":
				api.created = foreign
			case "canonical":
				api.canonical = foreign
			case "wrong-team":
				foreign.Name = "murmur-" + digest(req.OpID)[:24]
				foreign.TeamId = "another-team"
				api.lookup = foreign
			case "wrong-type":
				foreign.Name = "murmur-" + digest(req.OpID)[:24]
				foreign.Type = "P"
				api.lookup = foreign
			case "direct":
				req.Kind = "dm"
				var err *model.AppError
				foreign, err = f.GetDirectChannel(p.state.Agents["codex:a"].UserID, p.state.Agents["codex:x"].UserID)
				if err != nil {
					t.Fatal(err)
				}
				api.direct = foreign
			case "third-member":
				req.Kind = "dm"
				var err *model.AppError
				foreign, err = f.GetDirectChannel(p.state.Agents["codex:a"].UserID, p.state.Agents["claude:b"].UserID)
				if err != nil {
					t.Fatal(err)
				}
				f.members[foreign.Id][p.state.Agents["codex:x"].UserID] = true
				api.direct = foreign
			case "recovery":
				legacyRequest := req
				legacyRequest.Kind = "group"
				legacyRequest.Members = []string{"claude:b", "codex:a"}
				o, _, err := p.newOp(req.Actor, req.OpID, "conversation", legacyRequest)
				if err != nil {
					t.Fatal(err)
				}
				o.ChannelID, o.Stage, o.Open, o.Members = foreign.Id, "group", true, []string{"codex:a", "claude:b"}
				if err = p.save(); err != nil {
					t.Fatal(err)
				}
			}
			p.API = api
			adds := f.addCalls
			_, err := p.conversation(req)
			status(t, err, 409)
			if p.state.Conversations[foreign.Id] != nil || f.addCalls != adds {
				t.Fatal("substituted response granted foreign channel or added native members")
			}
			if source != "recovery" && p.state.Operations[req.OpID].ChannelID != "" {
				t.Fatal("foreign native ID was durably selected before validation")
			}
		})
	}
}
