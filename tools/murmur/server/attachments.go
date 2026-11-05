package main

import (
	"bytes"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"fmt"
	"io"
	"mime"
	"net/http"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/mattermost/mattermost/server/public/model"
)

// Mattermost owns the bytes. The journal retains only immutable upload facts
// and reserved native IDs, so lost responses can be checked without reuploading.
type UploadIntent struct {
	SessionID string         `json:"session_id"`
	FileID    string         `json:"file_id"`
	UserID    string         `json:"user_id"`
	Filename  string         `json:"filename"`
	Size      int64          `json:"size"`
	SHA256    string         `json:"sha256"`
	File      map[string]any `json:"file,omitempty"`
}

type uploadRequest struct {
	Actor     string `json:"actor"`
	OpID      string `json:"op_id"`
	ChannelID string `json:"channel_id"`
	Filename  string `json:"filename"`
	Size      int64  `json:"size"`
	SHA256    string `json:"sha256"`
}

type fileCaller struct {
	actor, userID string
	operator      bool
	readVersion   int
}

func uploadKind(operator bool) string {
	if operator {
		return "operator_upload"
	}
	return "upload"
}

func isUpload(o *Operation) bool {
	return o != nil && (o.Kind == "upload" || o.Kind == "operator_upload")
}

func (p *Plugin) attachmentLimit() int64 {
	config := p.API.GetConfig()
	if config == nil || config.FileSettings.MaxFileSize == nil || *config.FileSettings.MaxFileSize <= 0 {
		return 0
	}
	return *config.FileSettings.MaxFileSize
}

func (p *Plugin) attachmentEnabled() bool {
	config := p.API.GetConfig()
	return config != nil && config.FileSettings.EnableFileAttachments != nil && *config.FileSettings.EnableFileAttachments &&
		config.FileSettings.DriverName != nil && strings.TrimSpace(*config.FileSettings.DriverName) != ""
}

func (p *Plugin) attachmentCapability() map[string]any {
	limit := p.attachmentLimit()
	return map[string]any{"enabled": p.attachmentEnabled() && limit > 0, "max_file_size": limit, "max_files_per_post": 10}
}

// Called under p.mu. Both the coordinator and the authenticated operator use
// the same native file store, but neither can select another sender identity.
func (p *Plugin) fileCaller(r *http.Request, operator bool) (fileCaller, error) {
	if operator {
		id := r.Header.Get("Mattermost-User-Id")
		if id == "" {
			return fileCaller{}, bad(401, "Mattermost login required")
		}
		user, err := p.operatorUser(id)
		if err != nil {
			return fileCaller{}, err
		}
		if expected := r.Header.Get("X-Murmur-User-Id"); expected != "" && expected != id {
			return fileCaller{}, bad(409, "operator session changed; reload before continuing")
		}
		if p.fault != "" {
			return fileCaller{}, bad(503, "persistent state unavailable: "+p.fault)
		}
		return fileCaller{actor: user.Id, userID: user.Id, operator: true}, nil
	}
	authorization := r.Header.Get("Authorization")
	if !strings.HasPrefix(authorization, "Bearer ") || p.config.CoordinatorToken == "" ||
		subtle.ConstantTimeCompare([]byte(strings.TrimPrefix(authorization, "Bearer ")), []byte(p.config.CoordinatorToken)) != 1 {
		return fileCaller{}, bad(401, "coordinator authentication required")
	}
	if p.fault != "" {
		return fileCaller{}, bad(503, "persistent state unavailable: "+p.fault)
	}
	actor, err := p.requireActor(r.URL.Query().Get("actor"))
	if err != nil {
		return fileCaller{}, err
	}
	version, err := requestedReadVersion(r)
	if err != nil {
		return fileCaller{}, err
	}
	return fileCaller{actor: actor.ID, userID: actor.UserID, readVersion: version}, nil
}

func fileError(w http.ResponseWriter, err error) {
	status := 502
	if failure, ok := err.(*httpError); ok {
		status = failure.status
	} else if failure, ok := err.(*model.AppError); ok {
		status = failure.StatusCode
	}
	writeError(w, status, err.Error())
}

