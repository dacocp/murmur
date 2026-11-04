package main

import (
	"fmt"
	"sort"
	"strings"

	"github.com/mattermost/mattermost/server/public/model"
)

type conversationRequest struct {
	Actor   string   `json:"actor"`
	OpID    string   `json:"op_id"`
	Kind    string   `json:"kind"`
	Name    string   `json:"name,omitempty"`
	Members []string `json:"members"`
	Purpose string   `json:"purpose,omitempty"`
}

type memberRequest struct {
	Actor string `json:"actor"`
	OpID  string `json:"op_id"`
}

type postRequest struct {
	Actor     string   `json:"actor"`
	OpID      string   `json:"op_id"`
	ChannelID string   `json:"channel_id"`
	Message   string   `json:"message"`
	RootID    string   `json:"root_id,omitempty"`
	Causal    any      `json:"causal,omitempty"`
	FileIDs   []string `json:"file_ids,omitempty"`
}

func (p *Plugin) conversation(req conversationRequest) (map[string]any, error) {
	if req.Kind != "dm" && req.Kind != "channel" {
		return nil, bad(400, "kind must be dm or channel")
	}
	unique := map[string]bool{req.Actor: true}
	for _, id := range req.Members {
		if _, err := p.requireActor(id); err != nil {
			return nil, err
		}
		unique[id] = true
	}
	if req.Kind == "dm" && len(unique) != 2 {
		return nil, bad(400, "a DM requires exactly one different peer")
	}
	if req.Kind == "channel" && (req.Name == "" || len([]rune(req.Name)) > 64) {
		return nil, bad(400, "channel name must contain 1 to 64 characters")
	}
	if len([]rune(req.Purpose)) > model.ChannelPurposeMaxRunes || (req.Kind != "channel" && req.Purpose != "") {
		return nil, bad(400, "purpose is only available for channels and must contain at most 250 characters")
	}
	req.Members = nil
	for id := range unique {
		req.Members = append(req.Members, id)
	}
	sort.Strings(req.Members)
	// Keep the persisted intent encoding stable: existing operation hashes and
	// journal stages used "group" before the public contract named it "channel".
	// This happens after validation, so callers cannot submit the legacy kind.
	if req.Kind == "channel" {
		req.Kind = "group"
	}
	o, fresh, err := p.newOp(req.Actor, req.OpID, "conversation", req)
	if err != nil {
		return nil, err
	}
	if fresh {
		o.Name, o.Members = req.Name, req.Members
		o.Open, o.Purpose = req.Kind == "group", req.Purpose
		o.Tracking = req.Kind == "group"
		o.Stage = req.Kind
		if err = p.save(); err != nil {
			return nil, err
		}
	}
	if o.Status == "failed" {
		return nil, bad(409, o.Error)
	}
	if o.Status != "complete" {
		if err = p.finishOperation(o); err != nil {
			return nil, err
		}
	}
	c, err := p.access(req.Actor, o.ChannelID, false)
	if err != nil {
		return nil, err
	}
	return map[string]any{"conversation": view(c, req.Actor)}, nil
}

