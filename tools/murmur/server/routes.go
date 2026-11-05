package main

import (
	"net/http"
	"sort"
	"strconv"

	"github.com/mattermost/mattermost/server/public/model"
)

func (p *Plugin) route(parts []string, r *http.Request) (any, error) {
	if len(parts) < 2 || parts[0] != "v1" {
		return nil, bad(404, "API route not found")
	}
	actor := r.URL.Query().Get("actor")
	get := r.Method == http.MethodGet
	post := r.Method == http.MethodPost
	switch parts[1] {
	case "register":
		if len(parts) == 2 && post {
			var req struct {
				Actor          string        `json:"actor"`
				Name           string        `json:"name"`
				Parent         string        `json:"parent,omitempty"`
				UsernamePrefix *string       `json:"username_prefix,omitempty"`
				Lineage        []LineageEdge `json:"lineage,omitempty"`
				EnrollmentID   string        `json:"enrollment_id,omitempty"`
				Mode           string        `json:"mode"`
			}
			if err := decode(r, &req); err != nil {
				return nil, err
			}
			if req.Mode != "native" && req.Mode != "manual" {
				return nil, bad(400, "mode=native or manual is required; update the Murmur client")
			}
			if req.Mode == "native" {
				current := false
				for _, edge := range req.Lineage {
					if edge.Actor == req.Actor && edge.Parent == req.Parent {
						current = true
					}
				}
				if !current {
					return nil, bad(400, "native registration requires its verified current lineage edge")
				}
			}
			var prefixes []string
			if req.UsernamePrefix != nil {
				prefixes = append(prefixes, *req.UsernamePrefix)
			}
			if len(req.EnrollmentID) > 200 {
				return nil, bad(400, "enrollment_id must contain at most 200 characters")
			}
			if existing, ok := p.state.Enrollments[req.EnrollmentID]; req.EnrollmentID != "" && ok && existing.Actor != req.Actor {
				return nil, bad(409, "enrollment ID belongs to another actor")
			}
			a, err := p.registerMode(req.Actor, req.Name, req.Parent, req.Lineage, req.Mode, prefixes...)
			if err != nil {
				return nil, err
			}
			result := map[string]any{"agent": a}
			if req.EnrollmentID != "" {
				boundary, known := p.state.Enrollments[req.EnrollmentID]
				if !known {
					// Capture canonical first observation before publishing a new local
					// enrollment. A lost response retries the exact durable boundary.
					p.repair(true)
					if err := p.feedError(req.Actor); err != nil {
						return nil, err
					}
					boundary = Enrollment{Actor: req.Actor, Sequence: p.state.Sequence}
					p.state.Enrollments[req.EnrollmentID] = boundary
					if err := p.save(); err != nil {
						return nil, err
					}
				}
				result["enrollment_seq"] = boundary.Sequence
			}
			return result, nil
		}
	case "agents":
		if len(parts) == 2 && get {
			return p.agentDirectory(r)
		}
	case "operator-dm":
		if len(parts) == 2 && post {
			var req memberRequest
			if err := decode(r, &req); err != nil {
				return nil, err
			}
			return p.agentOperatorDM(req)
		}
	case "channels":
		if len(parts) == 2 && get {
			return p.channelDirectory(actor, r)
		}
	case "conversations":
		if len(parts) == 3 && parts[2] == "directory" && get {
			return p.managedConversationDirectory(actor, r)
		}
		if len(parts) == 2 {
			if post {
				var req conversationRequest
				if err := decode(r, &req); err != nil {
					return nil, err
				}
				return p.conversation(req)
			}
			if get {
				if _, err := p.requireActor(actor); err != nil {
					return nil, err
				}
				return map[string]any{"conversations": p.conversationSnapshot(actor)}, nil
			}
		}
		if len(parts) == 4 {
			channel := parts[2]
			if get && parts[3] == "attention-boundary" {
				return p.attentionBoundary(actor, channel)
			}
			if post && (parts[3] == "join" || parts[3] == "leave") {
				var req memberRequest
				if err := decode(r, &req); err != nil {
					return nil, err
				}
				return p.membership(channel, parts[3], req)
			}
			if get && (parts[3] == "posts" || parts[3] == "threads") {
				if parts[3] == "threads" {
					return p.conversationThreads(actor, channel, r)
				}
				version, err := requestedReadVersion(r)
				if err != nil {
					return nil, err
				}
				if _, err := p.contentRead(actor, channel, version); err != nil {
					return nil, err
				}
				posts, err := p.canonical(channel)
				if err != nil {
					return nil, err
				}
				limit, err := queryNumber(r, "limit", 100, 1, 1000)
				if err != nil {
					return nil, err
				}
				before := r.URL.Query().Get("before")
				end := len(posts)
				if before != "" {
					beforePost, accessErr := p.authorizedReadPost(actor, before, version)
					if accessErr != nil {
						return nil, accessErr
					}
					if beforePost.ChannelId != channel {
						return nil, bad(400, "before post belongs to another channel")
					}
					end = -1
					for i, item := range posts {
						if item.Id == before {
							end = i
							break
						}
					}
					if end < 0 {
						return nil, bad(404, "before post not found in canonical history")
					}
				}
				page := []map[string]any{}
				start := end
				for start > 0 && len(page) < limit {
					start--
					if userPost(posts[start]) {
						page = append(page, p.postView(posts[start]))
					}
				}
				for i, j := 0, len(page)-1; i < j; i, j = i+1, j-1 {
					page[i], page[j] = page[j], page[i]
				}
				var next any
				if start > 0 && len(page) > 0 {
					next = page[0]["id"]
				}
				return map[string]any{"posts": page, "next_before": next}, nil
			}
		}
	case "posts":
		if len(parts) == 2 && post {
			var req postRequest
			if err := decode(r, &req); err != nil {
				return nil, err
			}
			return p.send(req)
		}
		if len(parts) == 3 && get {
			version, err := requestedReadVersion(r)
			if err != nil {
				return nil, err
			}
			resolve := func(actor, id string) (*model.Post, error) { return p.authorizedReadPost(actor, id, version) }
			if values, present := r.URL.Query()["as_root"]; present {
				if len(values) != 1 || (values[0] != "true" && values[0] != "false") {
					return nil, bad(400, "as_root must be true or false")
				}
				if values[0] == "true" {
					resolve = func(actor, id string) (*model.Post, error) { return p.readThreadRoot(actor, id, version) }
				}
			}
			item, err := resolve(actor, parts[2])
			if err != nil {
				return nil, err
			}
			return map[string]any{"post": p.postView(item)}, nil
		}
	case "threads":
		if len(parts) == 3 && get {
			return p.threadPage(actor, parts[2], r)
		}
	case "operations":
		if len(parts) == 3 && get {
			if _, err := p.requireActor(actor); err != nil {
				return nil, err
			}
			o := p.state.Operations[parts[2]]
			if o == nil {
				return nil, bad(404, "operation not found")
			}
			if o.Actor != actor {
				return nil, bad(403, "operation belongs to another actor")
			}
			if (o.Kind == "post" || o.Kind == "agent_operator_dm") && o.Status != "complete" && o.Status != "failed" {
				if err := p.finishOperation(o); err != nil {
					return nil, err
				}
			}
			return p.operationResult(o)
		}
	case "changes":
		if len(parts) == 2 && get {
			return p.changes(actor, r)
		}
	case "saved":
		if len(parts) == 2 && get {
			version, err := requestedReadVersion(r)
			if err != nil {
				return nil, err
			}
			return p.savedRead(actor, version)
		}
		if len(parts) == 3 && post {
			var req savedRequest
			if err := decode(r, &req); err != nil {
				return nil, err
			}
			version, err := requestedReadVersion(r)
			if err != nil {
				return nil, err
			}
			return p.setSavedRead(parts[2], req, version)
		}
	}
	return nil, bad(404, "API route or method not found")
}

