package main

// Run against an explicitly disposable PostgreSQL 16 database whose name starts
// with murmur_metadata_test. Each test creates/drops only its own random schema:
// MURMUR_METADATA_TEST_DISPOSABLE=1 MURMUR_METADATA_TEST_DSN=postgres://.../murmur_metadata_test?sslmode=disable go test -run TestPostgresMetadata -v

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/lib/pq"
	"github.com/mattermost/mattermost/server/public/model"
)

func metadataDatabase(t *testing.T) (string, *sql.DB) {
	t.Helper()
	dsn := os.Getenv("MURMUR_METADATA_TEST_DSN")
	if dsn == "" {
		t.Skip("explicit disposable PostgreSQL metadata target not supplied")
	}
	if os.Getenv("MURMUR_METADATA_TEST_DISPOSABLE") != "1" {
		t.Fatal("metadata integration requires MURMUR_METADATA_TEST_DISPOSABLE=1")
	}
	admin, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = admin.Close() })
	var database string
	if err := admin.QueryRow("SELECT current_database()").Scan(&database); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(database, "murmur_metadata_test") {
		t.Fatal("refusing metadata integration outside a murmur_metadata_test database")
	}
	schema := "murmur_metadata_test_" + model.NewId()
	if _, err := admin.Exec("CREATE SCHEMA " + pq.QuoteIdentifier(schema)); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if _, err := admin.Exec("DROP SCHEMA " + pq.QuoteIdentifier(schema) + " CASCADE"); err != nil {
			t.Error(err)
		}
	})
	if strings.HasPrefix(dsn, "postgres://") || strings.HasPrefix(dsn, "postgresql://") {
		parsed, err := url.Parse(dsn)
		if err != nil {
			t.Fatal(err)
		}
		query := parsed.Query()
		query.Set("search_path", schema)
		parsed.RawQuery = query.Encode()
		dsn = parsed.String()
	} else {
		dsn += " search_path=" + schema
	}
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return dsn, db
}