func (p *Plugin) finishConversation(o *Operation) error {
	if err := p.ensureTeam(); err != nil {
		return err
	}
	channelID := o.ChannelID
	if channelID == "" {
		var channel *model.Channel
		var appErr *model.AppError
		if o.Stage == "dm" {
			channel, appErr = p.API.GetDirectChannel(p.state.Agents[o.Members[0]].UserID, p.state.Agents[o.Members[1]].UserID)
		} else {
			name := "murmur-" + digest(o.ID)[:24]
			channel, appErr = p.API.GetChannelByName(p.state.TeamID, name, false)
			if appErr != nil && appErr.StatusCode == 404 {
				typeValue := model.ChannelTypePrivate
				if o.Open {
					typeValue = model.ChannelTypeOpen
				}
				channel, appErr = p.API.CreateChannel(&model.Channel{TeamId: p.state.TeamID, Name: name, DisplayName: o.Name, Purpose: o.Purpose, Type: typeValue})
			}
		}
		if appErr != nil {
			return appErr
		}
		if channel == nil {
			return bad(409, "native conversation creation returned no channel")
		}
		channelID = channel.Id
	}
	if err := p.verifyConversationChannel(o, channelID); err != nil {
		return err
	}
	if o.ChannelID == "" {
		o.ChannelID = channelID
		if err := p.save(); err != nil {
			return err
		}
	}
	c := p.state.Conversations[o.ChannelID]
	if c == nil {
		name := o.Name
		if o.Stage == "dm" {
			names := []string{}
			for _, id := range o.Members {
				names = append(names, p.state.Agents[id].Name)
			}
			name = strings.Join(names, " / ")
		}
		c = &Conversation{ID: o.ChannelID, Name: name, Kind: o.Stage, Purpose: o.Purpose, Grants: map[string]Grant{}}
		p.state.Conversations[c.ID] = c
		if err := p.save(); err != nil {
			return err
		}
	}
	if c.Kind == "group" {
		if err := p.prepareTracking(o, c); err != nil {
			return err
		}
	}
	for _, actor := range o.Members {
		userID := p.state.Agents[actor].UserID
		if c.Kind == "group" {
			if _, err := p.API.AddChannelMember(c.ID, userID); err != nil {
				return err
			}
		}
		delete(c.Left, actor)
		p.grantMember(c, actor, o.TrackAfterSeq)
		// A crash after native addition replays the idempotent addition. A crash
		// after this save retains descendant grants even if the member leaves.
		if err := p.save(); err != nil {
			return err
		}
	}
	c.Open = o.Open
	o.Status, o.Error = "complete", ""
	return p.save()
}

// Resolve every creation/recovery response against the canonical channel before
// persisting its ID or granting access. An ID alone cannot attest to the intended
// native pair or operation-owned channel.
func (p *Plugin) verifyConversationChannel(o *Operation, id string) error {
	if !model.IsValidId(id) {
		return bad(409, "native conversation returned an invalid channel identity")
	}
	native, appErr := p.API.GetChannel(id)
	if appErr != nil {
		return appErr
	}
	if native == nil || native.Id != id || native.DeleteAt != 0 {
		return bad(409, "canonical conversation identity mismatch")
	}
	if o.Stage == "group" {
		expected := model.ChannelTypePrivate
		if o.Open {
			expected = model.ChannelTypeOpen
		}
		if native.Name != "murmur-"+digest(o.ID)[:24] || native.TeamId != p.state.TeamID || native.Type != expected {
			return bad(409, "native channel does not match its creation intent")
		}
		return nil
	}
	if o.Stage != "dm" || len(o.Members) != 2 {
		return bad(409, "invalid native DM creation intent")
	}
	users := []string{p.state.Agents[o.Members[0]].UserID, p.state.Agents[o.Members[1]].UserID}
	sort.Strings(users)
	if native.Type != model.ChannelTypeDirect || native.Name != strings.Join(users, "__") || users[0] == users[1] {
		return bad(409, "native DM does not match its intended pair")
	}
	members, appErr := p.API.GetChannelMembers(id, 0, 3)
	if appErr != nil {
		return appErr
	}
	if len(members) != 2 || members[0].UserId == members[1].UserId {
		return bad(409, "native DM membership is inconsistent")
	}
	for _, member := range members {
		if member.ChannelId != id || (member.UserId != users[0] && member.UserId != users[1]) {
			return bad(409, "native DM membership does not match its intended pair")
		}
	}
	return nil
}

