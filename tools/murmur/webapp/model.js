/* Browser-independent state and transport rules for the operator console. */
"use strict";

function comparePosts(a, b) {
    return a.create_at - b.create_at || a.id.localeCompare(b.id);
}

function mergePosts(existing, latest, complete = false) {
    if (complete || !latest.length) return latest;
    // The latest response is a canonical window. Preserve older loaded pages,
    // but remove deleted posts inside that window and replace edited copies.
    const boundary = latest[0];
    const posts = new Map(existing.filter(p => comparePosts(p, boundary) < 0).map(p => [p.id, p]));
    for (const post of latest) posts.set(post.id, post);
    return [...posts.values()].sort(comparePosts);
}

function mergePage(existing, page, before) {
    const complete = page.next_before === null;
    const hasOlder = !complete && page.posts.length && existing.some(p => comparePosts(p, page.posts[0]) < 0);
    return {posts: mergePosts(existing, page.posts, complete),
        next_before: hasOlder ? before : page.next_before};
}

function filterConversations(conversations, {agent = "", inherited = false, kind = "", search = ""} = {}) {
    const needle = search.trim().toLocaleLowerCase();
    return conversations.filter(c => (!kind || c.kind === kind) &&
        (!agent || c.members.includes(agent) || (inherited && (c.observers || []).includes(agent))) &&
        (!needle || `${c.name} ${c.preview || ""}`.toLocaleLowerCase().includes(needle)))
        .sort((a, b) => b.last_activity - a.last_activity || a.id.localeCompare(b.id));
}

function draftFiles(entry) { return entry.files || []; }
function validFile(file) {
    return file && typeof file.id === "string" && file.id && typeof file.name === "string" && file.name &&
        Number.isSafeInteger(file.size) && file.size >= 0 && typeof file.mime_type === "string";
}
function sameFileIds(left = [], right = []) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false;
    return left.every((id, index) => id === right[index]);
}

