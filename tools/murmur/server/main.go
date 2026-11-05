package main

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/mattermost/mattermost/server/public/plugin"
)

const version = "0.1.0"
const attentionProtocolVersion = 3

// The release build sets these; development builds remain visibly unversioned.
var buildRevision = "unversioned"
var buildSourceHash = "unversioned"

type configuration struct {
	CoordinatorToken              string `json:"coordinatortoken"`
	OperatorUserID                string `json:"operatoruserid"`
	AllowManagedConversationReads bool   `json:"allowmanagedconversationreads"`
}

type Plugin struct {
	plugin.MattermostPlugin
	mu              sync.Mutex
	state           State
	config          configuration
	fault           string
	lastErr         string
	stop            chan struct{}
	history         canonicalHistory
	metadata        metadataStore
	done            chan struct{}
	nextRepair      time.Time
	repairClock     func() time.Time
	repairErrors    map[string]string
	postHints       chan postHint
	postWake        chan struct{}
	workspaceReady  bool
	workspaceUsers  []string
	workspaceErrors map[string]string
	fileActive      map[string]bool
}

func main() { plugin.ClientMain(&Plugin{}) }

func (p *Plugin) OnConfigurationChange() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.loadConfiguration()
}

func (p *Plugin) loadConfiguration() error {
	// An omitted setting must disable it, not retain an earlier decoded value.
	var next configuration
	if err := p.API.LoadPluginConfiguration(&next); err != nil {
		return err
	}
	p.config = next
	return nil
}

func (p *Plugin) OnActivate() error {
	if p.API.GetServerVersion() != "12.0.1" {
		return bad(503, "Murmur's canonical history adapter supports Mattermost 12.0.1 only")
	}
	if err := p.loadConfiguration(); err != nil {
		return err
	}
	config := p.API.GetUnsanitizedConfig()
	if config.SqlSettings.DriverName == nil || *config.SqlSettings.DriverName != "postgres" || config.SqlSettings.DataSource == nil {
		return bad(503, "Murmur requires the pinned PostgreSQL server configuration")
	}
	history, err := openHistory(*config.SqlSettings.DataSource)
	if err != nil {
		return err
	}
	p.history = history
	store, err := openMetadata(*config.SqlSettings.DataSource, func() ([]byte, error) {
		data, appErr := p.API.KVGet("state-v1")
		if appErr != nil {
			return nil, appErr
		}
		return data, nil
	})
	if err != nil {
		_ = history.Close()
		return err
	}
	state, err := store.Load()
	if err != nil {
		_ = store.Close()
		_ = history.Close()
		return err
	}
	p.state, p.metadata = *state, store
	p.fault, p.lastErr, p.nextRepair = "", "", time.Time{}
	p.repairErrors = map[string]string{}
	p.initializePostHints()
	_ = p.loadWorkspaceUsers() // Workspace failures disable its API; agent journals stay independent.
	p.mu.Lock()
	p.repair(true)
	p.mu.Unlock()
	if p.fault != "" {
		_ = store.Close()
		_ = history.Close()
		return bad(503, "startup metadata persistence failed: "+p.fault)
	}
	p.stop = make(chan struct{})
	p.done = make(chan struct{})
	go func() {
		defer close(p.done)
		timer := time.NewTimer(canonicalRepairInterval)
		defer timer.Stop()
		for {
			select {
			case <-p.postWake:
			case <-timer.C:
			case <-p.stop:
				return
			}
			p.mu.Lock()
			p.repair(false)
			wait := time.Until(p.nextRepair)
			if wait <= 0 {
				wait = canonicalRepairInterval // A persistent fault must not spin.
			}
			p.mu.Unlock()
			// Reset to the actual completed-repair deadline. A fixed ticker can
			// otherwise fire just before it and skip an entire repair interval.
			timer.Reset(wait)
		}
	}()
	return nil
}

func (p *Plugin) OnDeactivate() error {
	if p.stop != nil {
		close(p.stop)
		if p.done != nil {
			<-p.done
		}
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	var failures []error
	if p.metadata != nil {
		failures = append(failures, p.metadata.Close())
	}
	if history, ok := p.history.(*postgresHistory); ok {
		failures = append(failures, history.Close())
	}
	return errors.Join(failures...)
}

func (p *Plugin) ServeHTTP(_ *plugin.Context, w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	path := strings.TrimPrefix(r.URL.Path, "/plugins/murmur")
	if p.serveAttachment(w, r, path) {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if strings.HasPrefix(path, "/operator/") {
		p.serveOperator(w, r, path)
		return
	}
	token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if !strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ") || p.config.CoordinatorToken == "" || subtle.ConstantTimeCompare([]byte(token), []byte(p.config.CoordinatorToken)) != 1 {
		writeError(w, http.StatusUnauthorized, "coordinator authentication required")
		return
	}
	if p.fault != "" {
		writeError(w, http.StatusServiceUnavailable, "persistent state unavailable: "+p.fault)
		return
	}
	if path == "/v1/health" && r.Method == http.MethodGet {
		writeJSON(w, map[string]any{"version": version, "reconciliation_error": p.lastErr,
			"capabilities": map[string]any{"attachments": p.attachmentCapability(), "managed_conversation_reads": p.managedReadCapability(),
				"attention_boundaries": map[string]any{"version": 1}},
			"build": map[string]any{"revision": buildRevision, "source_sha256": buildSourceHash,
				"compatibility": map[string]any{"attention_protocol": attentionProtocolVersion, "metadata_schema": metadataSchemaVersion,
					"metadata_backend": metadataBackend, "mattermost": "12.0.1", "postgres_major": 16, "ledger_schema": 5}}})
		return
	}
	result, err := p.route(strings.Split(strings.Trim(path, "/"), "/"), r)
	if err != nil {
		if failure, ok := err.(*httpError); ok {
			writeError(w, failure.status, failure.Error())
		} else {
			writeError(w, http.StatusBadGateway, err.Error())
		}
		return
	}
	writeJSON(w, result)
}

type httpError struct {
	status int
	text   string
}

func (e *httpError) Error() string               { return e.text }
func bad(status int, text string) error          { return &httpError{status, text} }
func writeJSON(w http.ResponseWriter, value any) { _ = json.NewEncoder(w).Encode(value) }
func writeError(w http.ResponseWriter, status int, text string) {
	w.WriteHeader(status)
	writeJSON(w, map[string]string{"error": text})
}

func decode(r *http.Request, dst any) error {
	reader := http.MaxBytesReader(nil, r.Body, 64*1024)
	d := json.NewDecoder(reader)
	d.DisallowUnknownFields()
	if err := d.Decode(dst); err != nil {
		return bad(400, "invalid JSON request: "+err.Error())
	}
	return nil
}
