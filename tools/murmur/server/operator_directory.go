package main

import (
	"context"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/lib/pq"
	"github.com/mattermost/mattermost/server/public/model"
)

type agentPostActivity struct{ latest, personal *model.Post }
type operatorDirectoryHistory interface {
	OwnPosts(channels []string, user, query, before string, limit int) ([]*model.Post, string, error)
	AgentActivity(channels, users []string, human string, followed []string) (map[string]agentPostActivity, error)
}

func (p *Plugin) directoryChannels() []string {
	ids := make([]string, 0, len(p.state.Conversations))
	for id := range p.state.Conversations {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// Your messages uses the authenticated native author, regardless of which
// client posted. Visibility does not imply human channel membership.
func (p *Plugin) operatorYourMessages(user *model.User, r *http.Request) (map[string]any, error) {
	query := strings.TrimSpace(r.URL.Query().Get("q"))
	if !utf8.ValidString(query) || utf8.RuneCountInString(query) > 256 {
		return nil, bad(400, "search accepts at most 256 characters")
	}
	limit, err := queryNumber(r, "limit", 50, 1, 100)
	if err != nil {
		return nil, err
	}
	binding := searchScope(query, user.Id, "your-messages")
	before := ""
	if raw := r.URL.Query().Get("before"); raw != "" {
		if len(raw) > 512 {
			return nil, bad(400, "invalid message cursor")
		}
		data, err := base64.RawURLEncoding.DecodeString(raw)
		var cursor searchCursor
		if err != nil || json.Unmarshal(data, &cursor) != nil || !model.IsValidId(cursor.Post) || cursor.Scope != binding {
			return nil, bad(400, "message cursor does not match this query and author")
		}
		before = cursor.Post
	}
	history, ok := p.history.(operatorDirectoryHistory)
	if !ok {
		return nil, bad(503, "personal message history is unavailable on this server")
	}
	posts, next, err := history.OwnPosts(p.directoryChannels(), user.Id, query, before, limit)
	if err != nil {
		return nil, err
	}
	views := []map[string]any{}
	for _, post := range posts {
		if post == nil || !userPost(post) || post.UserId != user.Id || p.state.Conversations[post.ChannelId] == nil {
			return nil, bad(503, "canonical personal history scope mismatch")
		}
		views = append(views, p.postView(post))
	}
	var cursor any
	if next != "" {
		raw, _ := json.Marshal(searchCursor{Post: next, Scope: binding})
		cursor = base64.RawURLEncoding.EncodeToString(raw)
	}
	return map[string]any{"posts": views, "next_before": cursor}, nil
}

// One canonical latest post per enrolled identity and scope. Timestamps describe
// authored messages, never runtime availability, presence or enrollment time.
func (p *Plugin) operatorAgentActivity(user *model.User, followed []string) (map[string]any, error) {
	history, ok := p.history.(operatorDirectoryHistory)
	if !ok {
		return nil, bad(503, "agent activity is unavailable on this server")
	}
	ids, users := []string{}, []string{}
	for id, a := range p.state.Agents {
		ids = append(ids, id)
		users = append(users, a.UserID)
	}
	sort.Strings(ids)
	activity, err := history.AgentActivity(p.directoryChannels(), users, user.Id, followed)
	if err != nil {
		return nil, err
	}
	agents := []map[string]any{}
	for _, id := range ids {
		a := p.state.Agents[id]
		entry := map[string]any{"id": id, "latest": nil, "latest_personal": nil}
		fact := activity[a.UserID]
		for key, post := range map[string]*model.Post{"latest": fact.latest, "latest_personal": fact.personal} {
			if post == nil {
				continue
			}
			if !userPost(post) || post.UserId != a.UserID || p.state.Conversations[post.ChannelId] == nil {
				return nil, bad(503, "canonical agent activity scope mismatch")
			}
			entry[key] = p.postView(post)
		}
		agents = append(agents, entry)
	}
	return map[string]any{"agents": agents}, nil
}

const directoryVisible = ` p.ChannelId=ANY($1) AND p.DeleteAt=0 AND left(p.Type,7)<>'system_'
 AND (p.RootId='' OR EXISTS (SELECT 1 FROM Posts root WHERE root.Id=p.RootId
 AND root.ChannelId=p.ChannelId AND root.RootId='' AND root.DeleteAt=0 AND left(root.Type,7)<>'system_'))`
const directoryColumns = `p.Id,p.ChannelId,p.UserId,p.Message,p.RootId,p.CreateAt,p.UpdateAt,p.DeleteAt,p.Type,p.Props,p.FileIds`

func directoryPosts(rows *sql.Rows) ([]*model.Post, error) {
	defer rows.Close()
	posts := []*model.Post{}
	for rows.Next() {
		post := &model.Post{}
		var props, fileIDs []byte
		if err := rows.Scan(&post.Id, &post.ChannelId, &post.UserId, &post.Message, &post.RootId, &post.CreateAt, &post.UpdateAt, &post.DeleteAt, &post.Type, &props, &fileIDs); err != nil {
			return nil, err
		}
		if len(props) > 0 {
			if err := json.Unmarshal(props, &post.Props); err != nil {
				return nil, err
			}
		}
		if len(fileIDs) > 0 {
			if err := json.Unmarshal(fileIDs, &post.FileIds); err != nil {
				return nil, err
			}
		}
		posts = append(posts, post)
	}
	return posts, rows.Err()
}

func (h *postgresHistory) OwnPosts(channels []string, user, query, before string, limit int) ([]*model.Post, string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	tx, err := h.db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead})
	if err != nil {
		return nil, "", err
	}
	defer tx.Rollback()
	const predicate = ` FROM Posts p WHERE ` + directoryVisible + ` AND p.UserId=$2 AND strpos(lower(p.Message),lower($3))>0`
	var at int64
	if before != "" {
		err = tx.QueryRowContext(ctx, `SELECT p.CreateAt`+predicate+` AND p.Id=$4`, pq.Array(channels), user, query, before).Scan(&at)
		if err == sql.ErrNoRows {
			return nil, "", bad(404, "message cursor is no longer available; restart the list")
		}
		if err != nil {
			return nil, "", err
		}
	}
	rows, err := tx.QueryContext(ctx, `SELECT `+directoryColumns+predicate+` AND ($4='' OR (p.CreateAt,p.Id)<($5,$4)) ORDER BY p.CreateAt DESC,p.Id DESC LIMIT $6`, pq.Array(channels), user, query, before, at, limit+1)
	if err != nil {
		return nil, "", err
	}
	posts, err := directoryPosts(rows)
	if err != nil {
		return nil, "", err
	}
	if err = tx.Commit(); err != nil {
		return nil, "", err
	}
	next := ""
	if len(posts) > limit {
		posts = posts[:limit]
		next = posts[len(posts)-1].Id
	}
	return posts, next, nil
}