func queryNumber(r *http.Request, name string, fallback, min, max int) (int, error) {
	value := r.URL.Query().Get(name)
	if value == "" {
		return fallback, nil
	}
	n, err := strconv.Atoi(value)
	if err != nil || n < min || n > max {
		return 0, bad(400, "invalid "+name)
	}
	return n, nil
}

func (p *Plugin) changes(actor string, r *http.Request) (any, error) {
	versions := r.URL.Query()["attention_version"]
	if len(versions) != 1 || versions[0] != strconv.Itoa(attentionProtocolVersion) {
		return nil, bad(400, "attention_version=3 is required; update and restart the Murmur client")
	}
	a, err := p.requireActor(actor)
	if err != nil {
		return nil, err
	}
	cursor, parseErr := strconv.ParseInt(r.URL.Query().Get("cursor"), 10, 64)
	if r.URL.Query().Get("cursor") == "" {
		cursor, parseErr = 0, nil
	}
	if parseErr != nil || cursor < 0 || cursor > p.state.Sequence {
		return nil, bad(400, "invalid cursor")
	}
	limit, err := queryNumber(r, "limit", 100, 1, 1000)
	if err != nil {
		return nil, err
	}
	p.repair(false)
	if err := p.feedError(actor); err != nil {
		return nil, err
	}
	generation, parseErr := strconv.ParseInt(r.URL.Query().Get("generation"), 10, 64)
	reset := parseErr != nil || generation != a.Generation
	if reset {
		cursor = 0
	}
	observations := []Observation{}
	for _, item := range p.state.Observed {
		if item.Seq <= cursor {
			continue
		}
		if c := p.state.Conversations[item.ChannelID]; c != nil {
			if grant, ok := c.Grants[actor]; ok && grant.Role == "member" &&
				(grant.TrackAfterSeq == nil || item.FirstSeenSeq > *grant.TrackAfterSeq) {
				observations = append(observations, item)
			}
		}
	}
	sort.Slice(observations, func(i, j int) bool { return observations[i].Seq < observations[j].Seq })
	more := len(observations) > limit
	if more {
		observations = observations[:limit]
	}
	changes := []map[string]any{}
	for _, item := range observations {
		post, appErr := p.API.GetPost(item.PostID)
		if appErr != nil {
			if appErr.StatusCode == 404 {
				cursor = item.Seq
				continue
			}
			return nil, appErr
		}
		if post == nil || post.Id != item.PostID || post.ChannelId != item.ChannelID {
			return nil, bad(503, "canonical post identity or channel mismatch")
		}
		if _, err := p.access(actor, post.ChannelId, false); err != nil {
			return nil, err
		}
		if userPost(post) {
			changes = append(changes, map[string]any{"seq": item.Seq, "first_seen_seq": item.FirstSeenSeq, "post": p.postView(post), "notification_eligible": !item.Historical})
		}
		cursor = item.Seq
	}
	if !more {
		cursor = p.state.Sequence
	}
	return map[string]any{"attention_version": attentionProtocolVersion, "conversations": p.conversationSnapshot(actor),
		"changes": changes, "cursor": cursor, "generation": a.Generation, "more": more, "reset": reset}, nil
}

func (p *Plugin) feedError(actor string) error {
	if p.fault != "" {
		return bad(503, "persistent state unavailable: "+p.fault)
	}
	for channel, text := range p.repairErrors {
		if c := p.state.Conversations[channel]; c != nil && c.Grants[actor].Role == "member" {
			return bad(503, "conversation reconciliation failed: "+text)
		}
	}
	return nil
}

// Called under the same plugin lock as grant reconciliation and feed paging.
// Every page carries the complete access snapshot, not a paged subset.
func (p *Plugin) conversationSnapshot(actor string) []map[string]any {
	conversations := []map[string]any{}
	for _, c := range p.state.Conversations {
		if _, ok := c.Grants[actor]; ok {
			conversations = append(conversations, view(c, actor))
		}
	}
	sort.Slice(conversations, func(i, j int) bool { return conversations[i]["id"].(string) < conversations[j]["id"].(string) })
	return conversations
}