// File handlers acquire the state mutex only for authorization and journal
// transitions. Reading request/file bytes and native upload RPCs run outside it.
func (p *Plugin) serveAttachment(w http.ResponseWriter, r *http.Request, path string) bool {
	parts := strings.Split(strings.Trim(path, "/"), "/")
	operator := len(parts) > 0 && parts[0] == "operator"
	if operator {
		parts = parts[1:]
	}
	if len(parts) < 2 || parts[0] != "v1" {
		return false
	}
	upload := len(parts) == 2 && parts[1] == "files" && r.Method == http.MethodPost
	download := (len(parts) == 5 || len(parts) == 6 && parts[5] == "info") && parts[1] == "posts" && parts[3] == "files" && r.Method == http.MethodGet
	operation := len(parts) == 3 && parts[1] == "operations" && r.Method == http.MethodGet
	if !upload && !download && !operation {
		return false
	}
	p.mu.Lock()
	if operation && !isUpload(p.state.Operations[parts[2]]) {
		p.mu.Unlock()
		return false
	}
	caller, err := p.fileCaller(r, operator)
	p.mu.Unlock()
	w.Header().Set("Cache-Control", "private, no-store")
	if err != nil {
		fileError(w, err)
		return true
	}
	if upload {
		p.serveUpload(w, r, caller)
	} else if operation {
		p.serveUploadOperation(w, caller, parts[2])
	} else {
		p.servePostedFile(w, r, caller, parts[2], parts[4], len(parts) == 6)
	}
	return true
}

func (p *Plugin) uploadAccess(caller fileCaller, channel string, size int64) error {
	if !p.attachmentEnabled() {
		return bad(403, "file uploads are disabled in Mattermost's configuration")
	}
	if limit := p.attachmentLimit(); limit <= 0 {
		return bad(503, "native file upload size limit is unavailable")
	} else if size > limit {
		return bad(413, "file exceeds Mattermost's configured size limit")
	}
	if caller.operator {
		if _, err := p.operatorChannel(channel); err != nil {
			return err
		}
	} else if _, err := p.access(caller.actor, channel, true); err != nil {
		return err
	}
	native, appErr := p.API.GetChannel(channel)
	if appErr != nil {
		return appErr
	}
	if native == nil || native.Id != channel || native.DeleteAt != 0 {
		return bad(404, "upload conversation is unavailable")
	}
	return nil
}

func parseUpload(r *http.Request, caller fileCaller, limit int64) (uploadRequest, error) {
	media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || media != "application/octet-stream" {
		return uploadRequest{}, bad(415, "Content-Type must be application/octet-stream")
	}
	q := r.URL.Query()
	for _, key := range []string{"op_id", "channel_id", "filename", "size", "sha256"} {
		if len(q[key]) != 1 {
			return uploadRequest{}, bad(400, "one "+key+" is required")
		}
	}
	size, err := strconv.ParseInt(q.Get("size"), 10, 64)
	if err != nil || size <= 0 {
		return uploadRequest{}, bad(400, "upload size must be a positive byte count")
	}
	if limit <= 0 {
		return uploadRequest{}, bad(503, "native file upload size limit is unavailable")
	}
	if size > limit || r.ContentLength > limit {
		return uploadRequest{}, bad(413, "file exceeds Mattermost's configured size limit")
	}
	name := q.Get("filename")
	if !utf8.ValidString(name) || name == "" || name == "." || name == ".." || len([]rune(name)) > model.MaxFilenameLength ||
		filepath.Base(name) != name || strings.ContainsAny(name, "\\/") || model.SanitizeFilename(name) != name ||
		strings.IndexFunc(name, func(r rune) bool { return unicode.IsControl(r) || unicode.Is(unicode.Cf, r) }) >= 0 {
		return uploadRequest{}, bad(400, "filename must be a safe single filename")
	}
	checksum, err := hex.DecodeString(q.Get("sha256"))
	if err != nil || len(checksum) != sha256.Size || q.Get("sha256") != strings.ToLower(q.Get("sha256")) {
		return uploadRequest{}, bad(400, "sha256 must be a lowercase SHA256 digest")
	}
	if !model.IsValidId(q.Get("channel_id")) || q.Get("op_id") == "" || len(q.Get("op_id")) > 200 {
		return uploadRequest{}, bad(400, "valid channel_id and op_id are required")
	}
	return uploadRequest{Actor: caller.actor, OpID: q.Get("op_id"), ChannelID: q.Get("channel_id"),
		Filename: name, Size: size, SHA256: q.Get("sha256")}, nil
}

