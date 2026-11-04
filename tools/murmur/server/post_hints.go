package main

import (
	"sort"

	"github.com/mattermost/mattermost/server/public/model"
	"github.com/mattermost/mattermost/server/public/plugin"
)

const postHintCapacity = 256
const postHintBatch = 64

type postHint struct{ ID, ChannelID string }

func (p *Plugin) initializePostHints() {
	// Created before OnActivate returns; callbacks never replace or close these
	// channels. A full queue may drop hints: the full canonical sweep repairs it.
	p.postHints = make(chan postHint, postHintCapacity)
	p.postWake = make(chan struct{}, 1)
}

// The SDK invokes these after commit, including posts created by this plugin.
// Never take p.mu or call API/SQL here: a synchronous callback can occur while
// a native write holds p.mu. Only immutable references enter the bounded queue.
func (p *Plugin) MessageHasBeenPosted(_ *plugin.Context, post *model.Post) {
	p.queuePostHint(post)
}

func (p *Plugin) MessageHasBeenUpdated(_ *plugin.Context, post, _ *model.Post) {
	p.queuePostHint(post)
}

func (p *Plugin) queuePostHint(post *model.Post) {
	if post == nil || !userPost(post) || !model.IsValidId(post.Id) || !model.IsValidId(post.ChannelId) {
		return
	}
	select {
	case p.postHints <- postHint{ID: post.Id, ChannelID: post.ChannelId}:
	default:
		return
	}
	p.wakePostHints()
}

func (p *Plugin) wakePostHints() {
	select {
	case p.postWake <- struct{}{}:
	default:
	}
}

// Called under p.mu by the existing loop or an actor poll. Hints accelerate
// ordinary UI/API arrivals without scanning every history. Current canonical
// lookup, not the callback payload, supplies observation metadata. A missed or
// failed lookup is repaired by the independent full sweep, never a blind post.
func (p *Plugin) drainPostHints() {
	unique := map[string]postHint{}
	for count := 0; count < postHintBatch; count++ {
		select {
		case hint := <-p.postHints:
			unique[hint.ID] = hint
		default:
			count = postHintBatch
		}
	}
	if len(p.postHints) > 0 {
		p.wakePostHints()
	}
	posts := []*model.Post{}
	for _, hint := range unique {
		if p.state.Conversations[hint.ChannelID] == nil {
			continue
		}
		post, err := p.API.GetPost(hint.ID)
		if err != nil || post == nil || post.Id != hint.ID || post.ChannelId != hint.ChannelID || !userPost(post) {
			continue
		}
		posts = append(posts, post)
	}
	sort.Slice(posts, func(i, j int) bool {
		return posts[i].CreateAt < posts[j].CreateAt || posts[i].CreateAt == posts[j].CreateAt && posts[i].Id < posts[j].Id
	})
	if p.observe(posts) {
		_ = p.save() // save records a global fault on failed/uncertain persistence.
	}
	if p.fault == "" {
		p.observeWorkspacePosts(posts)
	}
}
