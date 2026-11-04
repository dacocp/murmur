package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/mattermost/mattermost/server/public/model"
)

// Native uploads/files live separately from committed Murmur metadata. Lost
// responses keep their native effects, and post creation binds each file once.
type attachmentAPI struct {
	*fakeAPI
	sessions                                  map[string]*model.UploadSession
	files                                     map[string]*model.FileInfo
	data                                      map[string][]byte
	limit                                     int64
	disabled, missingDriver                   bool
	createSessionCalls, uploadCalls, getCalls int
	sessionFault, uploadFault                 string
	infoFault                                 bool
	withholdBinding                           bool
	keepFiles                                 int
	beforeSession                             func(*model.UploadSession)
	beforeUpload                              func(*model.UploadSession)
	beforeGet                                 func(string)
	afterUpload                               func()
	afterGet                                  func(string)
	afterInfo                                 func(string)
}

func (f *attachmentAPI) GetConfig() *model.Config {
	config := f.fakeAPI.GetConfig()
	config.FileSettings.MaxFileSize = &f.limit
	if f.disabled {
		enabled := false
		config.FileSettings.EnableFileAttachments = &enabled
	}
	if f.missingDriver {
		driver := ""
		config.FileSettings.DriverName = &driver
	}
	return config
}

func (f *attachmentAPI) HasPermissionTo(_ string, permission *model.Permission) bool {
	return permission.Id == model.PermissionManageSystem.Id
}

func (f *attachmentAPI) CreateUploadSession(session *model.UploadSession) (*model.UploadSession, error) {
	f.createSessionCalls++
	if f.beforeSession != nil {
		f.beforeSession(session)
	}
	if f.sessionFault == "before" {
		return nil, unavailable()
	}
	if f.sessions[session.Id] != nil {
		return nil, fmt.Errorf("duplicate native upload session")
	}
	copy := *session
	copy.CreateAt, copy.Path = 1000, "native/"+session.Id+"/"+session.Filename
	f.sessions[session.Id] = &copy
	if f.sessionFault == "after" {
		return nil, unavailable()
	}
	return &copy, nil
}

func (f *attachmentAPI) UploadData(session *model.UploadSession, reader io.Reader) (*model.FileInfo, error) {
	f.uploadCalls++
	if f.beforeUpload != nil {
		f.beforeUpload(session)
	}
	if f.uploadFault == "before" {
		return nil, unavailable()
	}
	stored := f.sessions[session.Id]
	if stored == nil || stored.ReqFileId != session.ReqFileId || stored.UserId != session.UserId || stored.ChannelId != session.ChannelId || stored.FileOffset != 0 {
		return nil, fmt.Errorf("native upload has no matching unconsumed session")
	}
	data, err := io.ReadAll(reader)
	if err != nil {
		return nil, err
	}
	if int64(len(data)) != session.FileSize {
		return nil, fmt.Errorf("native upload length mismatch")
	}
	stored.FileOffset = int64(len(data))
	info := &model.FileInfo{Id: session.ReqFileId, CreatorId: session.UserId, ChannelId: session.ChannelId,
		Name: session.Filename, Size: int64(len(data)), MimeType: http.DetectContentType(data)}
	f.files[info.Id], f.data[info.Id] = info, append([]byte(nil), data...)
	delete(f.sessions, session.Id)
	if f.afterUpload != nil {
		f.afterUpload()
	}
	if f.uploadFault == "after" {
		return nil, unavailable()
	}
	return info, nil
}

func (f *attachmentAPI) GetFileInfo(id string) (*model.FileInfo, *model.AppError) {
	if f.infoFault {
		return nil, unavailable()
	}
	if info := f.files[id]; info != nil {
		copy := *info
		if f.afterInfo != nil {
			f.afterInfo(id)
		}
		return &copy, nil
	}
	return nil, missing()
}

func (f *attachmentAPI) GetFile(id string) ([]byte, *model.AppError) {
	f.getCalls++
	if f.beforeGet != nil {
		f.beforeGet(id)
	}
	data, ok := f.data[id]
	if !ok {
		return nil, missing()
	}
	if f.afterGet != nil {
		f.afterGet(id)
	}
	return append([]byte(nil), data...), nil
}

func (f *attachmentAPI) CreatePost(post *model.Post) (*model.Post, *model.AppError) {
	// Mattermost 12.0.1 sorts FileIds in its native precommit step.
	post.PreCommit()
	wanted := append([]string(nil), post.FileIds...)
	post.FileIds = nil
	for _, id := range wanted {
		info := f.files[id]
		if info != nil && info.PostId == "" && info.CreatorId == post.UserId && info.ChannelId == post.ChannelId &&
			(f.keepFiles < 0 || len(post.FileIds) < f.keepFiles) {
			post.FileIds = append(post.FileIds, id)
		}
	}
	result, err := f.fakeAPI.CreatePost(post)
	if post.Id != "" && !f.withholdBinding {
		for _, id := range post.FileIds {
			f.files[id].PostId = post.Id
		}
	}
	return result, err
}

