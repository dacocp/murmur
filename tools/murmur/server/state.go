package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
	"time"

	"github.com/mattermost/mattermost/server/public/model"
)

type Agent struct {
	ID              string `json:"id"`
	Name            string `json:"name"`
	Username        string `json:"username"`
	UserID          string `json:"user_id"`
	Parent          string `json:"parent,omitempty"`
	RegisteredAt    int64  `json:"registered_at"`
	Generation      int64  `json:"generation"`
	LineageComplete bool   `json:"lineage_complete"`
	LineageReason   string `json:"lineage_reason,omitempty"`
	Mode            string `json:"mode,omitempty"`
}

type Enrollment struct {
	Actor    string `json:"actor"`
	Sequence int64  `json:"sequence"`
}

// Enrollment and native lineage are separate: a verified parent edge never
// provisions an account or authorizes that ancestor to use Murmur.
type LineageEdge struct {
	Actor  string `json:"actor"`
	Parent string `json:"parent"`
}

func (e *LineageEdge) UnmarshalJSON(data []byte) error {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	if len(fields) != 2 || fields["actor"] == nil || fields["parent"] == nil {
		return bad(400, "each lineage edge requires actor and explicit parent (null for a root)")
	}
	if err := json.Unmarshal(fields["actor"], &e.Actor); err != nil {
		return err
	}
	if string(fields["parent"]) == "null" {
		e.Parent = ""
		return nil
	}
	return json.Unmarshal(fields["parent"], &e.Parent)
}

type Grant struct {
	Role          string `json:"role"`
	GrantedAt     int64  `json:"granted_at"`
	TrackAfterSeq *int64 `json:"track_after_seq,omitempty"`
}

type Conversation struct {
	ID      string           `json:"id"`
	Name    string           `json:"name"`
	Kind    string           `json:"kind"`
	Grants  map[string]Grant `json:"grants"`
	Left    map[string]bool  `json:"left,omitempty"`
	Open    bool             `json:"open,omitempty"`
	Purpose string           `json:"purpose,omitempty"`
}

type Operation struct {
	ID            string        `json:"id"`
	Actor         string        `json:"actor"`
	Kind          string        `json:"kind"`
	Hash          string        `json:"hash"`
	Status        string        `json:"status"`
	Stage         string        `json:"stage"`
	ChannelID     string        `json:"channel_id,omitempty"`
	RootID        string        `json:"root_id,omitempty"`
	Name          string        `json:"name,omitempty"`
	Members       []string      `json:"members,omitempty"`
	PostID        string        `json:"post_id,omitempty"`
	Error         string        `json:"error,omitempty"`
	AgentID       string        `json:"agent_id,omitempty"`
	BotUserID     string        `json:"bot_user_id,omitempty"`
	HumanUserID   string        `json:"human_user_id,omitempty"`
	Open          bool          `json:"open,omitempty"`
	Purpose       string        `json:"purpose,omitempty"`
	TrackAfterSeq *int64        `json:"track_after_seq,omitempty"`
	Tracking      bool          `json:"tracking,omitempty"`
	Upload        *UploadIntent `json:"upload,omitempty"`
}

type Observation struct {
	Seq          int64  `json:"seq"`
	PostID       string `json:"post_id"`
	ChannelID    string `json:"channel_id"`
	UpdateAt     int64  `json:"update_at"`
	Historical   bool   `json:"historical,omitempty"`
	FirstSeenSeq int64  `json:"first_seen_seq"`
}

// Only identities, grants, operation/file descriptors and canonical references
// are retained here. Message and file bodies remain exclusively in Mattermost.
type State struct {
	TeamID        string                         `json:"team_id"`
	Agents        map[string]*Agent              `json:"agents"`
	Conversations map[string]*Conversation       `json:"conversations"`
	Operations    map[string]*Operation          `json:"operations"`
	Observed      map[string]Observation         `json:"observed"`
	Sequence      int64                          `json:"sequence"`
	Registrations map[string]*RegistrationIntent `json:"registrations,omitempty"`
	Lineage       map[string]string              `json:"lineage"`
	Enrollments   map[string]Enrollment          `json:"enrollments"`
}

