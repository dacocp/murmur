package main

import (
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/mattermost/mattermost/server/public/model"
)

func metadataFixture(t *testing.T) *State {
	t.Helper()
	state := &State{TeamID: "team", Sequence: 42}
	state.init()
	state.Agents["codex:child"] = &Agent{ID: "codex:child", Name: "Exact title", Username: "title-stable",
		UserID: "bot", Parent: "codex:unenrolled", RegisteredAt: 1024, Generation: 7}
	cutoff := int64(13)
	state.Conversations["room"] = &Conversation{ID: "room", Name: "Review", Kind: "group", Open: true,
		Purpose: "Build it", Grants: map[string]Grant{"codex:child": {Role: "member", GrantedAt: 17, TrackAfterSeq: &cutoff}},
		Left: map[string]bool{"codex:ancestor": true, "codex:other": false}}
	state.Operations["completed"] = &Operation{ID: "completed", Actor: "codex:child", Kind: "post", Hash: "immutable",
		Status: "complete", Stage: "creating", ChannelID: "room", RootID: "root", PostID: "post"}
	state.Operations["uncertain"] = &Operation{ID: "uncertain", Actor: "codex:child", Kind: "join", Hash: "join-hash",
		Status: "uncertain", Stage: "native", ChannelID: "room", Error: "held", Tracking: true, TrackAfterSeq: &cutoff}
	state.Observed["post"] = Observation{Seq: 42, PostID: "post", ChannelID: "room", UpdateAt: 123,
		Historical: true, FirstSeenSeq: 19}
	state.Registrations["claude:pending"] = &RegistrationIntent{Parent: "claude:unenrolled", Username: "pending-stable",
		UserID: "partial-bot", CreateAttempted: true}
	// Exercise newly added maps through their persisted JSON contract, without
	// teaching the storage codec an independent copy of their Go types.
	extra := []byte(`{"lineage":{"codex:child":"codex:unenrolled","codex:unenrolled":"codex:root","codex:root":""},"enrollments":{"codex:child":{"actor":"codex:child","sequence":8}}}`)
	if err := json.Unmarshal(extra, state); err != nil {
		t.Fatal(err)
	}
	state.init()
	return state
}

func TestMetadataRoundTripPreservesDomainStateAndSplitGrants(t *testing.T) {
	state := metadataFixture(t)
	records, err := encodeMetadata(state)
	if err != nil {
		t.Fatal(err)
	}
	loaded, err := decodeMetadata(records)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(state, loaded) {
		t.Fatalf("metadata changed across round trip:\n%#v\n%#v", state, loaded)
	}
	var base map[string]json.RawMessage
	if err := json.Unmarshal(records[metadataKey{"conversations", "room"}], &base); err != nil {
		t.Fatal(err)
	}
	if string(base["grants"]) != "{}" || string(base["left"]) != "{}" {
		t.Fatalf("conversation rewrites still contain grant maps: %s", records[metadataKey{"conversations", "room"}])
	}
	if len(compareMetadata(records, mustEncodeMetadata(t, loaded)).Upsert) != 0 {
		t.Fatal("unchanged round-trip state produces entity writes")
	}
}

func mustEncodeMetadata(t *testing.T, state *State) metadataRecords {
	t.Helper()
	records, err := encodeMetadata(state)
	if err != nil {
		t.Fatal(err)
	}
	return records
}

