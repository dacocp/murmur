package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"time"

	"github.com/mattermost/mattermost/server/public/model"
)

// Optional so older history implementations cannot silently advertise complete
// boards by filtering mixed root/reply history pages.
type rootBoardHistory interface {
	RootBoard(channel, before string, limit int) ([]boardRoot, string, error)
}

type boardRoot struct {
	post    *model.Post
	replies int64
}

func (p *Plugin) operatorBoard(channel string, r *http.Request) (map[string]any, error) {
	if _, err := p.operatorChannel(channel); err != nil {
		return nil, err
	}
	limit, err := queryNumber(r, "limit", 50, 1, 100)
	if err != nil {
		return nil, err
	}
	history, ok := p.history.(rootBoardHistory)
	if !ok {
		return nil, bad(503, "complete root boards are unavailable on this server")
	}
	roots, next, err := history.RootBoard(channel, r.URL.Query().Get("before"), limit)
	if err != nil {
		return nil, err
	}
	posts := []map[string]any{}
	for _, root := range roots {
		if !discussionRoot(root.post) || root.post.ChannelId != channel {
			return nil, bad(503, "canonical board identity mismatch")
		}
		view := p.postView(root.post)
		view["reply_count"] = root.replies
		posts = append(posts, view)
	}
	var cursor any
	if next != "" {
		cursor = next
	}
	return map[string]any{"posts": posts, "next_before": cursor}, nil
}

func (p *Plugin) operatorContext(id string) (map[string]any, error) {
	post, err := p.operatorPost(id)
	if err != nil {
		return nil, err
	}
	if post == nil || post.Id != id {
		return nil, bad(503, "canonical post identity mismatch")
	}
	root := post
	if post.RootId != "" {
		root, err = p.operatorPost(post.RootId)
		if err != nil {
			return nil, err
		}
	}
	if !discussionRoot(root) || root.ChannelId != post.ChannelId || (post.RootId != "" && root.Id != post.RootId) {
		return nil, bad(409, "post has no available root in its conversation")
	}
	return map[string]any{"post": p.postView(post), "root": p.postView(root), "channel_id": post.ChannelId}, nil
}

// Root-only keyset reads on the existing pinned Mattermost Posts schema. Each
// request has bounded output and no full-history materialization. PostgreSQL
// still visits qualifying replies to calculate exact counts; this is not a
// constant-work claim. Counts and roots share one read-only snapshot.
func (h *postgresHistory) RootBoard(channel, before string, limit int) ([]boardRoot, string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	tx, err := h.db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead})
	if err != nil {
		return nil, "", err
	}
	defer tx.Rollback()
	var at int64
	if before != "" {
		err = tx.QueryRowContext(ctx, `SELECT CreateAt FROM Posts WHERE Id=$1 AND ChannelId=$2
   AND RootId='' AND DeleteAt=0 AND left(Type,7)<>'system_'`, before, channel).Scan(&at)
		if err == sql.ErrNoRows {
			return nil, "", bad(404, "before root is unavailable in this conversation; restart the board")
		}
		if err != nil {
			return nil, "", err
		}
	}
	rows, err := tx.QueryContext(ctx, `SELECT p.Id,p.ChannelId,p.UserId,p.Message,p.RootId,p.CreateAt,p.UpdateAt,p.DeleteAt,p.Type,p.Props,p.FileIds,
  (SELECT count(*) FROM Posts r WHERE r.ChannelId=p.ChannelId AND r.RootId=p.Id AND r.DeleteAt=0 AND left(r.Type,7)<>'system_')
  FROM Posts p WHERE p.ChannelId=$1 AND p.RootId='' AND p.DeleteAt=0 AND left(p.Type,7)<>'system_'
  AND ($2='' OR (p.CreateAt,p.Id)<($3,$2))
  ORDER BY p.CreateAt DESC,p.Id DESC LIMIT $4`, channel, before, at, limit+1)
	if err != nil {
		return nil, "", err
	}
	roots := []boardRoot{}
	for rows.Next() {
		root := boardRoot{post: &model.Post{}}
		p := root.post
		var props, fileIDs []byte
		if err = rows.Scan(&p.Id, &p.ChannelId, &p.UserId, &p.Message, &p.RootId, &p.CreateAt, &p.UpdateAt, &p.DeleteAt, &p.Type, &props, &fileIDs, &root.replies); err != nil {
			rows.Close()
			return nil, "", err
		}
		if len(props) > 0 {
			if err = json.Unmarshal(props, &p.Props); err != nil {
				rows.Close()
				return nil, "", err
			}
		}
		if len(fileIDs) > 0 {
			if err = json.Unmarshal(fileIDs, &p.FileIds); err != nil {
				rows.Close()
				return nil, "", err
			}
		}
		roots = append(roots, root)
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
	if len(roots) > limit {
		roots = roots[:limit]
		next = roots[len(roots)-1].post.Id
	}
	for i, j := 0, len(roots)-1; i < j; i, j = i+1, j-1 {
		roots[i], roots[j] = roots[j], roots[i]
	}
	return roots, next, nil
}