class DraftStore {
    constructor(storage, userId) {
        this.storage = storage;
        this.prefix = `murmur.operator.${userId}.`;
    }
    key(channel, root = "") { return this.prefix + channel + ":" + root; }
    read(channel, root = "") {
        const value = this.storage.getItem(this.key(channel, root));
        if (!value) return {text: "", pending: null};
        const entry = JSON.parse(value);
        const files = draftFiles(entry), upload = entry.upload;
        if (!Array.isArray(files) || files.length > 10 || files.some(file => !validFile(file)) ||
            new Set(files.map(file => file.id)).size !== files.length ||
            (upload && (typeof upload.op_id !== "string" || !upload.op_id || upload.channel_id !== channel ||
             typeof upload.filename !== "string" || !upload.filename || !Number.isSafeInteger(upload.size) || upload.size < 0 ||
             !/^[a-f0-9]{64}$/.test(upload.sha256))) ||
            (entry.uploadRejected && (!upload || entry.uploadRejected.op_id !== upload.op_id || typeof entry.uploadRejected.error !== "string")) ||
            typeof entry.text !== "string" || (entry.pending &&
            (entry.pending.channel_id !== channel || entry.pending.root_id !== root ||
             typeof entry.pending.op_id !== "string" || !entry.pending.op_id ||
             entry.pending.message !== entry.text || upload || !sameFileIds(entry.pending.file_ids, files.map(file => file.id).sort()))) || (entry.rejected &&
             (!entry.pending || entry.rejected.op_id !== entry.pending.op_id || typeof entry.rejected.error !== "string"))) {
            throw new Error("The saved draft is invalid. Inspect browser storage before sending.");
        }
        return entry;
    }
    write(channel, root, entry) {
        // A failed storage write must prevent the subsequent network mutation.
        this.storage.setItem(this.key(channel, root), JSON.stringify(entry));
        return entry;
    }
    edit(channel, root, text) {
        const previous = this.read(channel, root);
        if (previous.pending) throw new Error("Check the existing send before composing another message here.");
        return this.write(channel, root, {...previous, text, pending: null});
    }
    prepare(channel, root, opId, visible, allowAttachments = false) {
        const entry = this.read(channel, root);
        assertVisibleDraft(entry, visible);
        if (entry.rejected) throw new Error("Restore or discard the rejected send before posting again.");
        const files = draftFiles(entry);
        if (files.length && !allowAttachments) throw new Error("This draft has attachments. Open the Murmur workspace to review and send them.");
        if (entry.pending) return entry;
        if (entry.upload) throw new Error("Check the saved upload before posting this draft.");
        if (!entry.text.trim() && !files.length) throw new Error("Write a message or attach a file first.");
        return this.write(channel, root, {...entry, pending: {
            op_id: opId, channel_id: channel, root_id: root, message: entry.text,
            ...(files.length ? {file_ids: files.map(file => file.id).sort()} : {}),
        }});
    }
    complete(channel, root, opId) {
        const entry = this.read(channel, root);
        if (!entry.pending || entry.pending.op_id !== opId) return entry;
        return this.write(channel, root, {text: "", pending: null});
    }
    reject(channel, root, opId, result) {
        if (!isRejectedSend(result, opId)) throw new Error("The server has not confirmed rejection without a post.");
        const entry = this.read(channel, root);
        if (entry.pending?.op_id !== opId) throw new Error("The saved send changed in another window. Review it before continuing.");
        return this.write(channel, root, {...entry, rejected: {op_id: opId, error: result.error || "Send rejected"}});
    }
    releaseRejected(channel, root, visible, discard = false) {
        const entry = this.read(channel, root);
        assertVisibleDraft(entry, visible);
        if (!entry.rejected) throw new Error("Only a confirmed rejected send can be restored or discarded.");
        return this.write(channel, root, {text: discard ? "" : entry.text, pending: null,
            ...(!discard && draftFiles(entry).length ? {files: entry.files} : {})});
    }
    prepareUpload(channel, root, request, visible) {
        const entry = this.read(channel, root);
        assertVisibleDraft(entry, visible);
        if (entry.pending || entry.upload) throw new Error("Check the existing send or upload before attaching another file.");
        if (!Number.isSafeInteger(request.size) || request.size <= 0) throw new Error("Empty files cannot be attached.");
        if (draftFiles(entry).length >= 10) throw new Error("A message can contain at most 10 attachments.");
        return this.write(channel, root, {...entry, upload: request});
    }
    finishUpload(channel, root, opId, file) {
        const entry = this.read(channel, root);
        if (!entry.upload || entry.upload.op_id !== opId) throw new Error("The saved upload changed. Review the draft before continuing.");
        if (!validFile(file) || file.name !== entry.upload.filename || file.size !== entry.upload.size ||
            draftFiles(entry).some(item => item.id === file.id)) throw new Error("Unexpected upload confirmation. Check the saved upload.");
        const {upload, uploadRejected, ...draft} = entry;
        return this.write(channel, root, {...draft, files: [...draftFiles(entry), file]});
    }
    rejectUpload(channel, root, opId, result) {
        const entry = this.read(channel, root);
        if (entry.upload?.op_id !== opId || !isRejectedSend(result, opId)) throw new Error("The server has not confirmed this upload was rejected.");
        return this.write(channel, root, {...entry, uploadRejected: {op_id: opId, error: result.error}});
    }
    releaseUpload(channel, root, visible) {
        const entry = this.read(channel, root);
        assertVisibleDraft(entry, visible);
        if (!entry.uploadRejected) throw new Error("Check the saved upload before removing it.");
        const {upload, uploadRejected, ...draft} = entry;
        return this.write(channel, root, draft);
    }
    abandonUpload(channel, root, visible) {
        const entry = this.read(channel, root);
        assertVisibleDraft(entry, visible);
        if (!entry.upload || entry.pending) throw new Error("Only a pending upload can be removed from the draft.");
        // This forgets the local selection; the native upload may still exist.
        const {upload, uploadRejected, ...draft} = entry;
        return this.write(channel, root, draft);
    }
    removeFile(channel, root, id, visible) {
        const entry = this.read(channel, root);
        assertVisibleDraft(entry, visible);
        if (entry.pending || entry.upload) throw new Error("Check the existing send or upload before changing attachments.");
        return this.write(channel, root, {...entry, files: draftFiles(entry).filter(file => file.id !== id)});
    }
}