func TestMetadataDeltaOnePostAndOneGrantDoNotRewriteRetainedHistory(t *testing.T) {
	state := metadataFixture(t)
	for n := 0; n < 10000; n++ {
		id := fmt.Sprintf("retained-%d", n)
		state.Observed[id] = Observation{Seq: int64(n + 100), FirstSeenSeq: int64(n + 100), PostID: id, ChannelID: "room", UpdateAt: int64(n)}
		state.Operations[id] = &Operation{ID: id, Kind: "post", Hash: id, Status: "complete", PostID: id, ChannelID: "room"}
	}
	before := mustEncodeMetadata(t, state)
	state.Sequence++
	state.Observed["new"] = Observation{Seq: state.Sequence, FirstSeenSeq: state.Sequence, PostID: "new", ChannelID: "room", UpdateAt: 999}
	state.Operations["new"] = &Operation{ID: "new", Actor: "codex:child", Kind: "post", Hash: "new", Status: "complete", PostID: "new", ChannelID: "room"}
	delta := compareMetadata(before, mustEncodeMetadata(t, state))
	if len(delta.Upsert) != 3 || len(delta.Delete) != 0 {
		t.Fatalf("one post requires %d upserts/%d deletes, want 3/0", len(delta.Upsert), len(delta.Delete))
	}
	before = mustEncodeMetadata(t, state)
	state.Conversations["room"].Grants["codex:another"] = Grant{Role: "observer", GrantedAt: 500}
	delta = compareMetadata(before, mustEncodeMetadata(t, state))
	if len(delta.Upsert) != 1 || delta.Upsert[0].Key.Collection != "@conversation_grants" {
		t.Fatalf("one grant changed unrelated entities: %#v", delta)
	}
	before = mustEncodeMetadata(t, state)
	delete(state.Conversations, "room")
	delta = compareMetadata(before, mustEncodeMetadata(t, state))
	if len(delta.Delete) != 5 || len(delta.Upsert) != 0 {
		t.Fatalf("conversation deletion did not include its grant/departure rows: %#v", delta)
	}
	var encodedBytes int
	for _, value := range before {
		encodedBytes += len(value)
	}
	t.Logf("compared %d metadata records / %d serialized payload bytes; one post writes 3 entity rows plus one revision row", len(before), encodedBytes)
}

func TestMetadataRejectsUnknownFutureFieldsAndCorruptOwners(t *testing.T) {
	state := metadataFixture(t)
	for _, change := range []func(metadataRecords){
		func(r metadataRecords) { r[metadataKey{"future_map", "x"}] = json.RawMessage(`{}`) },
		func(r metadataRecords) { delete(r, metadataKey{"@scalar", "sequence"}) },
		func(r metadataRecords) { r[metadataKey{"agents", "future"}] = json.RawMessage(`{"future_field":true}`) },
		func(r metadataRecords) { delete(r, metadataKey{"conversations", "room"}) },
	} {
		records := mustEncodeMetadata(t, state)
		change(records)
		if _, err := decodeMetadata(records); err == nil {
			t.Fatal("incompatible/corrupt metadata was accepted")
		}
	}
	for _, value := range []string{"null", "{} {}", `{"unknown_future_map":{}}`} {
		if _, err := decodeState([]byte(value)); err == nil {
			t.Fatalf("invalid legacy metadata was accepted: %s", value)
		}
	}
}

func TestMetadataSaveRequiresExplicitStoreAndFaultsClosed(t *testing.T) {
	p := &Plugin{}
	p.state.init()
	if err := p.persistState(&p.state); err == nil || p.fault == "" {
		t.Fatal("nil metadata store silently fell back to KV")
	}
}

type countingCanonicalHistory struct {
	canonicalHistory
	reads     int
	afterRead func()
}

func (h *countingCanonicalHistory) Posts(channel string) ([]*model.Post, error) {
	h.reads++
	if h.afterRead != nil {
		h.afterRead()
	}
	return h.canonicalHistory.Posts(channel)
}