func (s *State) init() {
	if s.Agents == nil {
		s.Agents = map[string]*Agent{}
	}
	if s.Conversations == nil {
		s.Conversations = map[string]*Conversation{}
	}
	if s.Operations == nil {
		s.Operations = map[string]*Operation{}
	}
	if s.Observed == nil {
		s.Observed = map[string]Observation{}
	}
	if s.Registrations == nil {
		s.Registrations = map[string]*RegistrationIntent{}
	}
	if s.Lineage == nil {
		s.Lineage = map[string]string{}
	}
	if s.Enrollments == nil {
		s.Enrollments = map[string]Enrollment{}
	}
	for id, a := range s.Agents {
		if _, known := s.Lineage[id]; !known && a.Mode != "manual" && (a.Parent != "" || a.Mode == "native") {
			s.Lineage[id] = a.Parent
		}
		a.LineageComplete, a.LineageReason = lineageCoverage(s.Lineage, id)
	}
	for id, item := range s.Observed {
		if item.FirstSeenSeq == 0 {
			item.FirstSeenSeq = item.Seq
			s.Observed[id] = item
		}
	}
}

func (p *Plugin) save() error {
	return p.persistState(&p.state)
}

func digest(value any) string {
	b, _ := json.Marshal(value)
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func (p *Plugin) requireActor(id string) (*Agent, error) {
	a := p.state.Agents[id]
	if a == nil {
		return nil, bad(403, "actor is not explicitly enrolled")
	}
	return a, nil
}

func (p *Plugin) grant(c *Conversation, actor, role string) {
	p.grantOne(c, actor, role)
	// Traverse the verified forest, including ancestors without enrolled bots.
	for id := range p.state.Agents {
		if id != actor && p.inheritsLineage(id) && p.descendsFrom(id, actor) {
			p.grantOne(c, id, "observer")
		}
	}
}

func (p *Plugin) inheritsLineage(actor string) bool {
	a := p.state.Agents[actor]
	return a != nil && (a.Mode == "native" || (a.Mode == "" && a.Parent != ""))
}

func (p *Plugin) grantOne(c *Conversation, actor, role string) {
	old, exists := c.Grants[actor]
	if !c.Left[actor] && (!exists || (old.Role == "observer" && role == "member")) {
		if !exists {
			old.GrantedAt = time.Now().UnixMilli()
		}
		old.Role = role
		c.Grants[actor] = old
		p.state.Agents[actor].Generation++
	}
}

func (p *Plugin) descendsFrom(actor, ancestor string) bool {
	seen := map[string]bool{}
	for current := actor; current != "" && !seen[current]; {
		seen[current] = true
		parent, known := p.state.Lineage[current]
		if !known {
			return false
		}
		if parent == ancestor {
			return true
		}
		current = parent
	}
	return false
}

func nativeActor(id string) bool {
	return id != "" && len(id) <= 160 && (strings.HasPrefix(id, "codex:") || strings.HasPrefix(id, "claude:"))
}

func lineageCoverage(graph map[string]string, actor string) (bool, string) {
	seen := map[string]bool{}
	for current := actor; current != ""; {
		if seen[current] {
			return false, "native lineage contains a cycle"
		}
		seen[current] = true
		parent, known := graph[current]
		if !known {
			return false, "unverified ancestor: " + current
		}
		current = parent
	}
	return true, ""
}

func (p *Plugin) verifiedLineage(actor, parent string, edges []LineageEdge) (map[string]string, error) {
	if len(edges) > 256 {
		return nil, bad(400, "lineage proof exceeds 256 edges")
	}
	graph := make(map[string]string, len(p.state.Lineage)+len(edges)+1)
	for id, value := range p.state.Lineage {
		graph[id] = value
	}
	for _, edge := range append([]LineageEdge{{Actor: actor, Parent: parent}}, edges...) {
		if !nativeActor(edge.Actor) || (edge.Parent != "" && !nativeActor(edge.Parent)) {
			return nil, bad(400, "invalid native lineage key")
		}
		if previous, known := graph[edge.Actor]; known && previous != edge.Parent {
			return nil, bad(409, "verified native lineage cannot be reparented")
		}
		if pending := p.state.Registrations[edge.Actor]; pending != nil && (pending.Mode == "native" || pending.Parent != "") && pending.Parent != edge.Parent {
			return nil, bad(409, "pending registration conflicts with verified lineage")
		}
		graph[edge.Actor] = edge.Parent
	}
	for actor := range graph {
		seen := map[string]bool{}
		for current := actor; current != ""; {
			if seen[current] {
				return nil, bad(400, "ancestry cycle")
			}
			seen[current] = true
			parent, known := graph[current]
			if !known {
				break
			}
			current = parent
		}
	}
	for _, edge := range edges {
		connected := false
		for current := actor; current != ""; current = graph[current] {
			if current == edge.Actor {
				connected = true
				break
			}
		}
		if !connected {
			return nil, bad(400, "lineage proof contains an unrelated edge")
		}
	}
	return graph, nil
}

func (p *Plugin) publishLineage(graph map[string]string) {
	p.state.Lineage = graph
	for id, a := range p.state.Agents {
		a.LineageComplete, a.LineageReason = lineageCoverage(graph, id)
	}
	for _, c := range p.state.Conversations {
		for actor := range p.state.Agents {
			if !p.inheritsLineage(actor) {
				continue
			}
			for ancestor := graph[actor]; ancestor != ""; ancestor = graph[ancestor] {
				if _, granted := c.Grants[ancestor]; granted {
					p.grant(c, actor, "observer")
					break
				}
			}
		}
	}
}

func (p *Plugin) access(actor, channel string, posting bool) (*Conversation, error) {
	if _, err := p.requireActor(actor); err != nil {
		return nil, err
	}
	c := p.state.Conversations[channel]
	if c == nil {
		return nil, bad(404, "conversation not found")
	}
	g, ok := c.Grants[actor]
	if !ok && !posting {
		open, err := p.managedPublicChannel(c)
		if err != nil {
			return nil, err
		}
		if open {
			return c, nil
		}
	}
	if !ok || (posting && g.Role != "member") {
		return nil, bad(403, "conversation access denied")
	}
	if posting {
		if _, err := p.API.GetChannelMember(channel, p.state.Agents[actor].UserID); err != nil {
			return nil, bad(409, "native membership changed outside Murmur; repair membership before posting")
		}
	}
	return c, nil
}

// Stored conversation kinds retain their original encoding for durable replay.
func conversationKind(c *Conversation) string {
	if c.Kind == "group" {
		return "channel"
	}
	return c.Kind
}

func view(c *Conversation, actor string) map[string]any {
	visibility := "private"
	if c.Kind == "group" {
		visibility = "legacy-private"
		if c.Open {
			visibility = "public"
		}
	}
	result := map[string]any{"id": c.ID, "name": c.Name, "kind": conversationKind(c), "role": nil,
		"open": c.Open, "purpose": c.Purpose, "visibility": visibility}
	if g, ok := c.Grants[actor]; ok {
		result["role"], result["granted_at"] = g.Role, g.GrantedAt
	}
	return result
}

func (p *Plugin) ensureTeam() error {
	if p.state.TeamID != "" {
		return nil
	}
	t, err := p.API.GetTeamByName("murmur")
	if err != nil {
		t, err = p.API.CreateTeam(&model.Team{Name: "murmur", DisplayName: "Murmur", Type: "I"})
		if err != nil {
			return err
		}
	}
	p.state.TeamID = t.Id
	return p.save()
}

func (p *Plugin) register(actor, name, parent string, prefixes ...string) (*Agent, error) {
	return p.registerLineage(actor, name, parent, nil, prefixes...)
}

func (p *Plugin) registerLineage(actor, name, parent string, edges []LineageEdge, prefixes ...string) (*Agent, error) {
	return p.registerMode(actor, name, parent, edges, "native", prefixes...)
}

func (p *Plugin) registerMode(actor, name, parent string, edges []LineageEdge, mode string, prefixes ...string) (*Agent, error) {
	if mode != "native" && mode != "manual" {
		return nil, bad(400, "mode must be native or manual")
	}
	var graph map[string]string
	if mode == "manual" {
		if parent != "" || len(edges) != 0 {
			return nil, bad(400, "manual enrollment cannot publish native ancestry")
		}
		graph = make(map[string]string, len(p.state.Lineage))
		for id, value := range p.state.Lineage {
			graph[id] = value
		}
	} else {
		var lineageErr error
		graph, lineageErr = p.verifiedLineage(actor, parent, edges)
		if lineageErr != nil {
			return nil, lineageErr
		}
	}
	if !nativeActor(actor) {
		return nil, bad(400, "invalid native actor key")
	}
	if name == "" || len([]rune(name)) > 64 {
		return nil, bad(400, "name must contain 1 to 64 characters")
	}
	var prefix *string
	if len(prefixes) > 1 {
		return nil, bad(400, "only one username prefix may be supplied")
	}
	if len(prefixes) == 1 {
		prefix = &prefixes[0]
		if strings.TrimSpace(*prefix) == "" || len([]rune(*prefix)) > 64 {
			return nil, bad(400, "username_prefix must contain 1 to 64 characters of nonblank text")
		}
	}
	if a := p.state.Agents[actor]; a != nil {
		if a.Mode != "" && a.Mode != mode {
			return nil, bad(409, "enrollment mode is immutable")
		}
		if a.Parent != parent {
			return nil, bad(409, "enrolled identity cannot be reparented")
		}
		bot, err := p.syncBotIdentity(actor, a.UserID, name, prefix, false)
		if err != nil {
			return nil, err
		}
		previousName, previousUsername := a.Name, a.Username
		a.Name, a.Username = name, bot.Username
		a.Mode = mode
		p.publishLineage(graph)
		// Persist even when the in-memory name already matches: a prior failed
		// save must never become a successful same-name registration shortcut.
		if err := p.save(); err != nil {
			a.Name, a.Username = previousName, previousUsername
			return nil, err
		}
		if err := p.prepareRegisteredOperatorDM(actor); err != nil {
			return nil, err
		}
		return a, nil
	}
	if err := p.ensureTeam(); err != nil {
		return nil, err
	}
	intent, err := p.registrationBot(actor, name, parent, prefix, mode)
	if err != nil {
		return nil, err
	}
	bot, err := p.syncBotIdentity(actor, intent.UserID, name, prefix, true)
	if err != nil {
		return nil, err
	}
	if _, err := p.API.CreateTeamMember(p.state.TeamID, bot.UserId); err != nil {
		return nil, err
	}
	a := &Agent{ID: actor, Name: name, Username: bot.Username, UserID: bot.UserId, Parent: parent, Mode: mode, RegisteredAt: time.Now().UnixMilli()}
	p.state.Agents[actor] = a
	p.publishLineage(graph)
	delete(p.state.Registrations, actor)
	if err := p.save(); err != nil {
		return nil, err
	}
	if err := p.prepareRegisteredOperatorDM(actor); err != nil {
		return nil, err
	}
	return a, nil
}

func (p *Plugin) newOp(actor, id, kind string, request any) (*Operation, bool, error) {
	if _, err := p.requireActor(actor); err != nil {
		return nil, false, err
	}
	if id == "" || len(id) > 200 {
		return nil, false, bad(400, "op_id is required (maximum 200 characters)")
	}
	if existing := p.state.Operations[id]; existing != nil {
		if existing.Actor != actor || existing.Kind != kind || existing.Hash != digest(request) {
			return nil, false, bad(409, "operation ID is already bound to another immutable request")
		}
		return existing, false, nil
	}
	o := &Operation{ID: id, Actor: actor, Kind: kind, Hash: digest(request), Status: "pending", Stage: "intent"}
	p.state.Operations[id] = o
	return o, true, nil
}

func (p *Plugin) operationResult(o *Operation) (map[string]any, error) {
	result := map[string]any{"status": o.Status}
	if o.Error != "" {
		result["error"] = o.Error
	}
	if o.Kind == "operator_post" && o.Status == "failed" && o.Stage == "rejected" {
		result["dispatch"], result["op_id"] = "rejected", o.ID
	}
	if o.Status == "complete" {
		switch o.Kind {
		case "post", "operator_post":
			// Ownership authorizes recovery metadata even after channel departure.
			// Content still requires the normal current-channel read authorization.
			result["result"] = map[string]string{"post_id": o.PostID, "channel_id": o.ChannelID}
		case "upload", "operator_upload":
			if o.Upload == nil || o.Upload.File == nil {
				return nil, bad(503, "upload operation has no reserved native file")
			}
			result["result"] = map[string]any{"file_id": o.Upload.FileID, "channel_id": o.ChannelID, "file": o.Upload.File}
		case "leave":
			result["result"] = map[string]bool{"ok": true}
		default:
			result["result"] = map[string]string{"channel_id": o.ChannelID}
		}
	}
	return result, nil
}

func (p *Plugin) canonical(channel string) ([]*model.Post, error) {
	posts, err := p.history.Posts(channel)
	if err != nil {
		return nil, err
	}
	for _, post := range posts {
		if post == nil || post.ChannelId != channel {
			return nil, bad(503, "canonical history channel mismatch")
		}
	}
	return posts, nil
}

func userPost(post *model.Post) bool {
	return post.DeleteAt == 0 && !strings.HasPrefix(post.Type, "system_")
}

func (p *Plugin) observe(posts []*model.Post) bool {
	changed := false
	for _, post := range posts {
		if !userPost(post) {
			continue
		}
		old, exists := p.state.Observed[post.Id]
		if !exists || post.UpdateAt > old.UpdateAt {
			p.state.Sequence++
			first := old.FirstSeenSeq
			if first == 0 {
				first = old.Seq
				if !exists {
					first = p.state.Sequence
				}
			}
			p.state.Observed[post.Id] = Observation{Seq: p.state.Sequence, FirstSeenSeq: first, PostID: post.Id, ChannelID: post.ChannelId, UpdateAt: post.UpdateAt, Historical: old.Historical}
			changed = true
		}
	}
	return changed
}

func (p *Plugin) authorizedPost(actor, id string) (*model.Post, error) {
	return p.authorizedReadPost(actor, id, 0)
}

func (p *Plugin) authorizedReadPost(actor, id string, version int) (*model.Post, error) {
	post, err := p.API.GetPost(id)
	if err != nil {
		if err.StatusCode == 403 || err.StatusCode == 404 {
			return nil, bad(err.StatusCode, "post unavailable")
		}
		return nil, err
	}
	if post == nil || post.Id != id {
		return nil, bad(503, "canonical post identity mismatch")
	}
	if post.DeleteAt != 0 {
		return nil, bad(404, "post not found")
	}
	if _, accessErr := p.contentRead(actor, post.ChannelId, version); accessErr != nil {
		return nil, accessErr
	}
	return post, nil
}

func (p *Plugin) postView(post *model.Post) map[string]any {
	author := post.UserId
	for _, a := range p.state.Agents {
		if a.UserID == post.UserId {
			author = a.Name
			break
		}
	}
	if author == post.UserId {
		if u, err := p.API.GetUser(post.UserId); err == nil {
			author = u.Username
		}
	}
	v := map[string]any{"id": post.Id, "channel_id": post.ChannelId, "user_id": post.UserId, "author": author,
		"message": post.Message, "root_id": post.RootId, "create_at": post.CreateAt, "update_at": post.UpdateAt,
		"file_ids": append([]string{}, post.FileIds...), "files": p.postFiles(post)}
	if causal, ok := post.Props["murmur_causal"]; ok {
		v["causal"] = causal
	}
	return v
}