func (p *Plugin) serveUpload(w http.ResponseWriter, r *http.Request, caller fileCaller) {
	p.mu.Lock()
	req, err := parseUpload(r, caller, p.attachmentLimit())
	if err == nil {
		err = p.uploadAccess(caller, req.ChannelID, req.Size)
	}
	p.mu.Unlock()
	if err != nil {
		fileError(w, err)
		return
	}
	data, err := io.ReadAll(io.LimitReader(r.Body, req.Size+1))
	if err != nil || int64(len(data)) != req.Size {
		fileError(w, bad(400, "upload body does not match its declared size"))
		return
	}
	actual := sha256.Sum256(data)
	if hex.EncodeToString(actual[:]) != req.SHA256 {
		fileError(w, bad(400, "upload body does not match its SHA256"))
		return
	}
	p.mu.Lock()
	current, err := p.fileCaller(r, caller.operator)
	if err == nil && current != caller {
		err = bad(409, "upload sender changed")
	}
	if err == nil {
		err = p.uploadAccess(caller, req.ChannelID, req.Size)
	}
	o := p.state.Operations[req.OpID]
	fresh := o == nil
	if err == nil && o != nil && (o.Actor != caller.actor || o.Kind != uploadKind(caller.operator) || o.Hash != digest(req)) {
		err = bad(409, "operation ID is already bound to another immutable request")
	}
	if err == nil && o != nil && o.Status == "complete" {
		if o.Upload == nil || o.Upload.File == nil {
			err = bad(503, "completed upload has no validated native metadata")
		} else {
			result := map[string]any{"file": o.Upload.File, "channel_id": o.ChannelID}
			p.mu.Unlock()
			writeJSON(w, result)
			return
		}
	}
	if err == nil && p.fileActive[req.OpID] {
		err = bad(409, "upload is already in progress; inspect its operation")
	}
	if err == nil && fresh {
		o = &Operation{ID: req.OpID, Actor: caller.actor, Kind: uploadKind(caller.operator), Hash: digest(req),
			Status: "pending", Stage: "session", ChannelID: req.ChannelID,
			Upload: &UploadIntent{SessionID: model.NewId(), FileID: model.NewId(), UserID: caller.userID,
				Filename: req.Filename, Size: req.Size, SHA256: req.SHA256}}
		p.state.Operations[o.ID] = o
		err = p.save()
	}
	if err == nil {
		if p.fileActive == nil {
			p.fileActive = map[string]bool{}
		}
		p.fileActive[o.ID] = true
	}
	p.mu.Unlock()
	if err != nil {
		fileError(w, err)
		return
	}
	defer func() { p.mu.Lock(); delete(p.fileActive, o.ID); p.mu.Unlock() }()
	var responseErr error
	if fresh {
		intent := o.Upload
		session, createErr := p.API.CreateUploadSession(&model.UploadSession{Id: intent.SessionID,
			Type: model.UploadTypeAttachment, UserId: intent.UserID, ChannelId: o.ChannelID,
			Filename: intent.Filename, FileSize: intent.Size, ReqFileId: intent.FileID})
		if createErr == nil && (session == nil || session.Id != intent.SessionID || session.ReqFileId != intent.FileID ||
			session.UserId != intent.UserID || session.ChannelId != o.ChannelID || session.Filename != intent.Filename ||
			session.Type != model.UploadTypeAttachment || session.FileSize != intent.Size || session.FileOffset != 0) {
			createErr = bad(503, "native upload session differs from its reserved intent")
		}
		if createErr == nil {
			p.mu.Lock()
			_, createErr = p.fileCaller(r, caller.operator)
			if createErr == nil {
				createErr = p.uploadAccess(caller, o.ChannelID, intent.Size)
			}
			if createErr == nil {
				o.Stage = "uploading"
				createErr = p.save()
			}
			p.mu.Unlock()
			if createErr == nil {
				_, createErr = p.API.UploadData(session, bytes.NewReader(data))
			}
		}
		responseErr = createErr
		// Even a failed response may have committed a file. Only the reserved
		// native ID and immutable bytes can establish the upload result.
	}
	info, err := p.recoverUpload(o, responseErr)
	if err != nil {
		fileError(w, err)
		return
	}
	writeJSON(w, map[string]any{"file": fileView(info), "channel_id": o.ChannelID})
}