func postgresMetadataFixture(t *testing.T, legacy []byte) (*postgresMetadata, *sql.DB, *int) {
	t.Helper()
	dsn, db := metadataDatabase(t)
	reads := 0
	store, err := openMetadata(dsn, func() ([]byte, error) {
		reads++
		return append([]byte(nil), legacy...), nil
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store, db, &reads
}

func metadataEntityCount(t *testing.T, db *sql.DB) int {
	t.Helper()
	var count int
	if err := db.QueryRow("SELECT count(*) FROM murmur_metadata_entities").Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func TestPostgresMetadataMigrationRestartAndBoundedEntityWrites(t *testing.T) {
	state := metadataFixture(t)
	legacy, _ := json.Marshal(state)
	untouched := append([]byte(nil), legacy...)
	store, db, reads := postgresMetadataFixture(t, legacy)
	loaded, err := store.Load()
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(state, loaded) || *reads != 1 || !bytes.Equal(legacy, untouched) {
		t.Fatal("legacy migration changed identity/state or its historical input")
	}
	// Audit actual INSERT/UPDATE/DELETE row effects, not a mocked delta count.
	if _, err := db.Exec(`CREATE TABLE metadata_test_mutations(kind text);
		CREATE FUNCTION metadata_test_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
		INSERT INTO metadata_test_mutations VALUES(TG_OP); RETURN NULL; END $$;
		CREATE TRIGGER metadata_test_audit AFTER INSERT OR UPDATE OR DELETE ON murmur_metadata_entities
		FOR EACH ROW EXECUTE FUNCTION metadata_test_audit()`); err != nil {
		t.Fatal(err)
	}
	if err := store.Save(loaded); err != nil {
		t.Fatal(err)
	}
	var mutations int
	if err := db.QueryRow("SELECT count(*) FROM metadata_test_mutations").Scan(&mutations); err != nil || mutations != 0 {
		t.Fatalf("unchanged save made %d entity mutations: %v", mutations, err)
	}
	loaded.Sequence++
	loaded.Observed["fresh"] = Observation{Seq: loaded.Sequence, FirstSeenSeq: loaded.Sequence, PostID: "fresh", ChannelID: "room"}
	loaded.Operations["fresh"] = &Operation{ID: "fresh", Actor: "codex:child", Kind: "post", Hash: "fresh-hash", Status: "complete", PostID: "fresh", ChannelID: "room"}
	if err := store.Save(loaded); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM metadata_test_mutations").Scan(&mutations); err != nil || mutations != 3 {
		t.Fatalf("one post made %d entity mutations, want 3: %v", mutations, err)
	}
	store.legacy = func() ([]byte, error) { t.Fatal("committed SQL state reread legacy KV"); return nil, nil }
	restarted, err := store.Load()
	if err != nil || !reflect.DeepEqual(loaded, restarted) {
		t.Fatalf("SQL restart lost domain state: %v", err)
	}
	if !bytes.Equal(legacy, untouched) {
		t.Fatal("legacy KV input was rewritten")
	}
}

func TestPostgresMetadataMigrationConnectionLossRollsBackAllRows(t *testing.T) {
	legacy, _ := json.Marshal(metadataFixture(t))
	store, db, reads := postgresMetadataFixture(t, legacy)
	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := metadataTables(context.Background(), tx); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	// Terminate the actual importing connection only after earlier ordered
	// entities were written. PostgreSQL must roll back both rows and marker.
	if _, err := db.Exec(`CREATE SEQUENCE metadata_test_attempts;
		CREATE FUNCTION metadata_test_crash() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
		PERFORM nextval('metadata_test_attempts');
		IF NEW.collection='operations' THEN PERFORM pg_terminate_backend(pg_backend_pid()); END IF;
		RETURN NEW; END $$;
		CREATE TRIGGER metadata_test_crash BEFORE INSERT ON murmur_metadata_entities
		FOR EACH ROW EXECUTE FUNCTION metadata_test_crash()`); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Load(); err == nil {
		t.Fatal("migration ignored a terminated SQL connection")
	}
	var marker, attempted int
	if err := db.QueryRow("SELECT count(*) FROM murmur_metadata_schema").Scan(&marker); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT last_value FROM metadata_test_attempts").Scan(&attempted); err != nil {
		t.Fatal(err)
	}
	if marker != 0 || metadataEntityCount(t, db) != 0 || attempted < 2 || store.loaded {
		t.Fatalf("partial migration survived: marker=%d attempts=%d loaded=%v", marker, attempted, store.loaded)
	}
	if _, err := db.Exec("DROP TRIGGER metadata_test_crash ON murmur_metadata_entities"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Load(); err != nil || *reads != 2 {
		t.Fatalf("uncommitted migration could not retry from legacy: %v, reads %d", err, *reads)
	}
}

func TestPostgresMetadataFailureRollbackFormatAndStaleWriter(t *testing.T) {
	store, db, _ := postgresMetadataFixture(t, nil)
	state, err := store.Load()
	if err != nil {
		t.Fatal(err)
	}
	baseline := mustEncodeMetadata(t, state)
	if _, err := db.Exec(`ALTER TABLE murmur_metadata_entities ADD CONSTRAINT metadata_test_failure CHECK (entity_key <> 'fail-after-scalar')`); err != nil {
		t.Fatal(err)
	}
	state.Sequence++
	state.Operations["fail-after-scalar"] = &Operation{ID: "fail-after-scalar", Kind: "post", Status: "complete", Hash: "fail"}
	if err := store.Save(state); err == nil {
		t.Fatal("injected SQL constraint failure did not abort save")
	}
	if store.revision != 1 || !reflect.DeepEqual(store.committed, baseline) {
		t.Fatal("failed transaction advanced the committed cache")
	}
	reloaded, err := store.Load()
	if err != nil || reloaded.Sequence != 0 || reloaded.Operations["fail-after-scalar"] != nil {
		t.Fatalf("failed save partially committed: %#v, %v", reloaded, err)
	}
	store.legacy = func() ([]byte, error) { t.Fatal("format mismatch fell back to KV"); return nil, nil }
	for _, version := range []int{metadataSchemaVersion + 1, metadataSchemaVersion - 1} {
		if _, err := db.Exec("UPDATE murmur_metadata_schema SET format_version=$1", version); err != nil {
			t.Fatal(err)
		}
		if _, err := store.Load(); err == nil || !strings.Contains(err.Error(), "incompatible SQL metadata") {
			t.Fatalf("format %d was accepted/fell back: %v", version, err)
		}
	}
	if _, err := db.Exec("UPDATE murmur_metadata_schema SET format_version=$1", metadataSchemaVersion); err != nil {
		t.Fatal(err)
	}
	state, err = store.Load()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE murmur_metadata_schema SET revision=revision+1"); err != nil {
		t.Fatal(err)
	}
	state.Sequence = 50
	if err := store.Save(state); err == nil || !strings.Contains(err.Error(), "revision changed") {
		t.Fatalf("stale store overwrote a committed revision: %v", err)
	}
}

func TestPostgresMetadataSerializedConcurrentSavesRetainAllOperations(t *testing.T) {
	store, _, _ := postgresMetadataFixture(t, nil)
	state, err := store.Load()
	if err != nil {
		t.Fatal(err)
	}
	// The plugin mutex is the owner of domain serialization. This intentionally
	// models concurrent handlers through that same boundary, not a cluster.
	var mutex sync.Mutex
	var wait sync.WaitGroup
	errors := make(chan error, 20)
	for n := 0; n < 20; n++ {
		wait.Add(1)
		go func(number int) {
			defer wait.Done()
			mutex.Lock()
			defer mutex.Unlock()
			id := fmt.Sprintf("concurrent-%d", number)
			state.Sequence++
			state.Operations[id] = &Operation{ID: id, Kind: "post", Hash: id, Status: "complete", PostID: id}
			errors <- store.Save(state)
		}(n)
	}
	wait.Wait()
	close(errors)
	for err := range errors {
		if err != nil {
			t.Fatal(err)
		}
	}
	loaded, err := store.Load()
	if err != nil || loaded.Sequence != 20 || len(loaded.Operations) != 20 {
		t.Fatalf("concurrent serialized saves lost operations: %v", err)
	}
}
