package main

import (
	"mime"
	"net/http"
	"sort"
	"strings"

	"github.com/mattermost/mattermost/server/public/model"
)

// Mattermost's plugin HTTP proxy supplies this authenticated identity. Human
// authentication never enters the coordinator routes or selects an agent actor.
func (p *Plugin) serveOperator(w http.ResponseWriter, r *http.Request, path string) {
	w.Header().Set("Cache-Control", "private, no-store")
	userID := r.Header.Get("Mattermost-User-Id")
	if userID == "" {
		writeError(w, 401, "Mattermost login required")
		return
	}
	user, err := p.operatorUser(userID)
	if err != nil {
		status := 502
		if failure, ok := err.(*httpError); ok {
			status = failure.status
		}
		writeError(w, status, err.Error())
		return
	}
	if expected := r.Header.Get("X-Murmur-User-Id"); expected != "" && expected != userID {
		writeError(w, 409, "operator session changed; reload before continuing")
		return
	}
	if p.fault != "" {
		writeError(w, 503, "persistent state unavailable: "+p.fault)
		return
	}
	result, err := p.operatorRoute(strings.Split(strings.Trim(path, "/"), "/"), r, user)
	if err != nil {
		if rejection, ok := err.(*operatorPostRejection); ok {
			w.WriteHeader(rejection.status)
			result, _ := p.operationResult(rejection.operation)
			writeJSON(w, result)
			return
		}
		if failure, ok := err.(*httpError); ok {
			writeError(w, failure.status, failure.Error())
		} else {
			writeError(w, 502, err.Error())
		}
		return
	}
	writeJSON(w, result)
}

func (p *Plugin) operatorRoute(parts []string, r *http.Request, user *model.User) (any, error) {
	if len(parts) < 3 || parts[0] != "operator" || parts[1] != "v1" {
		return nil, bad(404, "operator API route not found")
	}
	get := r.Method == http.MethodGet
	switch parts[2] {
	case "workspace":
		return p.workspaceRoute(parts[3:], r, user)
	case "bootstrap":
		if len(parts) == 3 && get {
			return p.operatorBootstrap(user)
		}
	case "your-messages":
		if len(parts) == 3 && get {
			return p.operatorYourMessages(user, r)
		}
	case "agent-activity":
		if len(parts) == 3 && get {
			followed, err := p.operatorFollowedRoots(user.Id)
			if err != nil {
				return nil, err
			}
			return p.operatorAgentActivity(user, followed)
		}
	case "search":
		if len(parts) == 3 && get {
			return p.operatorSearch(r)
		}
	case "context":
		if len(parts) == 4 && get {
			return p.operatorContext(parts[3])
		}
	case "conversations":
		if len(parts) == 5 && parts[4] == "board" && get {
			return p.operatorBoard(parts[3], r)
		}
		if len(parts) == 5 && parts[4] == "posts" && get {
			return p.operatorPage(parts[3], r)
		}
	case "threads":
		if len(parts) == 4 && get {
			root, err := p.operatorPost(parts[3])
			if err != nil {
				return nil, err
			}
			if !discussionRoot(root) {
				return nil, bad(400, "requested post is a reply, not a thread root")
			}
			return p.canonicalThreadPage(root, r, 50, p.operatorPost)
		}
	case "posts":
		if len(parts) == 3 && r.Method == http.MethodPost {
			mediaType, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
			if err != nil || mediaType != "application/json" {
				return nil, bad(415, "Content-Type must be application/json")
			}
			var req operatorPostRequest
			if err := decode(r, &req); err != nil {
				return nil, err
			}
			return p.operatorSend(user.Id, req)
		}
	case "dms":
		if len(parts) == 3 && r.Method == http.MethodPost {
			mediaType, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
			if err != nil || mediaType != "application/json" {
				return nil, bad(415, "Content-Type must be application/json")
			}
			var req operatorDMRequest
			if err := decode(r, &req); err != nil {
				return nil, err
			}
			return p.operatorDM(user.Id, req)
		}
	case "operations":
		if len(parts) == 4 && get {
			o := p.state.Operations[parts[3]]
			if o == nil {
				return nil, bad(404, "operation not found")
			}
			if (o.Kind != "operator_post" && o.Kind != "operator_dm") || o.Actor != user.Id {
				return nil, bad(403, "operation belongs to another sender")
			}
			if o.Status != "complete" && o.Status != "failed" {
				recover := p.recoverOperatorPost
				if o.Kind == "operator_dm" {
					recover = p.finishOperatorDM
				}
				if err := recover(o); err != nil {
					return nil, err
				}
			}
			return p.operationResult(o)
		}
	}
	return nil, bad(404, "operator API route or method not found")
}