func (p *Plugin) membership(channel, kind string, req memberRequest) (map[string]any, error) {
	if _, err := p.requireActor(req.Actor); err != nil {
		return nil, err
	}
	c, err := p.access(req.Actor, channel, false)
	if err != nil {
		// An explicit rejoin can restore a previously relinquished channel.
		// Knowing an arbitrary private-channel ID never grants admission.
		previous := p.state.Conversations[channel]
		if kind == "join" && previous != nil && previous.Kind == "group" && previous.Left[req.Actor] {
			c, err = previous, nil
		}
	}
	if err != nil {
		// Retrying a completed leave must still return its durable result.
		if existing := p.state.Operations[req.OpID]; kind != "leave" || existing == nil || existing.Actor != req.Actor || existing.Kind != "leave" || existing.ChannelID != channel {
			return nil, err
		}
	}
	if kind == "join" && c.Kind != "group" {
		return nil, bad(403, "inherited DMs remain read-only; open your own DM")
	}
	if req.OpID == "" || len(req.OpID) > 200 {
		return nil, bad(400, "op_id is required (maximum 200 characters)")
	}
	if p.state.Operations[req.OpID] == nil {
		// A newer command cannot overtake an uncertain native membership
		// effect. Complete the older journal before accepting a fresh intent.
		if err := p.settleMembership(req.Actor, channel); err != nil {
			return nil, err
		}
	}
	o, fresh, err := p.newOp(req.Actor, req.OpID, kind, struct {
		Channel string
		Request memberRequest
	}{channel, req})
	if err != nil {
		return nil, err
	}
	if fresh {
		o.ChannelID = channel
		o.Tracking = kind == "join"
		if err = p.save(); err != nil {
			return nil, err
		}
	}
	if o.Status == "failed" {
		return nil, bad(409, o.Error)
	}
	if o.Status != "complete" {
		if err = p.finishOperation(o); err != nil {
			return nil, err
		}
	}
	if kind == "leave" {
		return map[string]any{"ok": true}, nil
	}
	return map[string]any{"conversation": view(p.state.Conversations[channel], req.Actor)}, nil
}

func (p *Plugin) settleMembership(actor, channel string) error {
	for _, earlier := range p.state.Operations {
		if earlier.Status == "complete" || earlier.Status == "failed" {
			continue
		}
		var err error
		switch earlier.Kind {
		case "operator_dm", "agent_operator_dm":
			if earlier.AgentID != actor {
				continue
			}
			if earlier.ChannelID != channel {
				if earlier.ChannelID != "" || p.state.Conversations[channel].Kind != "dm" {
					continue
				}
				native, appErr := p.API.GetChannel(channel)
				if appErr != nil {
					return bad(409, "cannot resolve earlier DM opening: "+appErr.Error())
				}
				if native.Type != "D" || native.Name != operatorPairName(earlier) {
					continue
				}
			}
			err = p.finishOperation(earlier)
		case "join", "leave":
			if earlier.Actor != actor || earlier.ChannelID != channel {
				continue
			}
			err = p.finishOperation(earlier)
		case "conversation":
			participant := false
			for _, member := range earlier.Members {
				if member == actor {
					participant = true
					break
				}
			}
			if !participant {
				continue
			}
			if earlier.ChannelID != channel {
				// A lost direct-channel response can leave a new journal without
				// its already-existing channel ID. Match the native fixed pair;
				// unrelated conversations must not block this actor's departure.
				if earlier.ChannelID != "" || earlier.Stage != "dm" || p.state.Conversations[channel].Kind != "dm" {
					continue
				}
				native, appErr := p.API.GetChannel(channel)
				if appErr != nil {
					return bad(409, "cannot resolve earlier membership operation: "+appErr.Error())
				}
				users := []string{}
				for _, member := range earlier.Members {
					users = append(users, p.state.Agents[member].UserID)
				}
				sort.Strings(users)
				if native.Type != "D" || native.Name != strings.Join(users, "__") {
					continue
				}
			}
			err = p.finishOperation(earlier)
		default:
			continue
		}
		if (err != nil && earlier.Status != "failed") || (earlier.Status != "complete" && earlier.Status != "failed") {
			return bad(409, "earlier membership operation "+earlier.ID+" is still pending; retry after reconciliation")
		}
	}
	return nil
}

