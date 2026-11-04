package main

import (
	"net/http"
	"sort"
	"strings"
)

// Public access is a prospective managed-channel policy, never an import of
// arbitrary native channels. Native privacy/team changes cannot widen it.
func (p *Plugin) publicChannel(id string) (bool, error) {
	native, err := p.API.GetChannel(id)
	if err != nil {
		if err.StatusCode == 404 {
			return false, nil
		}
		return false, err
	}
	if native == nil || native.Id != id {
		return false, bad(503, "canonical channel identity mismatch")
	}
	return native.Type == "O" && native.TeamId == p.state.TeamID && native.DeleteAt == 0, nil
}

func (p *Plugin) managedPublicChannel(c *Conversation) (bool, error) {
	if c.Kind != "group" || !c.Open {
		return false, nil
	}
	return p.publicChannel(c.ID)
}

// The snapshot is the accepted-join boundary. Save it before any native add:
// response loss/restart must not move the boundary and discard later arrivals.
func (p *Plugin) prepareTracking(o *Operation, c *Conversation) error {
	// Older pending intents may already have committed their native add. They
	// have no provable original boundary: preserve their unfiltered semantics
	// instead of capturing a later snapshot and discarding intervening work.
	if !o.Tracking || o.TrackAfterSeq != nil {
		return nil
	}
	posts, err := p.canonical(c.ID)
	if err != nil {
		return err
	}
	p.observe(posts)
	cutoff := p.state.Sequence
	o.TrackAfterSeq = &cutoff
	return p.save()
}

func (p *Plugin) grantMember(c *Conversation, actor string, cutoff *int64) {
	already := c.Grants[actor].Role == "member"
	p.grant(c, actor, "member")
	if !already {
		grant := c.Grants[actor]
		grant.TrackAfterSeq = cutoff
		c.Grants[actor] = grant
	}
}

func (p *Plugin) channelDirectory(actor string, r *http.Request) (map[string]any, error) {
	if _, err := p.requireActor(actor); err != nil {
		return nil, err
	}
	limit, err := queryNumber(r, "limit", 20, 1, 100)
	if err != nil {
		return nil, err
	}
	query := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("query")))
	channels := []*Conversation{}
	for _, c := range p.state.Conversations {
		if c.Kind != "group" || !c.Open || (!strings.Contains(strings.ToLower(c.ID), query) &&
			!strings.Contains(strings.ToLower(c.Name), query) && !strings.Contains(strings.ToLower(c.Purpose), query)) {
			continue
		}
		open, err := p.managedPublicChannel(c)
		if err != nil {
			return nil, err
		}
		if open {
			channels = append(channels, c)
		}
	}
	sort.Slice(channels, func(i, j int) bool { return channels[i].ID < channels[j].ID })
	start := 0
	if after := r.URL.Query().Get("after"); after != "" {
		index := sort.Search(len(channels), func(i int) bool { return channels[i].ID >= after })
		if index == len(channels) || channels[index].ID != after {
			return nil, bad(400, "after must identify an existing channel matching this query; restart discovery without after")
		}
		start = index + 1
	}
	end := start + limit
	if end > len(channels) {
		end = len(channels)
	}
	items := []map[string]any{}
	for _, c := range channels[start:end] {
		item := view(c, actor)
		members := []map[string]string{}
		for id, grant := range c.Grants {
			if grant.Role == "member" {
				members = append(members, map[string]string{"id": id, "name": p.state.Agents[id].Name})
			}
		}
		sort.Slice(members, func(i, j int) bool { return members[i]["id"] < members[j]["id"] })
		posts, err := p.canonical(c.ID)
		if err != nil {
			return nil, err
		}
		var activity int64
		for _, post := range posts {
			if userPost(post) && post.CreateAt > activity {
				activity = post.CreateAt
			}
		}
		item["members"], item["last_activity"] = members, activity
		items = append(items, item)
	}
	var next any
	if end < len(channels) {
		next = channels[end-1].ID
	}
	return map[string]any{"channels": items, "next_after": next}, nil
}
