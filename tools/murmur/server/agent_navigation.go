package main

import (
	"net/http"
	"sort"
	"strings"

	"github.com/mattermost/mattermost/server/public/model"
)

func (p *Plugin) agentDirectory(r *http.Request) (map[string]any, error) {
	limit, err := queryNumber(r, "limit", 20, 1, 100)
	if err != nil {
		return nil, err
	}
	query := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("query")))
	agents := []map[string]string{}
	for _, a := range p.state.Agents {
		if strings.Contains(strings.ToLower(a.ID), query) || strings.Contains(strings.ToLower(a.Name), query) || strings.Contains(strings.ToLower(a.Username), query) {
			agents = append(agents, map[string]string{"id": a.ID, "name": a.Name, "username": a.Username})
		}
	}
	sort.Slice(agents, func(i, j int) bool { return agents[i]["id"] < agents[j]["id"] })
	start := 0
	if after := r.URL.Query().Get("after"); after != "" {
		index := sort.Search(len(agents), func(i int) bool { return agents[i]["id"] >= after })
		if index == len(agents) || agents[index]["id"] != after {
			return nil, bad(400, "after must identify an existing agent matching this query; restart discovery without after")
		}
		start = index + 1
	}
	end := start + limit
	if end > len(agents) {
		end = len(agents)
	}
	var next any
	if end < len(agents) {
		next = agents[end-1]["id"]
	}
	return map[string]any{"agents": agents[start:end], "next_after": next}, nil
}

type threadSummary struct {
	ID           string `json:"id"`
	ChannelID    string `json:"channel_id"`
	Author       string `json:"author"`
	Preview      string `json:"preview"`
	CreateAt     int64  `json:"create_at"`
	LastActivity int64  `json:"last_activity"`
	ReplyCount   int    `json:"reply_count"`
}

func (p *Plugin) threadRoot(actor, id string) (*model.Post, error) {
	return p.readThreadRoot(actor, id, 0)
}

func (p *Plugin) readThreadRoot(actor, id string, version int) (*model.Post, error) {
	root, err := p.authorizedReadPost(actor, id, version)
	if err != nil {
		return nil, err
	}
	if !discussionRoot(root) {
		return nil, bad(400, "requested post is not a discussion root; use a root ID from threads")
	}
	return root, nil
}

func discussionRoot(post *model.Post) bool {
	return post != nil && post.RootId == "" && userPost(post)
}

func (p *Plugin) conversationThreads(actor, channel string, r *http.Request) (map[string]any, error) {
	version, err := requestedReadVersion(r)
	if err != nil {
		return nil, err
	}
	c, err := p.contentRead(actor, channel, version)
	if err != nil {
		return nil, err
	}
	limit, err := queryNumber(r, "limit", 20, 1, 100)
	if err != nil {
		return nil, err
	}
	before := r.URL.Query().Get("before")
	if before != "" {
		root, err := p.readThreadRoot(actor, before, version)
		if err != nil {
			return nil, err
		}
		if root.ChannelId != channel {
			return nil, bad(400, "before root belongs to another conversation")
		}
	}
	posts, err := p.canonical(channel)
	if err != nil {
		return nil, err
	}
	roots := map[string]*threadSummary{}
	for _, post := range posts {
		if post.ChannelId != channel || !userPost(post) || post.RootId != "" {
			continue
		}
		preview := []rune(postPreview(post))
		if len(preview) > 160 {
			preview = preview[:160]
		}
		roots[post.Id] = &threadSummary{ID: post.Id, ChannelID: channel, Author: p.postView(post)["author"].(string),
			Preview: string(preview), CreateAt: post.CreateAt, LastActivity: post.CreateAt}
	}
	for _, post := range posts {
		if post.ChannelId != channel || !userPost(post) || post.RootId == "" {
			continue
		}
		if root := roots[post.RootId]; root != nil {
			root.ReplyCount++
			if post.CreateAt > root.LastActivity {
				root.LastActivity = post.CreateAt
			}
		}
	}
	summaries := []*threadSummary{}
	for _, summary := range roots {
		summaries = append(summaries, summary)
	}
	sort.Slice(summaries, func(i, j int) bool {
		if summaries[i].LastActivity == summaries[j].LastActivity {
			return summaries[i].ID < summaries[j].ID
		}
		return summaries[i].LastActivity > summaries[j].LastActivity
	})
	start := 0
	if before != "" {
		start = -1
		for i, summary := range summaries {
			if summary.ID == before {
				start = i + 1
				break
			}
		}
		if start < 0 {
			return nil, bad(404, "before root is no longer in canonical history; restart thread listing without before")
		}
	}
	end := start + limit
	if end > len(summaries) {
		end = len(summaries)
	}
	var next any
	if end < len(summaries) {
		next = summaries[end-1].ID
	}
	return map[string]any{"threads": summaries[start:end], "next_before": next, "conversation": view(c, actor)}, nil
}

func (p *Plugin) threadPage(actor, id string, r *http.Request) (map[string]any, error) {
	version, err := requestedReadVersion(r)
	if err != nil {
		return nil, err
	}
	root, err := p.readThreadRoot(actor, id, version)
	if err != nil {
		return nil, err
	}
	page, err := p.canonicalThreadPage(root, r, 20, func(id string) (*model.Post, error) { return p.authorizedReadPost(actor, id, version) })
	if err == nil {
		page["conversation"] = view(p.state.Conversations[root.ChannelId], actor)
	}
	return page, err
}

func (p *Plugin) canonicalThreadPage(root *model.Post, r *http.Request, fallback int, resolve func(string) (*model.Post, error)) (map[string]any, error) {
	if !discussionRoot(root) {
		return nil, bad(400, "requested post is not a discussion root")
	}
	limit, err := queryNumber(r, "limit", fallback, 1, 200)
	if err != nil {
		return nil, err
	}
	before := r.URL.Query().Get("before")
	if before != "" {
		cursor, err := resolve(before)
		if err != nil {
			return nil, err
		}
		if cursor.ChannelId != root.ChannelId || (cursor.Id != root.Id && cursor.RootId != root.Id) || !userPost(cursor) {
			return nil, bad(400, "before post must belong to this actual thread")
		}
	}
	items, err := p.canonical(root.ChannelId)
	if err != nil {
		return nil, err
	}
	posts := []*model.Post{}
	var anchor *model.Post
	for _, post := range items {
		if post.ChannelId == root.ChannelId && userPost(post) && (post.Id == root.Id || post.RootId == root.Id) {
			posts = append(posts, post)
			if post.Id == root.Id {
				anchor = post
			}
		}
	}
	if anchor == nil {
		return nil, bad(404, "thread root is no longer in canonical history")
	}
	end := len(posts)
	if before != "" {
		end = -1
		for i, post := range posts {
			if post.Id == before {
				end = i
				break
			}
		}
		if end < 0 {
			return nil, bad(404, "before post is no longer in canonical history; restart thread read without before")
		}
	}
	start := end - limit
	if start < 0 {
		start = 0
	}
	page := []map[string]any{}
	for _, post := range posts[start:end] {
		page = append(page, p.postView(post))
	}
	var next any
	if start > 0 && len(page) > 0 {
		next = page[0]["id"]
	}
	return map[string]any{"root": p.postView(anchor), "posts": page, "next_before": next}, nil
}
