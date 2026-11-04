package main

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/lib/pq"
	"github.com/mattermost/mattermost/server/public/model"
)

type operatorSearchHistory interface {
	SearchPosts(channels []string, root, query, before string, limit int) ([]*model.Post, string, error)
}

// The digest binds navigation state, not authorization. Every continuation
// still resolves its anchor under the current managed scope and query.
type searchCursor struct {
	Post  string `json:"post"`
	Scope string `json:"scope"`
}

func searchScope(query, channel, root string) string {
	raw, _ := json.Marshal([]string{query, channel, root})
	hash := sha256.Sum256(raw)
	return hex.EncodeToString(hash[:])
}

func (p *Plugin) operatorSearch(r *http.Request) (map[string]any, error) {
	query := strings.TrimSpace(r.URL.Query().Get("q"))
	if !utf8.ValidString(query) || query == "" || utf8.RuneCountInString(query) > 256 {
		return nil, bad(400, "search requires between 1 and 256 characters")
	}
	limit, err := queryNumber(r, "limit", 50, 1, 100)
	if err != nil {
		return nil, err
	}
	channel, root := r.URL.Query().Get("conversation"), r.URL.Query().Get("root")
	if channel != "" {
		if !model.IsValidId(channel) {
			return nil, bad(400, "invalid conversation scope")
		}
		if _, err = p.operatorChannel(channel); err != nil {
			return nil, err
		}
	}
	if root != "" {
		if !model.IsValidId(root) {
			return nil, bad(400, "invalid thread scope")
		}
		anchor, err := p.operatorPost(root)
		if err != nil {
			return nil, err
		}
		if !discussionRoot(anchor) {
			return nil, bad(400, "thread scope must be a root message")
		}
		if channel != "" && channel != anchor.ChannelId {
			return nil, bad(400, "thread does not belong to the conversation scope")
		}
		channel = anchor.ChannelId
	}
	channels := []string{}
	if channel != "" {
		channels = append(channels, channel)
	} else {
		for id := range p.state.Conversations {
			channels = append(channels, id)
		}
		sort.Strings(channels)
	}
	binding := searchScope(query, channel, root)
	before := ""
	if raw := r.URL.Query().Get("before"); raw != "" {
		if len(raw) > 512 {
			return nil, bad(400, "invalid search cursor")
		}
		data, err := base64.RawURLEncoding.DecodeString(raw)
		var cursor searchCursor
		if err != nil || json.Unmarshal(data, &cursor) != nil || !model.IsValidId(cursor.Post) || cursor.Scope != binding {
			return nil, bad(400, "search cursor does not match this query and scope")
		}
		before = cursor.Post
	}
	history, ok := p.history.(operatorSearchHistory)
	if !ok {
		return nil, bad(503, "full-history search is unavailable on this server")
	}
	posts, next, err := history.SearchPosts(channels, root, query, before, limit)
	if err != nil {
		return nil, err
	}
	views := []map[string]any{}
	for _, post := range posts {
		if post == nil || !userPost(post) {
			return nil, bad(503, "canonical search result unavailable")
		}
		if _, err = p.operatorChannel(post.ChannelId); err != nil {
			return nil, bad(503, "canonical search scope mismatch")
		}
		if channel != "" && post.ChannelId != channel || root != "" && post.Id != root && post.RootId != root {
			return nil, bad(503, "canonical search scope mismatch")
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

// Literal case-insensitive substring search of canonical managed history. SQL
// limits result materialization; substring matching may scan the selected
// history and is bounded by the request deadline, not a constant-work promise.
func (h *postgresHistory) SearchPosts(channels []string, root, query, before string, limit int) ([]*model.Post, string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	tx, err := h.db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead})
	if err != nil {
		return nil, "", err
	}
	defer tx.Rollback()
	if root != "" {
		var available bool
		err = tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM Posts WHERE Id=$1 AND ChannelId=ANY($2)
   AND RootId='' AND DeleteAt=0 AND left(Type,7)<>'system_')`, root, pq.Array(channels)).Scan(&available)
		if err != nil {
			return nil, "", err
		}
		if !available {
			return nil, "", bad(404, "thread scope is no longer available")
		}
	}
	// The root condition excludes deleted/system/missing roots and malformed
	// cross-channel reply references. The same predicate validates the cursor.
	const predicate = ` FROM Posts p WHERE p.ChannelId=ANY($1) AND p.DeleteAt=0 AND left(p.Type,7)<>'system_'
  AND ($2='' OR p.Id=$2 OR p.RootId=$2)
  AND (p.RootId='' OR EXISTS (SELECT 1 FROM Posts root WHERE root.Id=p.RootId
    AND root.ChannelId=p.ChannelId AND root.RootId='' AND root.DeleteAt=0 AND left(root.Type,7)<>'system_'))
  AND strpos(lower(p.Message),lower($3))>0`
	var at int64
	if before != "" {
		err = tx.QueryRowContext(ctx, `SELECT p.CreateAt`+predicate+` AND p.Id=$4`, pq.Array(channels), root, query, before).Scan(&at)
		if err == sql.ErrNoRows {
			return nil, "", bad(404, "search cursor is no longer available; restart the search")
		}
		if err != nil {
			return nil, "", err
		}
	}
	rows, err := tx.QueryContext(ctx, `SELECT p.Id,p.ChannelId,p.UserId,p.Message,p.RootId,p.CreateAt,p.UpdateAt,p.DeleteAt,p.Type,p.Props,p.FileIds`+predicate+
		` AND ($4='' OR (p.CreateAt,p.Id)<($5,$4)) ORDER BY p.CreateAt DESC,p.Id DESC LIMIT $6`,
		pq.Array(channels), root, query, before, at, limit+1)
	if err != nil {
		return nil, "", err
	}
	posts := []*model.Post{}
	for rows.Next() {
		post := &model.Post{}
		var props, fileIDs []byte
		if err = rows.Scan(&post.Id, &post.ChannelId, &post.UserId, &post.Message, &post.RootId, &post.CreateAt, &post.UpdateAt, &post.DeleteAt, &post.Type, &props, &fileIDs); err != nil {
			rows.Close()
			return nil, "", err
		}
		if len(props) > 0 {
			if err = json.Unmarshal(props, &post.Props); err != nil {
				rows.Close()
				return nil, "", err
			}
		}
		if len(fileIDs) > 0 {
			if err = json.Unmarshal(fileIDs, &post.FileIds); err != nil {
				rows.Close()
				return nil, "", err
			}
		}
		posts = append(posts, post)
	}
	err = rows.Err()
	rows.Close()
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