func attachmentFixture(t *testing.T) (*Plugin, *attachmentAPI, *model.User, string) {
	t.Helper()
	p, base := fixture(t)
	f := &attachmentAPI{fakeAPI: base, sessions: map[string]*model.UploadSession{}, files: map[string]*model.FileInfo{},
		data: map[string][]byte{}, limit: 1024 * 1024, keepFiles: -1}
	p.API = f
	user := &model.User{Id: model.NewId(), Username: "attachment-operator", Roles: "system_user system_admin"}
	f.users[user.Username] = user
	channel := channelConversation(t, p, "attachment-room", "claude:b")
	return p, f, user, channel
}

func reloadAttachments(t *testing.T, p *Plugin, f *attachmentAPI) *Plugin {
	t.Helper()
	next := &Plugin{config: p.config, metadata: f.metadata, history: f.fakeAPI, repairClock: p.repairClock}
	next.API = f
	state, err := next.metadata.Load()
	if err != nil {
		t.Fatal(err)
	}
	next.state = *state
	next.reconcile()
	if next.lastErr != "" {
		t.Fatal(next.lastErr)
	}
	return next
}

func uploadHTTP(actor, human, op, channel, name string, data []byte) *http.Request {
	checksum := sha256.Sum256(data)
	q := url.Values{"actor": {actor}, "op_id": {op}, "channel_id": {channel}, "filename": {name},
		"size": {fmt.Sprint(len(data))}, "sha256": {hex.EncodeToString(checksum[:])}}
	base := "/plugins/murmur/v1/files"
	if human != "" {
		base = "/plugins/murmur/operator/v1/files"
	}
	r := httptest.NewRequest(http.MethodPost, base+"?"+q.Encode(), bytes.NewReader(data))
	r.Header.Set("Content-Type", "application/octet-stream")
	if human == "" {
		r.Header.Set("Authorization", "Bearer test-token")
	} else {
		r.Header.Set("Mattermost-User-Id", human)
	}
	return r
}

func attachmentRequest(t *testing.T, p *Plugin, r *http.Request, want int) *httptest.ResponseRecorder {
	t.Helper()
	w := httptest.NewRecorder()
	p.ServeHTTP(nil, w, r)
	if w.Code != want {
		t.Fatalf("%s %s: want %d, got %d: %s", r.Method, r.URL.Path, want, w.Code, w.Body)
	}
	return w
}

func attachmentJSON(t *testing.T, w *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var value map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &value); err != nil {
		t.Fatal(err, w.Body.String())
	}
	return value
}

func uploadedFile(t *testing.T, p *Plugin, actor, human, op, channel string) string {
	t.Helper()
	w := attachmentRequest(t, p, uploadHTTP(actor, human, op, channel, "plan.html", []byte("<html>untrusted attachment</html>")), 200)
	return attachmentJSON(t, w)["file"].(map[string]any)["id"].(string)
}

func postedFileHTTP(actor, human, post, file string, info bool) *http.Request {
	base := "/plugins/murmur/v1/posts/"
	if human != "" {
		base = "/plugins/murmur/operator/v1/posts/"
	}
	path := base + post + "/files/" + file
	if info {
		path += "/info"
	}
	r := httptest.NewRequest(http.MethodGet, path+"?actor="+url.QueryEscape(actor), nil)
	if human == "" {
		r.Header.Set("Authorization", "Bearer test-token")
	} else {
		r.Header.Set("Mattermost-User-Id", human)
	}
	return r
}

func attachmentPost(t *testing.T, p *Plugin, actor, human, op, channel, root string, files []string, want int) map[string]any {
	t.Helper()
	body := map[string]any{"op_id": op, "channel_id": channel, "message": "", "root_id": root, "file_ids": files}
	path := "/plugins/murmur/v1/posts"
	if human == "" {
		body["actor"] = actor
	} else {
		path = "/plugins/murmur/operator/v1/posts"
	}
	data, _ := json.Marshal(body)
	r := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(data))
	r.Header.Set("Content-Type", "application/json")
	if human == "" {
		r.Header.Set("Authorization", "Bearer test-token")
	} else {
		r.Header.Set("Mattermost-User-Id", human)
	}
	return attachmentJSON(t, attachmentRequest(t, p, r, want))
}