func (p *Plugin) checkUploadedFile(o *Operation) (*model.FileInfo, error) {
	if o.Upload == nil || !model.IsValidId(o.Upload.FileID) {
		return nil, bad(409, "upload lacks a reserved native file identity")
	}
	u := o.Upload
	info, appErr := p.API.GetFileInfo(u.FileID)
	if appErr != nil {
		return nil, appErr
	}
	if info == nil || info.Id != u.FileID || info.DeleteAt != 0 || info.CreatorId != u.UserID ||
		info.ChannelId != o.ChannelID || info.Name != u.Filename || info.Size != u.Size {
		return nil, bad(409, "native file identity, owner, channel or metadata differs from upload intent")
	}
	data, appErr := p.API.GetFile(info.Id)
	if appErr != nil {
		return nil, appErr
	}
	actual := sha256.Sum256(data)
	if int64(len(data)) != u.Size || hex.EncodeToString(actual[:]) != u.SHA256 {
		return nil, bad(409, "native file bytes differ from upload intent")
	}
	return info, nil
}

func (p *Plugin) recoverUpload(o *Operation, responseErr error) (*model.FileInfo, error) {
	info, err := p.checkUploadedFile(o)
	p.mu.Lock()
	defer p.mu.Unlock()
	if o.Status == "complete" {
		return info, err // A later deletion/read failure cannot undo a committed operation.
	}
	if err != nil {
		o.Status, o.Error = "uncertain", "native upload is not verified: "+err.Error()+"; bytes will not be resent"
		if responseErr != nil {
			o.Error += "; native response: " + responseErr.Error()
		}
	} else {
		o.Status, o.Stage, o.Error = "complete", "uploaded", ""
		o.Upload.File = fileView(info)
	}
	if saveErr := p.save(); saveErr != nil {
		return nil, saveErr
	}
	if err != nil {
		return nil, bad(409, o.Error)
	}
	return info, nil
}

func (p *Plugin) serveUploadOperation(w http.ResponseWriter, caller fileCaller, id string) {
	p.mu.Lock()
	o := p.state.Operations[id]
	if !isUpload(o) || o.Actor != caller.actor || o.Kind != uploadKind(caller.operator) {
		p.mu.Unlock()
		fileError(w, bad(403, "upload operation belongs to another sender"))
		return
	}
	busy := p.fileActive[id]
	complete := o.Status == "complete"
	p.mu.Unlock()
	if !busy && !complete {
		_, _ = p.recoverUpload(o, nil)
	}
	p.mu.Lock()
	if p.fault != "" {
		err := bad(503, "persistent state unavailable: "+p.fault)
		p.mu.Unlock()
		fileError(w, err)
		return
	}
	result, err := p.operationResult(o)
	p.mu.Unlock()
	if err != nil {
		fileError(w, err)
		return
	}
	writeJSON(w, result)
}

func fileView(info *model.FileInfo) map[string]any {
	return map[string]any{"id": info.Id, "name": info.Name, "size": info.Size, "mime_type": info.MimeType,
		"extension": info.Extension, "width": info.Width, "height": info.Height}
}

// The actual live post is authoritative: a caller-provided channel or an old
// FileInfo.PostId cannot authorize a removed, deleted or substituted attachment.
func (p *Plugin) postedFile(caller fileCaller, postID, fileID string) (*model.FileInfo, error) {
	post, appErr := p.API.GetPost(postID)
	if appErr != nil {
		return nil, appErr
	}
	if post == nil || post.Id != postID || !userPost(post) || !slices.Contains(post.FileIds, fileID) {
		return nil, bad(404, "attachment is unavailable on this post")
	}
	info, appErr := p.API.GetFileInfo(fileID)
	if appErr != nil {
		return nil, appErr
	}
	if info == nil || info.Id != fileID || info.DeleteAt != 0 || info.PostId != postID || info.ChannelId != post.ChannelId {
		return nil, bad(404, "attachment binding is unavailable")
	}
	// Native reads may be slow. Authorize only after both have finished so a
	// failed grant transition during either read cannot expose uncommitted state.
	p.mu.Lock()
	var err error
	if p.fault != "" {
		err = bad(503, "persistent state unavailable: "+p.fault)
	} else if caller.operator {
		_, err = p.operatorUser(caller.userID)
		if err == nil {
			_, err = p.operatorChannel(post.ChannelId)
		}
	} else {
		_, err = p.contentRead(caller.actor, post.ChannelId, caller.readVersion)
	}
	p.mu.Unlock()
	if err != nil {
		return nil, err
	}
	return info, nil
}