func (p *Plugin) operatorChannel(id string) (*Conversation, error) {
	c := p.state.Conversations[id]
	if c == nil {
		return nil, bad(404, "managed conversation not found")
	}
	return c, nil
}

func (p *Plugin) operatorPost(id string) (*model.Post, error) {
	post, appErr := p.API.GetPost(id)
	if appErr != nil {
		if appErr.StatusCode == 403 || appErr.StatusCode == 404 {
			return nil, bad(appErr.StatusCode, "post unavailable")
		}
		return nil, appErr
	}
	if _, err := p.operatorChannel(post.ChannelId); err != nil {
		return nil, err
	}
	if !userPost(post) {
		return nil, bad(404, "post unavailable")
	}
	return post, nil
}

func (p *Plugin) operatorBootstrap(user *model.User) (map[string]any, error) {
	agents := []map[string]string{}
	for _, a := range p.state.Agents {
		agents = append(agents, map[string]string{"id": a.ID, "name": a.Name, "user_id": a.UserID, "username": a.Username})
	}
	sort.Slice(agents, func(i, j int) bool { return agents[i]["id"] < agents[j]["id"] })
	conversations := []map[string]any{}
	for _, c := range p.state.Conversations {
		members, observers := []string{}, []string{}
		for actor, grant := range c.Grants {
			if grant.Role == "member" {
				members = append(members, actor)
			} else if grant.Role == "observer" {
				observers = append(observers, actor)
			}
		}
		sort.Strings(members)
		sort.Strings(observers)
		name := c.Name
		var dmUsers []string
		if c.Kind == "dm" {
			var err error
			name, dmUsers, err = p.operatorDMIdentity(c.ID)
			if err != nil {
				return nil, err
			}
		}
		entry := map[string]any{"id": c.ID, "kind": conversationKind(c), "name": name, "members": members,
			"observers": observers, "description": c.Purpose, "last_activity": int64(0), "preview": "", "author": ""}
		if dmUsers != nil {
			entry["dm_user_ids"] = dmUsers
		}
		posts, err := p.canonical(c.ID)
		if err != nil {
			return nil, err
		}
		for i := len(posts) - 1; i >= 0; i-- {
			if post := posts[i]; userPost(post) {
				preview := []rune(postPreview(post))
				if len(preview) > 160 {
					preview = append(preview[:160], '…')
				}
				entry["last_activity"], entry["preview"], entry["author"] = post.CreateAt, string(preview), p.postView(post)["author"]
				break
			}
		}
		conversations = append(conversations, entry)
	}
	sort.Slice(conversations, func(i, j int) bool {
		a, b := conversations[i]["last_activity"].(int64), conversations[j]["last_activity"].(int64)
		if a == b {
			return conversations[i]["id"].(string) < conversations[j]["id"].(string)
		}
		return a > b
	})
	_, boardAvailable := p.history.(rootBoardHistory)
	_, searchAvailable := p.history.(operatorSearchHistory)
	_, directoryAvailable := p.history.(operatorDirectoryHistory)
	return map[string]any{"user": map[string]string{"id": user.Id, "username": user.Username}, "agents": agents, "conversations": conversations,
		"capabilities": map[string]any{"root_board": boardAvailable, "exact_context": true, "full_history_search": searchAvailable, "own_messages": directoryAvailable, "agent_activity": directoryAvailable, "personal_workspace": true, "attachments": p.attachmentCapability()}}, nil
}