func TestAttachmentsRejectInvalidSourcesAndSendersBeforeNativeEffects(t *testing.T) {
	cases := []struct {
		name   string
		want   int
		change func(*http.Request)
	}{
		{"hash", 400, func(r *http.Request) {
			q := r.URL.Query()
			q.Set("sha256", strings.Repeat("0", 64))
			r.URL.RawQuery = q.Encode()
		}},
		{"short body", 400, func(r *http.Request) { r.Body = io.NopCloser(strings.NewReader("short")) }},
		{"long body", 400, func(r *http.Request) { r.Body = io.NopCloser(strings.NewReader("too many bytes")) }},
		{"traversal", 400, func(r *http.Request) {
			q := r.URL.Query()
			q.Set("filename", "../plan.html")
			r.URL.RawQuery = q.Encode()
		}},
		{"bidi", 400, func(r *http.Request) {
			q := r.URL.Query()
			q.Set("filename", "plan\u202e.html")
			r.URL.RawQuery = q.Encode()
		}},
		{"oversize", 413, func(r *http.Request) { q := r.URL.Query(); q.Set("size", "1048577"); r.URL.RawQuery = q.Encode() }},
		{"zero", 400, func(r *http.Request) { q := r.URL.Query(); q.Set("size", "0"); r.URL.RawQuery = q.Encode() }},
		{"repeated field", 400, func(r *http.Request) { q := r.URL.Query(); q.Add("filename", "other"); r.URL.RawQuery = q.Encode() }},
		{"wrong media", 415, func(r *http.Request) { r.Header.Set("Content-Type", "text/html") }},
		{"no token", 401, func(r *http.Request) { r.Header.Del("Authorization") }},
		{"unknown actor", 403, func(r *http.Request) {
			q := r.URL.Query()
			q.Set("actor", "codex:missing")
			r.URL.RawQuery = q.Encode()
		}},
		{"not member", 403, func(r *http.Request) { q := r.URL.Query(); q.Set("actor", "codex:x"); r.URL.RawQuery = q.Encode() }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p, f, _, channel := attachmentFixture(t)
			r := uploadHTTP("codex:a", "", "rejected", channel, "plan.html", []byte("original"))
			tc.change(r)
			attachmentRequest(t, p, r, tc.want)
			if f.createSessionCalls != 0 || f.uploadCalls != 0 || p.state.Operations["rejected"] != nil {
				t.Fatal("rejected source reached native upload/journal")
			}
		})
	}
	p, f, user, channel := attachmentFixture(t)
	r := uploadHTTP("codex:a", user.Id, "switched", channel, "plan.html", []byte("original"))
	r.Header.Set("X-Murmur-User-Id", model.NewId())
	attachmentRequest(t, p, r, 409)
	if f.createSessionCalls != 0 {
		t.Fatal("changed human identity reached native upload")
	}
}

func TestAttachmentsPersistReservationsBeforeNativeCallsAndKeepBytesOutOfJournal(t *testing.T) {
	p, f, _, channel := attachmentFixture(t)
	body := []byte("unique-file-bytes-must-not-enter-plugin-metadata")
	check := func(stage string, session *model.UploadSession) {
		var disk State
		if err := json.Unmarshal(f.kv, &disk); err != nil {
			t.Fatal(err)
		}
		o := disk.Operations["upload"]
		if o == nil || o.Upload == nil || o.Stage != stage || o.Upload.SessionID != session.Id || o.Upload.FileID != session.ReqFileId ||
			o.Upload.UserID != p.state.Agents["codex:a"].UserID || o.ChannelID != channel || o.Upload.Size != int64(len(body)) {
			t.Fatal("native effect preceded exact durable reservation", o)
		}
		if bytes.Contains(f.kv, body) {
			t.Fatal("journal copied attachment bytes")
		}
	}
	f.beforeSession = func(s *model.UploadSession) { check("session", s) }
	f.beforeUpload = func(s *model.UploadSession) { check("uploading", s) }
	w := attachmentRequest(t, p, uploadHTTP("codex:a", "", "upload", channel, "plan.txt", body), 200)
	file := attachmentJSON(t, w)["file"].(map[string]any)["id"].(string)
	if !bytes.Equal(f.data[file], body) || f.files[file].CreatorId != p.state.Agents["codex:a"].UserID || f.files[file].ChannelId != channel {
		t.Fatal("native bytes/owner/channel differ")
	}
	f.kvFault = true
	attachmentRequest(t, p, uploadHTTP("codex:a", "", "failed-journal", channel, "plan.txt", body), 503)
	if f.createSessionCalls != 1 || f.uploadCalls != 1 {
		t.Fatal("native effect ran despite failed intent save")
	}
}

func TestAttachmentsNativeDisabledPolicyRejectsBeforeUpload(t *testing.T) {
	for _, mode := range []string{"disabled", "no driver"} {
		t.Run(mode, func(t *testing.T) {
			p, f, _, channel := attachmentFixture(t)
			f.disabled, f.missingDriver = mode == "disabled", mode == "no driver"
			capability := p.attachmentCapability()
			if capability["enabled"] != false {
				t.Fatal("disabled native storage advertised attachments", capability)
			}
			attachmentRequest(t, p, uploadHTTP("codex:a", "", "disabled", channel, "plan.txt", []byte("original")), 403)
			if f.createSessionCalls != 0 || f.uploadCalls != 0 {
				t.Fatal("disabled native policy allowed upload")
			}
		})
	}
}

func TestAttachmentsNativePolicyChangeAfterReservationStopsByteWrite(t *testing.T) {
	for _, mode := range []string{"disabled", "no driver", "smaller limit"} {
		t.Run(mode, func(t *testing.T) {
			p, f, _, channel := attachmentFixture(t)
			f.beforeSession = func(*model.UploadSession) {
				f.disabled, f.missingDriver = mode == "disabled", mode == "no driver"
				if mode == "smaller limit" {
					f.limit = 1
				}
			}
			attachmentRequest(t, p, uploadHTTP("codex:a", "", "changed-policy", channel, "plan.txt", []byte("original")), 409)
			if f.createSessionCalls != 1 || len(f.sessions) != 1 || f.uploadCalls != 0 || p.state.Operations["changed-policy"].Status != "uncertain" {
				t.Fatal("changed native policy allowed bytes or hid the reserved session")
			}
		})
	}
}

