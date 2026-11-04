package main

import (
	"context"
	"encoding/json"

	"mime"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/mattermost/mattermost/server/public/model"
)

const workspaceIndexKey = "operator-workspaces-v1"

type workspaceInterval struct {
	At      int64    `json:"at"`
	Enabled bool     `json:"enabled"`
	Before  []string `json:"before,omitempty"`
}
type workspaceFollow struct {
	Intervals []workspaceInterval `json:"intervals"`
}
type workspaceItem struct {
	PostID    string   `json:"post_id"`
	ChannelID string   `json:"channel_id"`
	RootID    string   `json:"root_id"`
	Reasons   []string `json:"reasons"`
	Sequence  uint64   `json:"sequence"`
	Revision  uint64   `json:"revision"`
	Handled   bool     `json:"handled"`
	UndoToken string   `json:"undo_token,omitempty"`
}
type workspaceSave struct {
	PostID string `json:"post_id"`
	Saved  bool   `json:"saved"`
	Note   string `json:"note"`
}
type humanWorkspace struct {
	Version     int                        `json:"version"`
	Revision    uint64                     `json:"revision"`
	StartedAt   int64                      `json:"started_at"`
	Baseline    []string                   `json:"baseline"`
	Sequence    uint64                     `json:"sequence"`
	Favorites   map[string]bool            `json:"favorites"`
	Follows     map[string]workspaceFollow `json:"follows"`
	Notes       map[string]string          `json:"notes"`
	Items       map[string]*workspaceItem  `json:"items"`
	Seen        map[string]bool            `json:"seen"`
	Gaps        map[string]bool            `json:"gaps"`
	PendingSave *workspaceSave             `json:"pending_saved,omitempty"`
}

