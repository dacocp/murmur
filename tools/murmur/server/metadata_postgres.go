package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"sync"
	"time"
)

// SQL writes are proportional to changed entity rows, not retained history.
// Save still serializes/compares O(all metadata) in memory. metadataDelta is the
// transaction input so future explicit dirty-entity writes can bypass that walk
// without changing the persistence or atomicity boundary.
type postgresMetadata struct {
	db        *sql.DB
	legacy    func() ([]byte, error)
	mu        sync.Mutex
	loaded    bool
	revision  int64
	committed metadataRecords
}

func openMetadata(dsn string, legacy func() ([]byte, error)) (*postgresMetadata, error) {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, fmt.Errorf("open metadata database: %w", err)
	}
	db.SetMaxOpenConns(2)
	db.SetMaxIdleConns(1)
	db.SetConnMaxLifetime(5 * time.Minute)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("metadata database unavailable: %w", err)
	}
	return &postgresMetadata{db: db, legacy: legacy}, nil
}

func (s *postgresMetadata) Close() error { return s.db.Close() }

func metadataTables(ctx context.Context, tx *sql.Tx) error {
	_, err := tx.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS murmur_metadata_schema (
		singleton smallint PRIMARY KEY CHECK (singleton = 1),
		format_version integer NOT NULL,
		revision bigint NOT NULL CHECK (revision > 0),
		migrated_from text NOT NULL
	)`)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS murmur_metadata_entities (
		collection text NOT NULL,
		entity_key text NOT NULL,
		payload jsonb NOT NULL,
		PRIMARY KEY (collection, entity_key)
	)`)
	return err
}

func (s *postgresMetadata) Load() (*State, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.loaded = false
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if err := metadataTables(ctx, tx); err != nil {
		return nil, fmt.Errorf("metadata schema unavailable: %w", err)
	}
	var version int
	var revision int64
	err = tx.QueryRowContext(ctx, `SELECT format_version, revision FROM murmur_metadata_schema WHERE singleton=1`).Scan(&version, &revision)
	if err == sql.ErrNoRows {
		// Absence of the committed marker is the only permitted legacy path.
		// DDL, all imported entities and the marker commit in one transaction.
		var count int
		if err := tx.QueryRowContext(ctx, `SELECT count(*) FROM murmur_metadata_entities`).Scan(&count); err != nil {
			return nil, err
		}
		if count != 0 {
			return nil, fmt.Errorf("metadata entities exist without a committed schema marker")
		}
		state := &State{}
		state.init()
		origin := "empty"
		if s.legacy == nil {
			return nil, fmt.Errorf("initial metadata migration requires an explicit legacy reader")
		}
		data, err := s.legacy()
		if err != nil {
			return nil, fmt.Errorf("legacy metadata unavailable: %w", err)
		}
		if len(data) != 0 {
			state, err = decodeState(data)
			if err != nil {
				return nil, err
			}
			origin = "state-v1"
		}
		records, err := encodeMetadata(state)
		if err != nil {
			return nil, err
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO murmur_metadata_schema(singleton,format_version,revision,migrated_from) VALUES(1,$1,1,$2)`, metadataSchemaVersion, origin); err != nil {
			return nil, err
		}
		if err := writeMetadataDelta(ctx, tx, compareMetadata(nil, records)); err != nil {
			return nil, fmt.Errorf("metadata migration failed: %w", err)
		}
		if err := tx.Commit(); err != nil {
			return nil, fmt.Errorf("metadata migration commit is unconfirmed: %w", err)
		}
		s.loaded, s.revision, s.committed = true, 1, records
		return state, nil
	}
	if err != nil {
		return nil, fmt.Errorf("metadata format unavailable: %w", err)
	}
	if version != metadataSchemaVersion {
		return nil, fmt.Errorf("incompatible SQL metadata format %d; this build requires %d (no KV fallback)", version, metadataSchemaVersion)
	}
	records := metadataRecords{}
	rows, err := tx.QueryContext(ctx, `SELECT collection,entity_key,payload FROM murmur_metadata_entities`)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var key metadataKey
		var data json.RawMessage
		if err := rows.Scan(&key.Collection, &key.ID, &data); err != nil {
			_ = rows.Close()
			return nil, err
		}
		records[key] = data
	}
	err = rows.Err()
	_ = rows.Close()
	if err != nil {
		return nil, err
	}
	state, err := decodeMetadata(records)
	if err != nil {
		return nil, err
	}
	// PostgreSQL jsonb formats bytes differently. Cache our typed encoder's
	// representation, so an unchanged first save does not rewrite every row.
	records, err = encodeMetadata(state)
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	s.loaded, s.revision, s.committed = true, revision, records
	return state, nil
}

func (s *postgresMetadata) Save(state *State) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.loaded {
		return fmt.Errorf("metadata store has not loaded committed state")
	}
	records, err := encodeMetadata(state)
	if err != nil {
		return err
	}
	delta := compareMetadata(s.committed, records)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	result, err := tx.ExecContext(ctx, `UPDATE murmur_metadata_schema SET revision=revision+1 WHERE singleton=1 AND format_version=$1 AND revision=$2`, metadataSchemaVersion, s.revision)
	if err != nil {
		return err
	}
	count, err := result.RowsAffected()
	if err != nil || count != 1 {
		return fmt.Errorf("metadata format/revision changed; reload requires plugin restart")
	}
	if err := writeMetadataDelta(ctx, tx, delta); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("metadata commit is unconfirmed: %w", err)
	}
	s.revision++
	s.committed = records
	return nil
}

func writeMetadataDelta(ctx context.Context, tx *sql.Tx, delta metadataDelta) error {
	for _, key := range delta.Delete {
		if _, err := tx.ExecContext(ctx, `DELETE FROM murmur_metadata_entities WHERE collection=$1 AND entity_key=$2`, key.Collection, key.ID); err != nil {
			return err
		}
	}
	for _, row := range delta.Upsert {
		if _, err := tx.ExecContext(ctx, `INSERT INTO murmur_metadata_entities(collection,entity_key,payload) VALUES($1,$2,$3::jsonb)
			ON CONFLICT(collection,entity_key) DO UPDATE SET payload=EXCLUDED.payload`, row.Key.Collection, row.Key.ID, string(row.Value)); err != nil {
			return err
		}
	}
	return nil
}