func TestAttachmentsRecoverLostResponsesWithoutResendingBytes(t *testing.T) {
	for _, fault := range []string{"session", "upload", "lookup", "completion"} {
		t.Run(fault, func(t *testing.T) {
			p, f, _, channel := attachmentFixture(t)
			want := 200
			switch fault {
			case "session":
				f.sessionFault = "after"
				want = 409
			case "upload":
				f.uploadFault = "after"
			case "lookup":
				f.afterUpload = func() { f.infoFault = true }
				want = 409
			case "completion":
				f.afterUpload = func() { f.kvFault = true }
				want = 503
			}
			request := func() *http.Request {
				return uploadHTTP("codex:a", "", "recover", channel, "plan.txt", []byte("recover exactly once"))
			}
			attachmentRequest(t, p, request(), want)
			sessionCalls, uploadCalls := f.createSessionCalls, f.uploadCalls
			f.kvFault, f.infoFault, f.afterUpload = false, false, nil
			p = reloadAttachments(t, p, f)
			path := "operations/recover?actor=" + url.QueryEscape("codex:a")
			op := call(t, p, path)
			if fault == "session" {
				if op["status"] != "uncertain" || f.uploadCalls != 0 {
					t.Fatal("lost session response caused a body write", op)
				}
				attachmentRequest(t, p, request(), 409)
			} else {
				if op["status"] != "complete" {
					t.Fatal("reserved native file was not recovered", op)
				}
				result := op["result"].(map[string]any)
				if result["file_id"] != p.state.Operations["recover"].Upload.FileID || result["channel_id"] != channel || result["file"].(map[string]any)["name"] != "plan.txt" {
					t.Fatal("recovered result lost safe metadata", result)
				}
				attachmentRequest(t, p, request(), 200)
			}
			if f.createSessionCalls != sessionCalls || f.uploadCalls != uploadCalls {
				t.Fatal("retry repeated native upload")
			}
		})
	}
}

func TestAttachmentsCompletedUploadRemainsCompleteAfterFileDeletion(t *testing.T) {
	p, f, _, channel := attachmentFixture(t)
	file := uploadedFile(t, p, "codex:a", "", "completed", channel)
	delete(f.files, file)
	delete(f.data, file)
	p = reloadAttachments(t, p, f)
	reads := f.getCalls
	op := call(t, p, "operations/completed?actor=codex:a")
	if op["status"] != "complete" || op["result"].(map[string]any)["file_id"] != file {
		t.Fatal("later deletion regressed historical completion", op)
	}
	retry := uploadedFile(t, p, "codex:a", "", "completed", channel)
	if retry != file || f.createSessionCalls != 1 || f.uploadCalls != 1 || f.getCalls != reads {
		t.Fatal("completed operation was revalidated or recreated after deletion")
	}
}

func TestAttachmentsBindUploadOperationsToImmutableSenderAndSource(t *testing.T) {
	p, f, user, channel := attachmentFixture(t)
	file := uploadedFile(t, p, "codex:a", "", "same-op", channel)
	for _, r := range []*http.Request{
		uploadHTTP("codex:a", "", "same-op", channel, "renamed.html", []byte("<html>untrusted attachment</html>")),
		uploadHTTP("claude:b", "", "same-op", channel, "plan.html", []byte("<html>untrusted attachment</html>")),
		uploadHTTP("codex:a", "", "same-op", channel, "plan.html", []byte("different bytes")),
	} {
		attachmentRequest(t, p, r, 409)
	}
	humanFile := uploadedFile(t, p, "codex:a", user.Id, "human-upload", channel)
	if f.files[humanFile].CreatorId != user.Id {
		t.Fatal("operator query selected bot instead of actual human")
	}
	other := &model.User{Id: model.NewId(), Username: "second-human", Roles: "system_admin"}
	f.users[other.Username] = other
	attachmentRequest(t, p, uploadHTTP("", other.Id, "human-upload", channel, "plan.html", []byte("<html>untrusted attachment</html>")), 409)
	for _, tc := range []struct{ human, actor, op string }{{"", "claude:b", "same-op"}, {other.Id, "", "human-upload"}, {user.Id, "", "same-op"}} {
		base := "/plugins/murmur/v1/"
		if tc.human != "" {
			base = "/plugins/murmur/operator/v1/"
		}
		r := httptest.NewRequest("GET", base+"operations/"+tc.op+"?actor="+url.QueryEscape(tc.actor), nil)
		if tc.human != "" {
			r.Header.Set("Mattermost-User-Id", tc.human)
		} else {
			r.Header.Set("Authorization", "Bearer test-token")
		}
		attachmentRequest(t, p, r, 403)
	}
	if f.uploadCalls != 2 {
		t.Fatal("immutable-source checks caused native uploads")
	}
	attachmentRequest(t, p, postedFileHTTP("codex:a", "", model.NewId(), file, false), 404)
}