func (p *Plugin) workspaceNow() int64 {
	if p.repairClock != nil {
		return p.repairClock().UnixMilli()
	}
	return model.GetMillis()
}
func workspaceKey(user string) string { return "operator-workspace-v1-" + user }
func (p *Plugin) loadWorkspaceUsers() error {
	data, err := p.API.KVGet(workspaceIndexKey)
	if err != nil {
		return err
	}
	ids := []string{}
	if len(data) > 0 {
		if json.Unmarshal(data, &ids) != nil {
			return bad(503, "human workspace index is unavailable")
		}
	}
	for _, id := range ids {
		if !model.IsValidId(id) {
			return bad(503, "human workspace index is invalid")
		}
	}
	p.workspaceUsers = ids
	p.workspaceReady = true
	return nil
}
func (p *Plugin) loadWorkspace(user string) (*humanWorkspace, error) {
	data, err := p.API.KVGet(workspaceKey(user))
	if err != nil {
		return nil, err
	}
	if len(data) == 0 {
		return nil, nil
	}
	var w humanWorkspace
	if json.Unmarshal(data, &w) != nil || w.Version != 1 || w.Favorites == nil || w.Follows == nil || w.Notes == nil || w.Items == nil {
		return nil, bad(503, "human workspace data is unavailable or incompatible")
	}
	if w.Seen == nil {
		w.Seen = map[string]bool{}
	}
	if w.Gaps == nil {
		w.Gaps = map[string]bool{}
	}
	return &w, nil
}
func (p *Plugin) saveWorkspace(user string, w *humanWorkspace) error {
	data, err := json.Marshal(w)
	if err != nil {
		return err
	}
	if appErr := p.API.KVSet(workspaceKey(user), data); appErr != nil {
		return appErr
	}
	return nil
}
func (p *Plugin) beginWorkspace(user string) (*humanWorkspace, error) {
	if !p.workspaceReady {
		if err := p.loadWorkspaceUsers(); err != nil {
			return nil, err
		}
		if len(p.workspaceUsers) > 0 {
			p.repair(true)
		}
	}
	w, err := p.loadWorkspace(user)
	if err != nil {
		return nil, err
	}
	found := false
	for _, id := range p.workspaceUsers {
		if id == user {
			found = true
		}
	}
	if !found {
		users := append(append([]string{}, p.workspaceUsers...), user)
		data, _ := json.Marshal(users)
		if err := p.API.KVSet(workspaceIndexKey, data); err != nil {
			return nil, err
		}
		p.workspaceUsers = users
	}
	if w == nil {
		w = &humanWorkspace{Version: 1, Revision: 1, StartedAt: p.workspaceNow(), Favorites: map[string]bool{}, Follows: map[string]workspaceFollow{}, Notes: map[string]string{}, Items: map[string]*workspaceItem{}, Seen: map[string]bool{}, Gaps: map[string]bool{}}
		for channel := range p.state.Conversations {
			ids, err := p.workspacePostsAt(channel, w.StartedAt)
			if err != nil {
				return nil, err
			}
			w.Baseline = append(w.Baseline, ids...)
		}
		if err = p.saveWorkspace(user, w); err != nil {
			return nil, err
		}
	}
	return w, nil
}
func workspaceEnabled(f workspaceFollow, at int64, post string) bool {
	enabled := true
	for _, change := range f.Intervals {
		if change.At < at || change.At == at && (post == "" || !workspaceContains(change.Before, post)) {
			enabled = change.Enabled
		}
	}
	return enabled
}
func (p *Plugin) workspaceView(w *humanWorkspace) map[string]any {
	favorites := []string{}
	for id, on := range w.Favorites {
		if on {
			favorites = append(favorites, id)
		}
	}
	sort.Strings(favorites)
	follows := []map[string]any{}
	for id, f := range w.Follows {
		follows = append(follows, map[string]any{"root_id": id, "enabled": workspaceEnabled(f, p.workspaceNow(), "")})
	}
	sort.Slice(follows, func(i, j int) bool { return follows[i]["root_id"].(string) < follows[j]["root_id"].(string) })
	return map[string]any{"version": 1, "revision": w.Revision, "started_at": w.StartedAt, "favorites": favorites, "follows": follows, "pending_saved": w.PendingSave}
}
func (p *Plugin) operatorFollowedRoots(user string) ([]string, error) {
	w, err := p.loadWorkspace(user)
	if err != nil {
		return nil, err
	}
	roots := []string{}
	if w != nil {
		for root, f := range w.Follows {
			if workspaceEnabled(f, p.workspaceNow(), "") {
				roots = append(roots, root)
			}
		}
	}
	sort.Strings(roots)
	return roots, nil
}
func (p *Plugin) workspacePreferences(user string) (map[string]bool, error) {
	prefs, err := p.API.GetPreferencesForUser(user)
	if err != nil {
		return nil, err
	}
	saved := map[string]bool{}
	for _, pref := range prefs {
		if pref.UserId == user && pref.Category == model.PreferenceCategoryFlaggedPost && pref.Value == "true" {
			saved[pref.Name] = true
		}
	}
	return saved, nil
}

