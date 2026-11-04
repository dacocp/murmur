package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	_ "github.com/lib/pq"
	"github.com/mattermost/mattermost/server/public/model"
)

type canonicalHistory interface {
	Posts(channel string) ([]*model.Post, error)
}
type postgresHistory struct{ db *sql.DB }

func openHistory(dsn string) (*postgresHistory, error) {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, fmt.Errorf("open canonical history connection: %w", err)
	}
	db.SetMaxOpenConns(2)
	db.SetMaxIdleConns(1)
	db.SetConnMaxLifetime(5 * time.Minute)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err = db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("canonical history connection unavailable: %w", err)
	}
	var serverVersion int
	if err = db.QueryRowContext(ctx, "SHOW server_version_num").Scan(&serverVersion); err != nil {
		_ = db.Close()
		return nil, err
	}
	if serverVersion < 160000 || serverVersion >= 170000 {
		_ = db.Close()
		return nil, fmt.Errorf("Murmur requires PostgreSQL 16, got server version %d", serverVersion)
	}
	return &postgresHistory{db: db}, nil
}

func (h *postgresHistory) Close() error { return h.db.Close() }

// Mattermost 12.0.1's public Before API uses strictly smaller CreateAt and its
// offset API orders only by CreateAt. Both can omit equal-millisecond boundary
// posts. This pinned-schema adapter reads the primary Posts table through one
// read-only repeatable-read snapshot, using an explicit (CreateAt,Id) keyset.
// All writes still use Mattermost's plugin API. No SQL mutation is performed.
func (h *postgresHistory) Posts(channel string) ([]*model.Post, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	tx, err := h.db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	posts := []*model.Post{}
	var at int64
	var id string
	for {
		rows, queryErr := tx.QueryContext(ctx, `SELECT Id, ChannelId, UserId, Message, RootId, CreateAt, UpdateAt, DeleteAt, Type, Props, FileIds
			FROM Posts WHERE ChannelId = $1 AND DeleteAt = 0
			AND (CreateAt > $2 OR (CreateAt = $2 AND Id > $3))
			ORDER BY CreateAt ASC, Id ASC LIMIT 1000`, channel, at, id)
		if queryErr != nil {
			return nil, queryErr
		}
		count := 0
		for rows.Next() {
			post := &model.Post{}
			var props, fileIDs []byte
			if err = rows.Scan(&post.Id, &post.ChannelId, &post.UserId, &post.Message, &post.RootId,
				&post.CreateAt, &post.UpdateAt, &post.DeleteAt, &post.Type, &props, &fileIDs); err != nil {
				_ = rows.Close()
				return nil, err
			}
			if len(props) > 0 {
				if err = json.Unmarshal(props, &post.Props); err != nil {
					_ = rows.Close()
					return nil, err
				}
			}
			if len(fileIDs) > 0 {
				if err = json.Unmarshal(fileIDs, &post.FileIds); err != nil {
					_ = rows.Close()
					return nil, err
				}
			}
			posts = append(posts, post)
			at, id = post.CreateAt, post.Id
			count++
		}
		err = rows.Err()
		_ = rows.Close()
		if err != nil {
			return nil, err
		}
		if count < 1000 {
			break
		}
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return posts, nil
}