func TestAttachmentsRecoveryRejectsSubstitutedNativeMetadataOrBytes(t *testing.T) {
	p, f, _, channel := attachmentFixture(t)
	body := []byte("exact reserved native bytes")
	f.uploadFault = "before"
	attachmentRequest(t, p, uploadHTTP("codex:a", "", "uncertain", channel, "plan.txt", body), 409)
	o := p.state.Operations["uncertain"]
	seed := func() {
		f.files[o.Upload.FileID] = &model.FileInfo{Id: o.Upload.FileID, CreatorId: o.Upload.UserID,
			ChannelId: channel, Name: "plan.txt", Size: int64(len(body)), MimeType: "text/plain"}
		f.data[o.Upload.FileID] = append([]byte(nil), body...)
	}
	changes := []struct {
		name   string
		change func(*model.FileInfo)
	}{
		{"id", func(info *model.FileInfo) { info.Id = model.NewId() }},
		{"owner", func(info *model.FileInfo) { info.CreatorId = model.NewId() }},
		{"channel", func(info *model.FileInfo) { info.ChannelId = model.NewId() }},
		{"name", func(info *model.FileInfo) { info.Name = "other.txt" }},
		{"size", func(info *model.FileInfo) { info.Size++ }},
		{"deleted", func(info *model.FileInfo) { info.DeleteAt = 1 }},
		{"bytes", func(info *model.FileInfo) { f.data[info.Id] = bytes.Repeat([]byte("x"), len(body)) }},
	}
	for _, tc := range changes {
		t.Run(tc.name, func(t *testing.T) {
			seed()
			tc.change(f.files[o.Upload.FileID])
			if result := call(t, p, "operations/uncertain?actor=codex:a"); result["status"] != "uncertain" {
				t.Fatal("substituted native file proved upload completion", result)
			}
		})
	}
	seed()
	if result := call(t, p, "operations/uncertain?actor=codex:a"); result["status"] != "complete" {
		t.Fatal("exact late native file was not recovered", result)
	}
	if f.createSessionCalls != 1 || f.uploadCalls != 1 {
		t.Fatal("reconciliation resubmitted native upload")
	}
}

func TestAttachmentsAttachmentOnlyPostsBindNativeFilesAndPreserveOldTextHashes(t *testing.T) {
	p, f, user, channel := attachmentFixture(t)
	first := uploadedFile(t, p, "codex:a", "", "root-upload", channel)
	root := attachmentPost(t, p, "codex:a", "", "root-post", channel, "", []string{first}, 200)["post"].(map[string]any)
	rootID := root["id"].(string)
	if root["message"] != "" || !reflect.DeepEqual(root["file_ids"], []any{first}) || len(root["files"].([]any)) != 1 || f.files[first].PostId != rootID {
		t.Fatal("attachment-only root was not bound", root)
	}
	second := uploadedFile(t, p, "codex:a", "", "reply-upload", channel)
	reply := attachmentPost(t, p, "codex:a", "", "reply-post", channel, rootID, []string{second}, 200)["post"].(map[string]any)
	if reply["root_id"] != rootID || f.files[second].PostId != reply["id"] {
		t.Fatal("attachment-only reply lost native root/binding", reply)
	}
	humanFile := uploadedFile(t, p, "", user.Id, "human-file", channel)
	attachmentPost(t, p, "", user.Id, "human-post", channel, rootID, []string{humanFile}, 200)
	for _, req := range []postRequest{{Actor: "codex:a", OpID: "text", ChannelID: channel, Message: "text"}, {Actor: "codex:a", OpID: "text", ChannelID: channel, Message: "text", FileIDs: []string{}}} {
		old := sha256.Sum256([]byte(fmt.Sprintf(`{"actor":"codex:a","op_id":"text","channel_id":%q,"message":"text"}`, channel)))
		if digest(req) != hex.EncodeToString(old[:]) {
			t.Fatal("empty attachment field changed old post hash")
		}
	}
	for _, ids := range [][]string{nil, {}} {
		req := operatorPostRequest{OpID: "human-text", ChannelID: channel, Message: "text", FileIDs: ids}
		old := sha256.Sum256([]byte(fmt.Sprintf(`{"op_id":"human-text","channel_id":%q,"message":"text"}`, channel)))
		if digest(req) != hex.EncodeToString(old[:]) {
			t.Fatal("empty attachment field changed old human post hash")
		}
	}
	if postPreview(f.posts[rootID]) == "" {
		t.Fatal("attachment-only post has a blank preview")
	}
}