function isRejectedSend(result, opId) {
    return result?.status === "failed" && result.dispatch === "rejected" && result.op_id === opId &&
        typeof result.error === "string";
}

function assertVisibleDraft(saved, visible) {
    if (visible && (saved.text !== visible.text ||
        ["op_id", "channel_id", "root_id", "message"].some(key => saved.pending?.[key] !== visible.pending?.[key]) ||
        JSON.stringify(saved.pending?.file_ids || []) !== JSON.stringify(visible.pending?.file_ids || []) ||
        JSON.stringify(draftFiles(saved)) !== JSON.stringify(draftFiles(visible)) ||
        JSON.stringify(saved.upload || null) !== JSON.stringify(visible.upload || null) ||
        saved.uploadRejected?.op_id !== visible.uploadRejected?.op_id ||
        saved.rejected?.op_id !== visible.rejected?.op_id)) {
        const error = new Error("The draft changed in another window. Review it before posting.");
        error.code = "draft_changed";
        throw error;
    }
}

function withDraftLock(host, store, channel, root, action) {
    if (!host.navigator?.locks) throw new Error("This browser cannot safely coordinate sends. Use a browser with Web Locks support.");
    // All draft mutations share this origin-wide lock, not only send claims.
    return host.navigator.locks.request(store.key(channel, root), action);
}

// Opening a DM is independent of any conversation's text or uncertain send.
class OpenStore {
    constructor(storage, userId) {
        this.storage = storage;
        this.userId = userId;
    }
    key(agent) { return `murmur.operator.open.${this.userId}.${agent}`; }
    read(agent) {
        const value = this.storage.getItem(this.key(agent));
        if (!value) return null;
        const entry = JSON.parse(value);
        if (!entry || entry.agent_id !== agent || typeof entry.op_id !== "string" || !entry.op_id ||
            !["pending", "complete", "failed"].includes(entry.status) ||
            (entry.status === "complete" && (typeof entry.conversation?.id !== "string" || !entry.conversation.id || entry.conversation.kind !== "dm"))) {
            throw new Error("The saved DM opening is invalid. Inspect browser storage before opening.");
        }
        return entry;
    }
    write(agent, entry) {
        this.storage.setItem(this.key(agent), JSON.stringify(entry));
        return entry;
    }
    prepare(agent, opId) {
        const entry = this.read(agent);
        if (entry && entry.status !== "failed") return {entry, created: false};
        return {entry: this.write(agent, {op_id: opId, agent_id: agent, status: "pending"}), created: true};
    }
    finish(agent, opId, result) {
        const entry = this.read(agent);
        if (!entry || entry.op_id !== opId) throw new Error("The saved DM opening changed in another window. Select the agent again.");
        return this.write(agent, {...entry, ...result});
    }
}

function createClient(host) {
    let userId = "";
    const base = "/plugins/murmur/operator/v1";
    async function request(path, options = {}) {
        const controller = new host.AbortController();
        const timer = host.setTimeout(() => controller.abort(), 30000);
        const csrf = host.document.cookie.split(";").map(s => s.trim()).find(s => s.startsWith("MMCSRF="));
        const headers = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"};
        if (csrf) headers["X-CSRF-Token"] = decodeURIComponent(csrf.slice(7));
        if (userId) headers["X-Murmur-User-Id"] = userId;
        try {
            const response = await host.fetch(base + path, {...options, headers,
                credentials: "same-origin", signal: controller.signal});
            let result;
            try { result = await response.json(); }
            catch { throw new Error("Unexpected server response. Check your Mattermost login."); }
            if (!response.ok) {
                const error = new Error(result.error || `Request failed (${response.status})`);
                error.status = response.status;
                error.result = result;
                throw error;
            }
            return result;
        } finally { host.clearTimeout(timer); }
    }
    return {request, bindUser(id) { userId = id; }};
}

module.exports = {comparePosts, mergePosts, mergePage, filterConversations, DraftStore, OpenStore,
    isRejectedSend, assertVisibleDraft, withDraftLock, createClient, draftFiles, sameFileIds};