// A DM's fixed native pair survives either participant relinquishing its
// Murmur grant. Grants determine filters, not the conversation's identity.
func (p *Plugin) operatorDMName(id string) (string, error) {
	name, _, err := p.operatorDMIdentity(id)
	return name, err
}

func (p *Plugin) operatorDMIdentity(id string) (string, []string, error) {
	channel, appErr := p.API.GetChannel(id)
	if appErr != nil {
		return "", nil, appErr
	}
	pair := strings.Split(channel.Name, "__")
	if channel.Type != model.ChannelTypeDirect || len(pair) != 2 || pair[0] == pair[1] || !model.IsValidId(pair[0]) || !model.IsValidId(pair[1]) {
		return "", nil, bad(409, "managed DM has no valid native pair")
	}
	names := []string{}
	for _, userID := range pair {
		name := ""
		for _, a := range p.state.Agents {
			if a.UserID == userID {
				name = a.Name
				break
			}
		}
		if name == "" {
			user, appErr := p.API.GetUser(userID)
			if appErr != nil {
				return "", nil, appErr
			}
			name = user.Username
		}
		names = append(names, name)
	}
	return strings.Join(names, " / "), pair, nil
}

func (p *Plugin) operatorPage(channel string, r *http.Request) (map[string]any, error) {
	if _, err := p.operatorChannel(channel); err != nil {
		return nil, err
	}
	limit, err := queryNumber(r, "limit", 50, 1, 1000)
	if err != nil {
		return nil, err
	}
	items, err := p.canonical(channel)
	if err != nil {
		return nil, err
	}
	posts := []*model.Post{}
	for _, post := range items {
		if post.ChannelId == channel && userPost(post) {
			posts = append(posts, post)
		}
	}
	end := len(posts)
	if before := r.URL.Query().Get("before"); before != "" {
		cursor, err := p.operatorPost(before)
		if err != nil {
			return nil, err
		}
		if cursor.ChannelId != channel {
			return nil, bad(400, "before post belongs to another channel")
		}
		end = -1
		for i, post := range posts {
			if post.Id == before {
				end = i
				break
			}
		}
		if end < 0 {
			return nil, bad(404, "before post not found in canonical history")
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
	return map[string]any{"posts": page, "next_before": next}, nil
}

type operatorPostRequest struct {
	OpID      string   `json:"op_id"`
	ChannelID string   `json:"channel_id"`
	RootID    string   `json:"root_id,omitempty"`
	Message   string   `json:"message"`
	FileIDs   []string `json:"file_ids,omitempty"`
}

type operatorPostRejection struct {
	status    int
	operation *Operation
}

func (e *operatorPostRejection) Error() string { return e.operation.Error }

func (p *Plugin) rejectOperatorPost(o *Operation, err error) error {
	o.Status, o.Stage, o.Error = "failed", "rejected", err.Error()
	if saveErr := p.save(); saveErr != nil {
		return saveErr
	}
	status := 502
	if failure, ok := err.(*httpError); ok {
		status = failure.status
	}
	return &operatorPostRejection{status: status, operation: o}
}

func (p *Plugin) operatorSend(userID string, req operatorPostRequest) (map[string]any, error) {
	if req.OpID == "" || len(req.OpID) > 200 {
		return nil, bad(400, "op_id is required (maximum 200 characters)")
	}
	ids, err := canonicalFileIDs(req.FileIDs)
	if err != nil {
		return nil, err
	}
	req.FileIDs = ids
	o := p.state.Operations[req.OpID]
	if o != nil {
		if o.Actor != userID || o.Kind != "operator_post" || o.Hash != digest(req) {
			return nil, bad(409, "operation ID is already bound to another immutable request")
		}
		if o.Status == "failed" && o.Stage == "rejected" {
			return nil, &operatorPostRejection{status: 409, operation: o}
		}
		if o.Status != "complete" && o.Status != "failed" {
			if err := p.recoverOperatorPost(o); err != nil {
				return nil, err
			}
		}
	} else {
		o = &Operation{ID: req.OpID, Actor: userID, Kind: "operator_post", Hash: digest(req),
			Status: "pending", Stage: "validating", ChannelID: req.ChannelID, RootID: req.RootID}
		p.state.Operations[o.ID] = o
		// Persist rejection with the same immutable identity. Only this explicit
		// terminal marker proves a retry can never dispatch the rejected payload.
		if (strings.TrimSpace(req.Message) == "" && len(req.FileIDs) == 0) || len([]rune(req.Message)) > 16000 {
			return nil, p.rejectOperatorPost(o, bad(400, "message requires text or attachments, with at most 16000 characters"))
		}
		if _, err := p.operatorChannel(req.ChannelID); err != nil {
			return nil, p.rejectOperatorPost(o, err)
		}
		if req.RootID != "" {
			root, err := p.operatorPost(req.RootID)
			if err != nil {
				return nil, p.rejectOperatorPost(o, err)
			}
			if root.ChannelId != req.ChannelID || !discussionRoot(root) {
				return nil, p.rejectOperatorPost(o, bad(400, "thread root must be a discussion root in the requested channel"))
			}
		}
		if err := p.validatePostFiles(userID, userID, req.ChannelID, true, req.FileIDs); err != nil {
			return nil, p.rejectOperatorPost(o, err)
		}
		o.Stage = "creating"
		if err := p.save(); err != nil {
			return nil, err
		}
		post, appErr := p.API.CreatePost(&model.Post{UserId: userID, ChannelId: req.ChannelID, RootId: req.RootID, Message: req.Message, FileIds: append([]string(nil), req.FileIDs...),
			Props: model.StringInterface{"murmur_operator_op": o.ID, "murmur_operator_user": userID, "murmur_operator_hash": o.Hash}})
		if appErr != nil {
			o.Status, o.Error = "uncertain", "create response failed; canonical reconciliation required"
			if err := p.save(); err != nil {
				return nil, err
			}
			if err := p.recoverOperatorPost(o); err != nil {
				return nil, err
			}
		} else {
			o.PostID = post.Id
			if p.matchesPostOperation(o, post) {
				o.Status = "complete"
			} else {
				o.Status, o.Error = "uncertain", "canonical post or attachment set differs from immutable send intent; do not repost"
			}
			p.observe([]*model.Post{post})
			if err := p.save(); err != nil {
				return nil, err
			}
		}
	}
	if o.Status != "complete" {
		return nil, bad(409, "outbound operation is uncertain; inspect operations/"+o.ID+"; do not repost")
	}
	post, err := p.operatorPost(o.PostID)
	if err != nil {
		return nil, err
	}
	return map[string]any{"post": p.postView(post)}, nil
}

// The human identity is a real Mattermost user, not a provisioned native actor.
// Canonical correlation verifies author, channel, root and immutable content as
// well as props, so a bot cannot spoof recovery by copying the props.
func (p *Plugin) recoverOperatorPost(o *Operation) error {
	if _, err := p.operatorChannel(o.ChannelID); err != nil {
		return err
	}
	posts, err := p.canonical(o.ChannelID)
	if err != nil {
		return err
	}
	found := ""
	for _, post := range posts {
		if !p.matchesPostOperation(o, post) {
			continue
		}
		if found != "" && found != post.Id {
			o.Status, o.Error = "uncertain", "duplicate canonical echoes; operator review required"
			return p.save()
		}
		found = post.Id
	}
	if found == "" {
		o.Status, o.Error = "uncertain", "no canonical echo yet; automatic reposting is disabled"
	} else {
		o.PostID, o.Status, o.Error = found, "complete", ""
	}
	return p.save()
}