func TestAttachmentsReverseFileIDsUseNativeCanonicalOrderAcrossLostResponseAndRestart(t *testing.T) {
	for _, operator := range []bool{false, true} {
		t.Run(fmt.Sprint("operator=", operator), func(t *testing.T) {
			p, f, user, channel := attachmentFixture(t)
			actor, human := "codex:a", ""
			if operator {
				actor, human = "", user.Id
			}
			ordered := []string{uploadedFile(t, p, actor, human, "first-file", channel), uploadedFile(t, p, actor, human, "second-file", channel)}
			sort.Strings(ordered)
			reversed := []string{ordered[1], ordered[0]}
			original := append([]string(nil), reversed...)
			if reversed[0] <= reversed[1] {
				t.Fatal("fixture must send two distinct IDs in reverse native order")
			}
			sendFiles := func(op string, ids []string) (map[string]any, error) {
				if operator {
					return p.operatorSend(human, operatorPostRequest{OpID: op, ChannelID: channel, FileIDs: ids})
				}
				return p.send(postRequest{Actor: actor, OpID: op, ChannelID: channel, FileIDs: ids})
			}
			// The native post commits and its response is lost. Recovery must
			// match the canonical order, without sorting the caller's slice.
			f.postFault = "after"
			result, err := sendFiles("canonical-post", reversed)
			if err != nil {
				t.Fatal("native sorting made a genuine lost-response echo uncertain", err)
			}
			if !reflect.DeepEqual(reversed, original) {
				t.Fatal("canonicalization mutated the caller's file IDs", reversed)
			}
			post := result["post"].(map[string]any)
			postID := post["id"].(string)
			if !reflect.DeepEqual(post["file_ids"], ordered) || !reflect.DeepEqual(f.posts[postID].FileIds, model.StringArray(ordered)) {
				t.Fatal("post and native file order differ", post)
			}
			metadataIDs := []string{}
			for _, file := range post["files"].([]map[string]any) {
				metadataIDs = append(metadataIDs, file["id"].(string))
			}
			if !reflect.DeepEqual(metadataIDs, ordered) {
				t.Fatal("attachment metadata order differs from native Post.FileIds", metadataIDs)
			}
			canonicalHash := digest(postRequest{Actor: actor, OpID: "canonical-post", ChannelID: channel, FileIDs: ordered})
			if operator {
				canonicalHash = digest(operatorPostRequest{OpID: "canonical-post", ChannelID: channel, FileIDs: ordered})
			}
			var disk State
			if err := json.Unmarshal(f.kv, &disk); err != nil {
				t.Fatal(err)
			}
			op := disk.Operations["canonical-post"]
			if op == nil || op.Status != "complete" || op.PostID != postID || op.Hash != canonicalHash {
				t.Fatal("canonical send identity/completion was not durable", op)
			}
			p = reloadAttachments(t, p, f)
			replayed, err := sendFiles("canonical-post", reversed)
			if err != nil || replayed["post"].(map[string]any)["id"] != postID || f.creates != 1 || !reflect.DeepEqual(reversed, original) {
				t.Fatal("restart repeated the native post or changed the caller slice", replayed, err)
			}
			unused := uploadedFile(t, p, actor, human, "unused-duplicate-file", channel)
			_, duplicateErr := sendFiles("duplicate-post", []string{unused, unused})
			status(t, duplicateErr, 400)
			if f.creates != 1 || p.state.Operations["duplicate-post"] != nil {
				t.Fatal("duplicate IDs reached the native post/journal")
			}
		})
	}
}

func TestAttachmentsPostRejectsCrossOwnerChannelDuplicatesAndBoundFiles(t *testing.T) {
	p, f, user, channel := attachmentFixture(t)
	file := uploadedFile(t, p, "codex:a", "", "upload", channel)
	otherChannel := channelConversation(t, p, "other-room", "claude:b")
	before := f.creates
	attachmentPost(t, p, "claude:b", "", "wrong-owner", channel, "", []string{file}, 403)
	attachmentPost(t, p, "codex:a", "", "wrong-room", otherChannel, "", []string{file}, 403)
	attachmentPost(t, p, "codex:a", "", "duplicate", channel, "", []string{file, file}, 400)
	attachmentPost(t, p, "", user.Id, "human-steal", channel, "", []string{file}, 403)
	if f.creates != before {
		t.Fatal("invalid attachment reached native create")
	}
	attachmentPost(t, p, "codex:a", "", "valid", channel, "", []string{file}, 200)
	before = f.creates
	attachmentPost(t, p, "codex:a", "", "reuse-bound", channel, "", []string{file}, 409)
	if f.creates != before {
		t.Fatal("already-bound file produced another native post")
	}
}

func TestAttachmentsPartialCanonicalBindingNeverCompletesOrReposts(t *testing.T) {
	for _, count := range []int{0, 1} {
		t.Run(fmt.Sprint(count), func(t *testing.T) {
			p, f, _, channel := attachmentFixture(t)
			ids := []string{uploadedFile(t, p, "codex:a", "", "first", channel), uploadedFile(t, p, "codex:a", "", "second", channel)}
			f.keepFiles = count
			attachmentPost(t, p, "codex:a", "", "partial", channel, "", ids, 409)
			if f.creates != 1 || p.state.Operations["partial"].Status != "uncertain" {
				t.Fatal("partial native binding was reported complete")
			}
			p = reloadAttachments(t, p, f)
			attachmentPost(t, p, "codex:a", "", "partial", channel, "", ids, 409)
			if f.creates != 1 {
				t.Fatal("partial binding retried the post")
			}
		})
	}
	p, f, _, channel := attachmentFixture(t)
	id := uploadedFile(t, p, "codex:a", "", "upload", channel)
	f.postFault = "after"
	first := attachmentPost(t, p, "codex:a", "", "lost-post", channel, "", []string{id}, 200)["post"].(map[string]any)
	p = reloadAttachments(t, p, f)
	second := attachmentPost(t, p, "codex:a", "", "lost-post", channel, "", []string{id}, 200)["post"].(map[string]any)
	if f.creates != 1 || first["id"] != second["id"] {
		t.Fatal("lost post response duplicated the native post")
	}
}