func (p *Plugin) finishMembership(o *Operation) error {
	c := p.state.Conversations[o.ChannelID]
	if c == nil {
		return fmt.Errorf("journal references missing conversation")
	}
	a := p.state.Agents[o.Actor]
	if o.Kind == "join" {
		if c.Open {
			open, err := p.managedPublicChannel(c)
			if err != nil {
				return err
			}
			if !open {
				return bad(409, "managed public channel changed outside Murmur")
			}
		}
		if c.Grants[o.Actor].Role != "member" {
			if err := p.prepareTracking(o, c); err != nil {
				return err
			}
		}
		if _, err := p.API.AddChannelMember(c.ID, a.UserID); err != nil {
			return err
		}
		delete(c.Left, o.Actor)
		p.grantMember(c, o.Actor, o.TrackAfterSeq)
	} else {
		// Native DMs have fixed membership. Leaving a DM relinquishes the
		// Murmur grant; Mattermost's UI can still reopen the native DM.
		if c.Kind == "group" {
			if _, err := p.API.GetChannelMember(c.ID, a.UserID); err == nil {
				if err = p.API.DeleteChannelMember(c.ID, a.UserID); err != nil {
					return err
				}
			} else if err.StatusCode != 404 {
				return err
			}
		}
		delete(c.Grants, o.Actor)
		if c.Left == nil {
			c.Left = map[string]bool{}
		}
		c.Left[o.Actor] = true
		a.Generation++
	}
	o.Status, o.Error = "complete", ""
	return p.save()
}