func (p *Plugin) servePostedFile(w http.ResponseWriter, r *http.Request, caller fileCaller, postID, fileID string, metadata bool) {
	info, err := p.postedFile(caller, postID, fileID)
	if err != nil {
		fileError(w, err)
		return
	}
	if metadata {
		writeJSON(w, map[string]any{"file": fileView(info)})
		return
	}
	data, appErr := p.API.GetFile(fileID)
	if appErr != nil {
		fileError(w, appErr)
		return
	}
	// Recheck the live authorization/binding after the potentially slow read.
	latest, err := p.postedFile(caller, postID, fileID)
	if err != nil {
		fileError(w, err)
		return
	}
	if latest.Size != info.Size || int64(len(data)) != latest.Size {
		fileError(w, bad(503, "attachment changed while downloading"))
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{"filename": info.Name}))
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Content-Security-Policy", "sandbox")
	w.Header().Set("Content-Length", strconv.Itoa(len(data)))
	checksum := sha256.Sum256(data)
	w.Header().Set("X-Content-SHA256", hex.EncodeToString(checksum[:]))
	_, _ = w.Write(data)
}

func canonicalFileIDs(ids []string) ([]string, error) {
	if len(ids) > 10 {
		return nil, bad(400, "a post supports at most 10 attachments")
	}
	seen := map[string]bool{}
	for _, id := range ids {
		if !model.IsValidId(id) || seen[id] {
			return nil, bad(400, "file_ids must contain unique native file IDs")
		}
		seen[id] = true
	}
	// Mattermost 12.0.1 Post.PreCommit sorts IDs while deduplicating. Reject
	// duplicates ourselves, then freeze that same order without mutating callers.
	result := append([]string(nil), ids...)
	slices.Sort(result)
	return result, nil
}

// New sends accept only this sender's completed upload for this conversation.
// Existing bound files must be downloaded and uploaded as a new explicit send.
func (p *Plugin) validatePostFiles(owner, userID, channel string, operator bool, ids []string) error {
	if len(ids) > 0 && !p.attachmentEnabled() {
		return bad(403, "file attachments are disabled in Mattermost's configuration")
	}
	for _, id := range ids {
		var upload *Operation
		for _, o := range p.state.Operations {
			if isUpload(o) && o.Upload != nil && o.Upload.FileID == id && o.Actor == owner && o.Kind == uploadKind(operator) {
				upload = o
				break
			}
		}
		if upload == nil || upload.Status != "complete" || upload.ChannelID != channel || upload.Upload.UserID != userID {
			return bad(403, "file is not this sender's completed upload in this conversation")
		}
		info, appErr := p.API.GetFileInfo(id)
		if appErr != nil {
			return appErr
		}
		if info == nil || info.Id != id || info.DeleteAt != 0 || info.PostId != "" || info.CreatorId != userID ||
			info.ChannelId != channel || info.Name != upload.Upload.Filename || info.Size != upload.Upload.Size {
			return bad(409, "uploaded file is no longer available for this post")
		}
	}
	return nil
}

func (p *Plugin) postFiles(post *model.Post) []map[string]any {
	files := []map[string]any{}
	for _, id := range post.FileIds {
		info, appErr := p.API.GetFileInfo(id)
		if appErr != nil || info == nil || info.Id != id || info.DeleteAt != 0 || info.PostId != post.Id || info.ChannelId != post.ChannelId {
			files = append(files, map[string]any{"id": id, "available": false})
			continue
		}
		files = append(files, fileView(info))
	}
	return files
}

func (p *Plugin) postFileBindingsMatch(post *model.Post) bool {
	for _, id := range post.FileIds {
		info, appErr := p.API.GetFileInfo(id)
		if appErr != nil || info == nil || info.Id != id || info.DeleteAt != 0 || info.PostId != post.Id ||
			info.ChannelId != post.ChannelId || info.CreatorId != post.UserId {
			return false
		}
	}
	return true
}

func postPreview(post *model.Post) string {
	if strings.TrimSpace(post.Message) == "" && len(post.FileIds) > 0 {
		return fmt.Sprintf("[%d attachment(s)]", len(post.FileIds))
	}
	return post.Message
}