func TestAttachmentsCanonicalIDsWithoutNativeBindingRemainUncertain(t *testing.T) {
	for _, operator := range []bool{false, true} {
		t.Run(fmt.Sprint("operator=", operator), func(t *testing.T) {
			p, f, user, channel := attachmentFixture(t)
			actor, human := "codex:a", ""
			if operator {
				actor, human = "", user.Id
			}
			file := uploadedFile(t, p, actor, human, "upload", channel)
			f.withholdBinding = true
			attachmentPost(t, p, actor, human, "unbound", channel, "", []string{file}, 409)
			post := p.state.Operations["unbound"].PostID
			if f.creates != 1 || post == "" || !reflect.DeepEqual(f.posts[post].FileIds, model.StringArray{file}) || f.files[file].PostId != "" {
				t.Fatal("fixture did not preserve canonical IDs with missing native binding")
			}
			p = reloadAttachments(t, p, f)
			attachmentPost(t, p, actor, human, "unbound", channel, "", []string{file}, 409)
			if f.creates != 1 || p.state.Operations["unbound"].Status != "uncertain" {
				t.Fatal("unbound canonical IDs completed or repeated the native post")
			}
			// A later native binding can prove the original effect; it does not
			// authorize another CreatePost or another upload.
			f.files[file].PostId = post
			p = reloadAttachments(t, p, f)
			result := attachmentPost(t, p, actor, human, "unbound", channel, "", []string{file}, 200)["post"].(map[string]any)
			if result["id"] != post || f.creates != 1 || f.uploadCalls != 1 {
				t.Fatal("late native binding did not recover the original operation", result)
			}
		})
	}
}

func TestAttachmentsPostedDownloadsRespectActualPostBindingAndObserverAccess(t *testing.T) {
	p, f, user, _ := attachmentFixture(t)
	channel := legacyPrivateChannel(t, p, "private-attachments", "claude:b")
	mustRegister(t, p, "codex:child", "codex:a")
	file := uploadedFile(t, p, "codex:a", "", "upload", channel)
	post := attachmentPost(t, p, "codex:a", "", "post", channel, "", []string{file}, 200)["post"].(map[string]any)["id"].(string)
	before, _ := json.Marshal(p.state)
	members, _ := json.Marshal(f.members)
	for _, who := range []struct{ actor, human string }{{"codex:a", ""}, {"codex:child", ""}, {"", user.Id}} {
		w := attachmentRequest(t, p, postedFileHTTP(who.actor, who.human, post, file, false), 200)
		checksum := sha256.Sum256(f.data[file])
		if !bytes.Equal(w.Body.Bytes(), f.data[file]) || w.Header().Get("X-Content-SHA256") != hex.EncodeToString(checksum[:]) ||
			w.Header().Get("Content-Type") != "application/octet-stream" || !strings.HasPrefix(w.Header().Get("Content-Disposition"), "attachment;") ||
			w.Header().Get("X-Content-Type-Options") != "nosniff" || w.Header().Get("Content-Security-Policy") != "sandbox" || w.Header().Get("Cache-Control") != "private, no-store" {
			t.Fatal("unsafe/corrupted binary response", w.Header())
		}
		info := attachmentJSON(t, attachmentRequest(t, p, postedFileHTTP(who.actor, who.human, post, file, true), 200))["file"].(map[string]any)
		if info["id"] != file || info["name"] != "plan.html" {
			t.Fatal("posted metadata unavailable", info)
		}
	}
	after, _ := json.Marshal(p.state)
	afterMembers, _ := json.Marshal(f.members)
	if !bytes.Equal(before, after) || !bytes.Equal(members, afterMembers) {
		t.Fatal("download consumed state or enrolled an observer")
	}
	attachmentRequest(t, p, postedFileHTTP("codex:x", "", post, file, false), 403)
	f.posts[post].DeleteAt = 1
	attachmentRequest(t, p, postedFileHTTP("codex:a", "", post, file, false), 404)
	f.posts[post].DeleteAt = 0
	f.posts[post].FileIds = nil
	attachmentRequest(t, p, postedFileHTTP("codex:a", "", post, file, false), 404)
	f.posts[post].FileIds = []string{file}
	f.files[file].PostId = model.NewId()
	attachmentRequest(t, p, postedFileHTTP("codex:a", "", post, file, false), 404)
	f.files[file].PostId = post
	f.files[file].ChannelId = model.NewId()
	attachmentRequest(t, p, postedFileHTTP("codex:a", "", post, file, false), 404)
	f.files[file].ChannelId = channel
	f.files[file].DeleteAt = 1
	attachmentRequest(t, p, postedFileHTTP("codex:a", "", post, file, false), 404)
	f.files[file].DeleteAt = 0
	attachmentRequest(t, p, postedFileHTTP("codex:a", "", post, model.NewId(), false), 404)
	public := channelConversation(t, p, "public-attachment", "claude:b")
	publicFile := uploadedFile(t, p, "codex:a", "", "public-upload", public)
	publicPost := attachmentPost(t, p, "codex:a", "", "public-post", public, "", []string{publicFile}, 200)["post"].(map[string]any)["id"].(string)
	attachmentRequest(t, p, postedFileHTTP("codex:x", "", publicPost, publicFile, false), 200)
}