func (p *Plugin) send(req postRequest) (map[string]any, error) {
	ids, err := canonicalFileIDs(req.FileIDs)
	if err != nil {
		return nil, err
	}
	req.FileIDs = ids
	if (strings.TrimSpace(req.Message) == "" && len(req.FileIDs) == 0) || len([]rune(req.Message)) > 16000 {
		return nil, bad(400, "message requires text or attachments, with at most 16000 characters")
	}
	if _, err := p.access(req.Actor, req.ChannelID, true); err != nil {
		return nil, err
	}
	if req.RootID != "" {
		root, err := p.threadRoot(req.Actor, req.RootID)
		if err != nil {
			return nil, err
		}
		if root.RootId != "" || root.ChannelId != req.ChannelID {
			return nil, bad(400, "thread root must be a root in the requested channel")
		}
	}
	o, fresh, err := p.newOp(req.Actor, req.OpID, "post", req)
	if err != nil {
		return nil, err
	}
	if fresh {
		if err = p.validatePostFiles(req.Actor, p.state.Agents[req.Actor].UserID, req.ChannelID, false, req.FileIDs); err != nil {
			delete(p.state.Operations, o.ID) // No native effect or journal was published.
			return nil, err
		}
		o.ChannelID, o.RootID = req.ChannelID, req.RootID
		o.Stage = "creating"
		if err = p.save(); err != nil {
			return nil, err
		}
		props := model.StringInterface{"murmur_op": o.ID, "murmur_actor": o.Actor, "murmur_hash": o.Hash}
		if req.Causal != nil {
			props["murmur_causal"] = req.Causal
		}
		post, appErr := p.API.CreatePost(&model.Post{ChannelId: req.ChannelID, UserId: p.state.Agents[req.Actor].UserID, RootId: req.RootID, Message: req.Message, FileIds: append([]string(nil), req.FileIDs...), Props: props})
		if appErr != nil {
			o.Status, o.Error = "uncertain", "create response failed; canonical reconciliation required"
			if err = p.save(); err != nil {
				return nil, err
			}
			if err = p.recoverPost(o); err != nil {
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
			if err = p.save(); err != nil {
				return nil, err
			}
		}
	} else if o.Status != "complete" && o.Status != "failed" {
		if err = p.finishOperation(o); err != nil {
			return nil, err
		}
	}
	if o.Status != "complete" {
		return nil, bad(409, "outbound operation is uncertain; inspect operations/"+o.ID+"; do not repost")
	}
	post, err := p.authorizedPost(req.Actor, o.PostID)
	if err != nil {
		return nil, err
	}
	return map[string]any{"post": p.postView(post)}, nil
}

func (p *Plugin) recoverPost(o *Operation) error {
	posts, err := p.canonical(o.ChannelID)
	if err != nil {
		return err
	}
	found := ""
	for _, post := range posts {
		if p.matchesPostOperation(o, post) {
			if found != "" && found != post.Id {
				o.Status, o.Error = "uncertain", "duplicate canonical echoes; operator review required"
				return p.save()
			}
			found = post.Id
		}
	}
	if found == "" {
		o.Status, o.Error = "uncertain", "no canonical echo yet; automatic reposting is disabled"
	} else {
		o.PostID, o.Status, o.Error = found, "complete", ""
	}
	p.observe(posts)
	return p.save()
}

// Correlate the actual canonical sender/root/content, not caller-copyable props.
// Reconstructing the immutable hash avoids retaining a second message body.
func (p *Plugin) matchesPostOperation(o *Operation, post *model.Post) bool {
	if !userPost(post) || post.ChannelId != o.ChannelID || post.RootId != o.RootID {
		return false
	}
	switch o.Kind {
	case "post":
		a := p.state.Agents[o.Actor]
		if a == nil || post.UserId != a.UserID || post.Props["murmur_op"] != o.ID || post.Props["murmur_actor"] != o.Actor || post.Props["murmur_hash"] != o.Hash {
			return false
		}
		return digest(postRequest{Actor: o.Actor, OpID: o.ID, ChannelID: post.ChannelId, Message: post.Message, RootID: post.RootId, Causal: post.Props["murmur_causal"], FileIDs: post.FileIds}) == o.Hash && p.postFileBindingsMatch(post)
	case "operator_post":
		if post.UserId != o.Actor || post.Props["murmur_operator_op"] != o.ID || post.Props["murmur_operator_user"] != o.Actor || post.Props["murmur_operator_hash"] != o.Hash {
			return false
		}
		return digest(operatorPostRequest{OpID: o.ID, ChannelID: post.ChannelId, RootID: post.RootId, Message: post.Message, FileIDs: post.FileIds}) == o.Hash && p.postFileBindingsMatch(post)
	}
	return false
}

func terminalPolicyError(err error) bool {
	status := 0
	switch failure := err.(type) {
	case *httpError:
		status = failure.status
	case *model.AppError:
		status = failure.StatusCode
	}
	return status == 400 || status == 403 || status == 404 || status == 409 || status == 410
}

func (p *Plugin) finishOperation(o *Operation) error {
	if o.Status == "complete" || o.Status == "failed" {
		return nil
	}
	var err error
	switch o.Kind {
	case "upload", "operator_upload":
		// Upload lookup/retry checks the reserved native file outside p.mu.
		// Never resume bytes or perform file I/O from the locked repair sweep.
		return nil
	case "post":
		err = p.recoverPost(o)
	case "operator_post":
		err = p.recoverOperatorPost(o)
	case "operator_dm", "agent_operator_dm":
		err = p.finishOperatorDM(o)
	case "conversation":
		err = p.finishConversation(o)
	case "join", "leave":
		err = p.finishMembership(o)
	default:
		err = bad(409, "unsupported journal operation")
	}
	if err == nil || p.fault != "" {
		return err
	}
	status := o.Status
	if o.Kind == "post" || o.Kind == "operator_post" {
		// A failed read cannot establish whether a previous native create ran.
		status = "uncertain"
	} else if terminalPolicyError(err) {
		status = "failed"
	}
	if o.Error != err.Error() || o.Status != status {
		o.Status, o.Error = status, err.Error()
		if saveErr := p.save(); saveErr != nil {
			return saveErr
		}
	}
	return err
}

func (p *Plugin) reconcile() {
	if p.fault != "" {
		return
	}
	p.lastErr = ""
	p.repairErrors = map[string]string{}
	p.workspaceErrors = map[string]string{}
	for _, o := range p.state.Operations {
		if o.Status == "complete" || o.Status == "failed" {
			continue
		}
		_ = p.finishOperation(o)
		if p.fault != "" {
			p.lastErr = p.fault
			return
		}
	}
	changed := false
	for id := range p.state.Conversations {
		posts, err := p.canonical(id)
		if err != nil {
			p.repairErrors[id] = err.Error()
			p.lastErr = "one or more managed conversations require canonical repair"
			continue
		}
		changed = p.observe(posts) || changed
		p.observeWorkspacePosts(posts)
	}
	if changed {
		if err := p.save(); err != nil {
			p.lastErr = err.Error()
		}
	}
}