func TestSharedRepairCoalescesConcurrentPollsAndForcesStartupRepair(t *testing.T) {
	p, f := fixture(t)
	channelConversation(t, p, "repair-room-1", "claude:b")
	channelConversation(t, p, "repair-room-2", "claude:b")
	history := &countingCanonicalHistory{canonicalHistory: f}
	p.history = history
	now := time.Unix(1000, 0)
	p.repairClock = func() time.Time { return now }
	p.mu.Lock()
	p.repair(true)
	p.mu.Unlock()
	if history.reads != 2 {
		t.Fatalf("forced startup repair read %d histories, want 2", history.reads)
	}
	var wait sync.WaitGroup
	for n := 0; n < 20; n++ {
		wait.Add(1)
		go func() {
			defer wait.Done()
			p.mu.Lock()
			defer p.mu.Unlock()
			p.repair(false)
		}()
	}
	wait.Wait()
	if history.reads != 2 {
		t.Fatalf("actor polling repeated canonical scans: %d", history.reads)
	}
	now = now.Add(canonicalRepairInterval)
	p.repair(false)
	if history.reads != 4 {
		t.Fatalf("due repair read %d histories, want 4", history.reads)
	}
	p.repair(true)
	if history.reads != 6 {
		t.Fatal("explicit full repair was suppressed by the cache")
	}
}

func TestSharedRepairIntervalStartsAfterSlowSweepCompletes(t *testing.T) {
	p, f := fixture(t)
	channelConversation(t, p, "slow-room", "claude:b")
	now := time.Unix(1000, 0)
	p.repairClock = func() time.Time { return now }
	history := &countingCanonicalHistory{canonicalHistory: f,
		afterRead: func() { now = now.Add(11 * time.Second) }}
	p.history = history
	p.repair(true)
	p.repair(false)
	if history.reads != 1 {
		t.Fatal("queued poll repeated a slow sweep immediately after completion")
	}
	now = now.Add(canonicalRepairInterval)
	p.repair(false)
	if history.reads != 2 {
		t.Fatal("slow sweep prevented the next due repair")
	}
}

func TestMetadataHealthExposesCompatibilityWithoutSecrets(t *testing.T) {
	p, _ := fixture(t)
	body := call(t, p, "health")
	encoded, _ := json.Marshal(body)
	compatibility := body["build"].(map[string]any)["compatibility"].(map[string]any)
	if compatibility["attention_protocol"] != float64(3) || compatibility["metadata_schema"] != float64(1) || compatibility["ledger_schema"] != float64(3) {
		t.Fatalf("incorrect naming compatibility boundary: %#v", compatibility)
	}
	for _, expected := range []string{"metadata_schema", metadataBackend, "source_sha256", "attention_protocol", "ledger_schema"} {
		if !strings.Contains(string(encoded), expected) {
			t.Fatalf("health missing %s: %s", expected, encoded)
		}
	}
	if strings.Contains(string(encoded), "test-token") {
		t.Fatal("health exposed coordinator credentials")
	}
}

func BenchmarkMetadataEncodeAndCompare(b *testing.B) {
	for _, size := range []int{1000, 10000} {
		b.Run(fmt.Sprint(size), func(b *testing.B) {
			state := &State{}
			state.init()
			for n := 0; n < size; n++ {
				id := fmt.Sprint(n)
				state.Observed[id] = Observation{Seq: int64(n + 1), FirstSeenSeq: int64(n + 1), PostID: id, ChannelID: "room"}
				state.Operations[id] = &Operation{ID: id, Kind: "post", Hash: id, Status: "complete", PostID: id}
			}
			before, err := encodeMetadata(state)
			if err != nil {
				b.Fatal(err)
			}
			state.Sequence++
			state.Observed["fresh"] = Observation{Seq: state.Sequence, FirstSeenSeq: state.Sequence, PostID: "fresh", ChannelID: "room"}
			b.ReportAllocs()
			b.ResetTimer()
			for n := 0; n < b.N; n++ {
				after, err := encodeMetadata(state)
				if err != nil {
					b.Fatal(err)
				}
				if delta := compareMetadata(before, after); len(delta.Upsert) != 2 {
					b.Fatal("benchmark no longer represents a single changed observation and sequence")
				}
			}
		})
	}
}