func TestAttachmentsDownloadRechecksDeletionAfterNativeRead(t *testing.T) {
	p, f, _, channel := attachmentFixture(t)
	file := uploadedFile(t, p, "codex:a", "", "upload", channel)
	post := attachmentPost(t, p, "codex:a", "", "post", channel, "", []string{file}, 200)["post"].(map[string]any)["id"].(string)
	f.afterGet = func(string) { f.posts[post].FileIds = nil }
	w := attachmentRequest(t, p, postedFileHTTP("codex:a", "", post, file, false), 404)
	if bytes.Contains(w.Body.Bytes(), f.data[file]) {
		t.Fatal("removed attachment bytes leaked after slow read")
	}
}

func TestAttachmentsDownloadRechecksPersistenceFaultAfterNativeRead(t *testing.T) {
	p, f, _, channel := attachmentFixture(t)
	file := uploadedFile(t, p, "codex:a", "", "upload", channel)
	post := attachmentPost(t, p, "codex:a", "", "post", channel, "", []string{file}, 200)["post"].(map[string]any)["id"].(string)
	f.afterGet = func(string) {
		p.mu.Lock()
		p.fault = "injected unconfirmed metadata commit"
		p.mu.Unlock()
	}
	w := attachmentRequest(t, p, postedFileHTTP("codex:a", "", post, file, false), 503)
	if bytes.Contains(w.Body.Bytes(), f.data[file]) {
		t.Fatal("attachment bytes escaped after the metadata state became untrustworthy")
	}
}

func TestAttachmentsFinalMetadataLookupCannotBypassPersistenceFault(t *testing.T) {
	for _, metadata := range []bool{true, false} {
		t.Run(fmt.Sprint("metadata=", metadata), func(t *testing.T) {
			p, f, _, channel := attachmentFixture(t)
			file := uploadedFile(t, p, "codex:a", "", "upload", channel)
			post := attachmentPost(t, p, "codex:a", "", "post", channel, "", []string{file}, 200)["post"].(map[string]any)["id"].(string)
			calls, finalLookup := 0, 2
			if metadata {
				finalLookup = 1
			}
			f.afterInfo = func(string) {
				calls++
				if calls == finalLookup {
					p.mu.Lock()
					p.fault = "metadata failed during the final native file lookup"
					p.mu.Unlock()
				}
			}
			w := attachmentRequest(t, p, postedFileHTTP("codex:a", "", post, file, metadata), 503)
			if calls != finalLookup || bytes.Contains(w.Body.Bytes(), f.data[file]) || bytes.Contains(w.Body.Bytes(), []byte("plan.html")) {
				t.Fatal("final native metadata lookup leaked file content or skipped the intended interleaving", calls, w.Body.String())
			}
		})
	}
}

func TestAttachmentsNativeIOLeavesPluginMutexAvailable(t *testing.T) {
	for _, phase := range []string{"session", "upload", "download"} {
		t.Run(phase, func(t *testing.T) {
			p, f, _, channel := attachmentFixture(t)
			var request *http.Request
			entered, release, done := make(chan struct{}), make(chan struct{}), make(chan *httptest.ResponseRecorder, 1)
			var once sync.Once
			var releaseOnce sync.Once
			unblock := func() { releaseOnce.Do(func() { close(release) }) }
			block := func() { once.Do(func() { close(entered); <-release }) }
			if phase == "session" || phase == "upload" {
				if phase == "session" {
					f.beforeSession = func(*model.UploadSession) { block() }
				} else {
					f.beforeUpload = func(*model.UploadSession) { block() }
				}
				request = uploadHTTP("codex:a", "", "upload", channel, "plan.txt", []byte("bounded upload"))
			} else {
				file := uploadedFile(t, p, "codex:a", "", "upload", channel)
				post := attachmentPost(t, p, "codex:a", "", "post", channel, "", []string{file}, 200)["post"].(map[string]any)["id"].(string)
				f.beforeGet = func(string) { block() }
				request = postedFileHTTP("codex:a", "", post, file, false)
			}
			go func() { w := httptest.NewRecorder(); p.ServeHTTP(nil, w, request); done <- w }()
			defer unblock()
			select {
			case <-entered:
			case <-time.After(time.Second):
				t.Fatal("native I/O did not begin")
			}
			if !p.mu.TryLock() {
				t.Fatal("native attachment I/O held the global plugin mutex")
			}
			p.mu.Unlock()
			if phase == "upload" {
				attachmentRequest(t, p, uploadHTTP("codex:a", "", "upload", channel, "plan.txt", []byte("bounded upload")), 409)
				if f.uploadCalls != 1 {
					t.Fatal("concurrent request repeated native upload")
				}
			}
			// Release before waiting for the handler, including if the assertion fails.
			unblock()
			select {
			case w := <-done:
				if w.Code != 200 {
					t.Fatal(w.Code, w.Body.String())
				}
			case <-time.After(time.Second):
				t.Fatal("handler failed to finish after release")
			}
		})
	}
}