// A native save and private note live in separate authorities. Freeze intent
// before native I/O; after uncertainty only canonical preferences can confirm
// completion. A GET never replays a write. Explicit same-intent retry is allowed.
func (p *Plugin) resolveWorkspaceSave(user string, w *humanWorkspace) error {
	if w.PendingSave == nil {
		return nil
	}
	saved, err := p.workspacePreferences(user)
	if err != nil {
		return err
	}
	intent := w.PendingSave
	if saved[intent.PostID] != intent.Saved {
		return nil
	}
	if intent.Saved {
		w.Notes[intent.PostID] = intent.Note
	} else {
		delete(w.Notes, intent.PostID)
	}
	w.PendingSave = nil
	w.Revision++
	return p.saveWorkspace(user, w)
}
func (p *Plugin) workspaceRoute(parts []string, r *http.Request, user *model.User) (any, error) {
	w, err := p.beginWorkspace(user.Id)
	if err != nil {
		return nil, err
	}
	if err = p.resolveWorkspaceSave(user.Id, w); err != nil {
		return nil, err
	}
	if len(parts) == 0 && r.Method == http.MethodGet {
		return p.workspaceView(w), nil
	}
	if len(parts) == 1 && parts[0] == "saved" && r.Method == http.MethodGet {
		return p.workspaceSaved(user.Id, w)
	}
	if len(parts) == 1 && parts[0] == "inbox" && r.Method == http.MethodGet {
		if p.workspaceErrors[user.Id] != "" || p.lastErr != "" {
			return nil, bad(503, "Inbox repair is incomplete; refresh after recovery")
		}
		return p.workspaceInbox(w, r)
	}
	if r.Method != http.MethodPut && r.Method != http.MethodPost {
		return nil, bad(404, "workspace route or method not found")
	}
	media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || media != "application/json" {
		return nil, bad(415, "Content-Type must be application/json")
	}
	if p.workspaceErrors[user.Id] != "" || p.lastErr != "" {
		return nil, bad(503, "personal workspace repair is incomplete")
	}
	var req struct {
		Revision *uint64 `json:"revision"`
		Enabled  *bool   `json:"enabled"`
		Saved    *bool   `json:"saved"`
		Note     string  `json:"note"`
		Token    string  `json:"token"`
	}
	if err = decode(r, &req); err != nil {
		return nil, err
	}
	if len(parts) == 3 && parts[0] == "inbox" && r.Method == http.MethodPost {
		item := w.Items[parts[1]]
		if item == nil {
			return nil, bad(404, "Inbox item unavailable")
		}
		if parts[2] == "handle" {
			if req.Revision == nil || *req.Revision != item.Revision || item.Handled {
				return nil, bad(409, "Inbox item changed; refresh before handling")
			}
			item.Handled = true
			item.Revision++
			item.UndoToken = model.NewId()
			w.Revision++
			if err = p.saveWorkspace(user.Id, w); err != nil {
				return nil, err
			}
			return map[string]any{"post_id": item.PostID, "revision": item.Revision, "undo_token": item.UndoToken}, nil
		}
		if parts[2] == "undo" {
			if req.Token == "" || req.Token != item.UndoToken || !item.Handled {
				return nil, bad(409, "this action can no longer be undone")
			}
			item.Handled = false
			item.Revision++
			item.UndoToken = ""
			w.Revision++
			if err = p.saveWorkspace(user.Id, w); err != nil {
				return nil, err
			}
			return map[string]any{"post_id": item.PostID, "revision": item.Revision, "handled": false}, nil
		}
	}
	if req.Revision == nil || *req.Revision != w.Revision {
		return nil, bad(409, "workspace changed; refresh before editing")
	}
	if len(parts) != 2 || !model.IsValidId(parts[1]) || r.Method != http.MethodPut {
		return nil, bad(404, "workspace route or method not found")
	}
	id := parts[1]
	if w.PendingSave != nil && parts[0] != "saved" {
		return nil, bad(409, "a saved-message change needs recovery before editing")
	}
	switch parts[0] {
	case "favorites":
		if req.Enabled == nil {
			return nil, bad(400, "enabled is required")
		}
		if *req.Enabled {
			if _, err = p.operatorChannel(id); err != nil {
				return nil, err
			}
			w.Favorites[id] = true
		} else {
			delete(w.Favorites, id)
		}
	case "follows":
		if req.Enabled == nil {
			return nil, bad(400, "enabled is required")
		}
		root, err := p.operatorPost(id)
		if err != nil {
			return nil, err
		}
		if !discussionRoot(root) {
			return nil, bad(400, "follow target must be a thread root")
		}
		at := p.workspaceNow()
		before, err := p.workspacePostsAt(root.ChannelId, at)
		if err != nil {
			return nil, err
		}
		follow := w.Follows[id]
		follow.Intervals = append(follow.Intervals, workspaceInterval{At: at, Enabled: *req.Enabled, Before: before})
		w.Follows[id] = follow
	case "saved":
		if req.Saved == nil || !utf8.ValidString(req.Note) || utf8.RuneCountInString(req.Note) > 4000 {
			return nil, bad(400, "saved and a note of at most 4000 characters are required")
		}
		if *req.Saved {
			if _, err = p.operatorContext(id); err != nil {
				return nil, err
			}
		}
		intent := workspaceSave{PostID: id, Saved: *req.Saved, Note: req.Note}
		if w.PendingSave != nil && *w.PendingSave != intent {
			return nil, bad(409, "retry the pending saved-message change before another edit")
		}
		if w.PendingSave == nil {
			w.PendingSave = &intent
			w.Revision++
			if err = p.saveWorkspace(user.Id, w); err != nil {
				return nil, err
			}
		}
		pref := model.Preference{UserId: user.Id, Category: model.PreferenceCategoryFlaggedPost, Name: id, Value: "true"}
		var appErr *model.AppError
		if *req.Saved {
			appErr = p.API.UpdatePreferencesForUser(user.Id, []model.Preference{pref})
		} else {
			appErr = p.API.DeletePreferencesForUser(user.Id, []model.Preference{pref})
		}
		if appErr != nil {
			return nil, appErr
		}
		if err = p.resolveWorkspaceSave(user.Id, w); err != nil {
			return nil, err
		}
		return p.workspaceView(w), nil
	default:
		return nil, bad(404, "workspace route not found")
	}
	w.Revision++
	if err = p.saveWorkspace(user.Id, w); err != nil {
		return nil, err
	}
	return p.workspaceView(w), nil
}
func (p *Plugin) workspaceSaved(user string, w *humanWorkspace) (map[string]any, error) {
	prefs, err := p.workspacePreferences(user)
	if err != nil {
		return nil, err
	}
	ids := []string{}
	for id := range prefs {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	items := []map[string]any{}
	for _, id := range ids {
		item := map[string]any{"post_id": id, "post": nil, "note": w.Notes[id], "unavailable": true}
		context, err := p.operatorContext(id)
		if err == nil {
			item["post"] = context["post"]
			item["unavailable"] = false
		} else if e, ok := err.(*httpError); !ok || (e.status != 403 && e.status != 404 && e.status != 409) {
			return nil, err
		}
		items = append(items, item)
	}
	return map[string]any{"items": items, "revision": w.Revision}, nil
}
func (p *Plugin) workspaceInbox(w *humanWorkspace, r *http.Request) (map[string]any, error) {
	limit, err := queryNumber(r, "limit", 50, 1, 100)
	if err != nil {
		return nil, err
	}
	query := strings.TrimSpace(r.URL.Query().Get("q"))
	if !utf8.ValidString(query) || utf8.RuneCountInString(query) > 256 {
		return nil, bad(400, "Inbox query exceeds 256 characters")
	}
	query = strings.ToLower(query)
	before := uint64(0)
	if value := r.URL.Query().Get("before"); value != "" {
		before, err = strconv.ParseUint(value, 10, 64)
		if err != nil || before == 0 {
			return nil, bad(400, "invalid Inbox cursor")
		}
	}
	items := []map[string]any{}
	total, matched := 0, 0
	for _, item := range w.Items {
		if item.Handled {
			continue
		}
		total++
		row := map[string]any{"post_id": item.PostID, "post": nil, "root_id": item.RootID, "channel_id": item.ChannelID, "reasons": item.Reasons, "sequence": item.Sequence, "revision": item.Revision, "unavailable": true}
		haystack := strings.Join(item.Reasons, " ")
		if c := p.state.Conversations[item.ChannelID]; c != nil {
			haystack += " " + c.Name
		}
		context, err := p.operatorContext(item.PostID)
		if err == nil {
			row["post"] = context["post"]
			row["unavailable"] = false
			post := context["post"].(map[string]any)
			haystack += " " + post["message"].(string) + " " + post["author"].(string)
		} else if e, ok := err.(*httpError); !ok || (e.status != 403 && e.status != 404 && e.status != 409) {
			return nil, err
		}
		if query != "" && !strings.Contains(strings.ToLower(haystack), query) {
			continue
		}
		matched++
		if before == 0 || item.Sequence < before {
			items = append(items, row)
		}
	}
	sort.Slice(items, func(i, j int) bool { return items[i]["sequence"].(uint64) > items[j]["sequence"].(uint64) })
	var next any
	if len(items) > limit {
		items = items[:limit]
		next = strconv.FormatUint(items[len(items)-1]["sequence"].(uint64), 10)
	}
	return map[string]any{"items": items, "next_before": next, "total": total, "matched_total": matched, "complete": len(w.Gaps) == 0, "coverage_warning": workspaceCoverageWarning(w)}, nil
}

func (p *Plugin) observeWorkspacePosts(posts []*model.Post) {
	if !p.workspaceReady || len(p.workspaceUsers) == 0 {
		return
	}
	if p.workspaceErrors == nil {
		p.workspaceErrors = map[string]string{}
	}
	ordered := append([]*model.Post{}, posts...)
	sort.Slice(ordered, func(i, j int) bool {
		return ordered[i].CreateAt < ordered[j].CreateAt || ordered[i].CreateAt == ordered[j].CreateAt && ordered[i].Id < ordered[j].Id
	})
	for _, id := range p.workspaceUsers {
		user, err := p.operatorUser(id)
		if failure, ok := err.(*httpError); ok && failure.status == 403 {
			continue
		}
		if err != nil {
			p.workspaceErrors[id] = "human account unavailable"
			continue
		}
		w, err := p.loadWorkspace(id)
		if err != nil {
			p.workspaceErrors[id] = "human workspace unavailable"
			continue
		}
		if w == nil {
			continue
		}
		changed := false
		mention := regexp.MustCompile(`(?i)(^|[^a-z0-9._-])@` + regexp.QuoteMeta(user.Username) + `($|[^a-z0-9._-])`)
		for _, post := range ordered {
			if !userPost(post) || (post.CreateAt < w.StartedAt || post.CreateAt == w.StartedAt && workspaceContains(w.Baseline, post.Id)) || p.state.Conversations[post.ChannelId] == nil || p.state.Observed[post.Id].Historical {
				continue
			}
			if w.Seen[post.Id] || w.Items[post.Id] != nil {
				continue
			}
			root := post
			if post.RootId != "" {
				root, err = p.operatorPost(post.RootId)
				if err != nil {
					if failure, ok := err.(*httpError); ok && (failure.status == 403 || failure.status == 404) {
						continue
					}
					p.workspaceErrors[id] = "thread root unavailable"
					continue
				}
			}
			if !discussionRoot(root) || root.ChannelId != post.ChannelId {
				continue
			}
			if post.UserId == id {
				w.Seen[post.Id] = true
				changed = true
				continue
			}
			original, known, lookupErr := p.workspaceOriginalMessage(post)
			if lookupErr != nil {
				p.workspaceErrors[id] = "original message evidence unavailable"
				continue
			}
			if !known {
				if !w.Gaps[post.Id] {
					w.Gaps[post.Id] = true
					changed = true
				}
				continue
			}
			reasons := []string{}
			if mention.MatchString(original) {
				reasons = append(reasons, "mention")
			}

			if post.RootId == "" && p.state.Conversations[post.ChannelId].Kind == "dm" {
				native, appErr := p.API.GetChannel(post.ChannelId)
				if appErr != nil {
					p.workspaceErrors[id] = "DM recipient evidence unavailable"
					continue
				}
				pair := strings.Split(native.Name, "__")
				if native.Type == model.ChannelTypeDirect && len(pair) == 2 && pair[0] != pair[1] && (pair[0] == id || pair[1] == id) {
					reasons = append(reasons, "direct")
				}
			}
			follow := w.Follows[root.Id]
			if post.RootId != "" && workspaceEnabled(follow, post.CreateAt, post.Id) {
				if root.UserId == id {
					reasons = append(reasons, "reply")
				}
				followed := false
				for _, interval := range follow.Intervals {
					if interval.At < post.CreateAt || interval.At == post.CreateAt && !workspaceContains(interval.Before, post.Id) {
						followed = interval.Enabled
					}
				}
				if followed {
					reasons = append(reasons, "followed")
				}
				participated, lookupErr := p.workspaceParticipated(id, post)
				if lookupErr != nil {
					p.workspaceErrors[id] = "participation evidence unavailable"
					continue
				}
				if participated {
					reasons = append(reasons, "participated")
				}
			}
			delete(w.Gaps, post.Id)
			w.Seen[post.Id] = true
			changed = true
			if len(reasons) == 0 {
				continue
			}
			w.Sequence++
			w.Items[post.Id] = &workspaceItem{PostID: post.Id, ChannelID: post.ChannelId, RootID: root.Id, Reasons: reasons, Sequence: w.Sequence, Revision: 1}
			changed = true
		}
		if changed {
			w.Revision++
			if err = p.saveWorkspace(id, w); err != nil {
				p.workspaceErrors[id] = "human workspace persistence failed"
			}
		}
	}
}

type humanParticipationHistory interface {
	HumanParticipated(user string, post *model.Post) (bool, error)
}

func (p *Plugin) workspaceParticipated(user string, post *model.Post) (bool, error) {
	if history, ok := p.history.(humanParticipationHistory); ok {
		return history.HumanParticipated(user, post)
	}
	posts, err := p.canonical(post.ChannelId)
	if err != nil {
		return false, err
	}
	for _, prior := range posts {
		if userPost(prior) && prior.UserId == user && (prior.Id == post.RootId || prior.RootId == post.RootId) && (prior.CreateAt < post.CreateAt || prior.CreateAt == post.CreateAt && prior.Id < post.Id) {
			return true, nil
		}
	}
	return false, nil
}
func (h *postgresHistory) HumanParticipated(user string, post *model.Post) (bool, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var exists bool
	err := h.db.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM Posts WHERE ChannelId=$1 AND UserId=$2
  AND (Id=$3 OR RootId=$3) AND DeleteAt=0 AND left(Type,7)<>'system_' AND (CreateAt,Id)<($4,$5))`, post.ChannelId, user, post.RootId, post.CreateAt, post.Id).Scan(&exists)
	return exists, err
}

func workspaceContains(ids []string, id string) bool {
	for _, value := range ids {
		if id == value {
			return true
		}
	}
	return false
}
func workspaceCoverageWarning(w *humanWorkspace) string {
	if len(w.Gaps) == 0 {
		return ""
	}
	return "Some messages changed before their original version could be read. Inbox coverage is incomplete; search conversation history for additional work."
}

type workspaceEvidenceHistory interface {
	PostsAt(channel string, at int64) ([]string, error)
	OriginalMessage(post *model.Post) (string, bool, error)
}

func (p *Plugin) workspacePostsAt(channel string, at int64) ([]string, error) {
	if h, ok := p.history.(workspaceEvidenceHistory); ok {
		return h.PostsAt(channel, at)
	}
	posts, err := p.canonical(channel)
	if err != nil {
		return nil, err
	}
	ids := []string{}
	for _, post := range posts {
		if userPost(post) && post.CreateAt == at {
			ids = append(ids, post.Id)
		}
	}
	return ids, nil
}
func (p *Plugin) workspaceOriginalMessage(post *model.Post) (string, bool, error) {
	if h, ok := p.history.(workspaceEvidenceHistory); ok {
		return h.OriginalMessage(post)
	}
	if post.EditAt == 0 {
		return post.Message, true, nil
	}
	return "", false, nil
}
func (h *postgresHistory) PostsAt(channel string, at int64) ([]string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	rows, err := h.db.QueryContext(ctx, `SELECT Id FROM Posts WHERE ChannelId=$1 AND CreateAt=$2 AND DeleteAt=0 AND left(Type,7)<>'system_'`, channel, at)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	ids := []string{}
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// Mattermost 12.0.1 archives the pre-edit row under OriginalId, retaining its
// original EditAt=0. UpdateAt alone is not edit evidence: replies update roots.
// Retention or an interrupted archive insert may remove this evidence. Never
// infer an initial mention from edited current text when that version is absent.
func (h *postgresHistory) OriginalMessage(post *model.Post) (string, bool, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	rows, err := h.db.QueryContext(ctx, `SELECT DISTINCT Message FROM Posts WHERE (Id=$1 OR OriginalId=$1)
  AND ChannelId=$2 AND UserId=$3 AND RootId=$4 AND CreateAt=$5 AND EditAt=0 LIMIT 2`, post.Id, post.ChannelId, post.UserId, post.RootId, post.CreateAt)
	if err != nil {
		return "", false, err
	}
	defer rows.Close()
	messages := []string{}
	for rows.Next() {
		var message string
		if err = rows.Scan(&message); err != nil {
			return "", false, err
		}
		messages = append(messages, message)
	}
	if err = rows.Err(); err != nil {
		return "", false, err
	}
	if len(messages) != 1 {
		return "", false, nil
	}
	return messages[0], true, nil
}