func (h *postgresHistory) AgentActivity(channels, users []string, human string, followed []string) (map[string]agentPostActivity, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	tx, err := h.db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	result := map[string]agentPostActivity{}
	const base = `SELECT DISTINCT ON (p.UserId) ` + directoryColumns + ` FROM Posts p WHERE ` + directoryVisible + ` AND p.UserId=ANY($2)`
	const personal = ` AND (COALESCE(NULLIF(p.RootId,''),p.Id)=ANY($3) OR EXISTS (
 SELECT 1 FROM Posts contribution WHERE contribution.ChannelId=p.ChannelId AND contribution.UserId=$4
 AND contribution.DeleteAt=0 AND left(contribution.Type,7)<>'system_'
 AND (contribution.Id=COALESCE(NULLIF(p.RootId,''),p.Id) OR contribution.RootId=COALESCE(NULLIF(p.RootId,''),p.Id))))`
	for _, mine := range []bool{false, true} {
		query := base
		args := []any{pq.Array(channels), pq.Array(users)}
		if mine {
			query += personal
			args = append(args, pq.Array(followed), human)
		}
		rows, err := tx.QueryContext(ctx, query+` ORDER BY p.UserId,p.CreateAt DESC,p.Id DESC`, args...)
		if err != nil {
			return nil, err
		}
		posts, err := directoryPosts(rows)
		if err != nil {
			return nil, err
		}
		for _, post := range posts {
			fact := result[post.UserId]
			if mine {
				fact.personal = post
			} else {
				fact.latest = post
			}
			result[post.UserId] = fact
		}
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return result, nil
}
