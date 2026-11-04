"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {mergePosts, mergePage, filterConversations, DraftStore, withDraftLock, createClient} = require("./model.js");

function storage() {
    const values = new Map();
    return {getItem: k => values.get(k) || null, setItem: (k, v) => values.set(k, v)};
}
test("agent filtering means participation, with an explicit observer expansion", () => {
    const chats = [
        {id: "old", name: "Ancestor room", kind: "channel", members: ["parent"], observers: ["child"], last_activity: 3},
        {id: "own", name: "Working channel", kind: "channel", members: ["child"], observers: [], last_activity: 1},
        {id: "dm", name: "Human / Child", kind: "dm", members: ["child"], last_activity: 2},
    ];
    assert.deepEqual(filterConversations(chats, {agent: "child"}).map(c => c.id), ["dm", "own"]);
    assert.deepEqual(filterConversations(chats, {agent: "child", inherited: true}).map(c => c.id), ["old", "dm", "own"]);
    assert.deepEqual(filterConversations(chats, {kind: "dm", search: "human"}).map(c => c.id), ["dm"]);
    assert.deepEqual(filterConversations(chats, {kind: "channel"}).map(c => c.id), ["old", "own"]);
});
test("refresh keeps loaded older pages, applies edits and removes deleted window posts", () => {
    const old = [{id: "older", create_at: 1, message: "Older page"},
        {id: "a", create_at: 2, message: "Before edit"}, {id: "b", create_at: 2, message: "Deleted"}];
    const latest = [{id: "a", create_at: 2, message: "After edit"}, {id: "c", create_at: 2, message: "New"}];
    assert.deepEqual(mergePosts(old, latest), [old[0], ...latest]);
    assert.deepEqual(mergePosts(old, []), []);
});
test("a complete canonical page replaces deleted oldest history and exhausts pagination", () => {
    const a = {id: "a", create_at: 1}, b = {id: "b", create_at: 2};
    assert.deepEqual(mergePage([a, b], {posts: [b], next_before: null}, "a"),
        {posts: [b], next_before: null});
    assert.deepEqual(mergePage([a], {posts: [], next_before: null}, "a"),
        {posts: [], next_before: null});
});
test("a partial refresh updates its cursor unless older pages remain loaded", () => {
    const p = n => ({id: `p${n}`, create_at: n});
    const previous = Array.from({length: 50}, (_, i) => p(i + 11));
    const latest = [p(10), ...previous.slice(1)]; // p11 was the deleted before cursor.
    assert.equal(mergePage(previous, {posts: latest, next_before: "p10"}, "p11").next_before, "p10");
    const older = [p(1), p(2), ...previous];
    const result = mergePage(older, {posts: latest, next_before: "p10"}, "p1");
    assert.deepEqual(result.posts, [p(1), p(2), ...latest]);
    assert.equal(result.next_before, "p1");
    assert.equal(mergePage(older, {posts: latest, next_before: "p10"}, null).next_before, null);
});
test("uncertain send retains its exact ID, body and destination across reload", () => {
    const disk = storage();
    const first = new DraftStore(disk, "david");
    first.edit("chat", "root", "Review API\nKeep this exact text.");
    const pending = first.prepare("chat", "root", "operation-one").pending;
    const reloaded = new DraftStore(disk, "david");
    assert.deepEqual(reloaded.prepare("chat", "root", "do-not-replace").pending, pending);
    assert.throws(() => reloaded.edit("chat", "root", "Different request"), /Check the existing send/);
    reloaded.complete("chat", "root", "stale-operation");
    assert.deepEqual(reloaded.read("chat", "root").pending, pending);
    reloaded.complete("chat", "root", "operation-one");
    assert.deepEqual(reloaded.read("chat", "root"), {text: "", pending: null});
});
test("drafts stay attached to user, conversation and root", () => {
    const disk = storage();
    const david = new DraftStore(disk, "david");
    david.edit("a", "", "Root request");
    david.edit("a", "thread", "Thread reply");
    david.edit("b", "", "Another conversation");
    assert.equal(david.read("a", "").text, "Root request");
    assert.equal(david.read("a", "thread").text, "Thread reply");
    assert.equal(david.read("b", "").text, "Another conversation");
    assert.deepEqual(new DraftStore(disk, "another-human").read("a", ""), {text: "", pending: null});
});
test("storage failure cannot pretend the outbound operation was persisted", () => {
    const disk = storage();
    const drafts = new DraftStore(disk, "david");
    drafts.edit("a", "", "Important request");
    disk.setItem = () => { throw new Error("Quota exceeded"); };
    assert.throws(() => drafts.prepare("a", "", "op"), /Quota exceeded/);
    assert.equal(drafts.read("a", "").pending, null);
});
test("a stale window cannot post another window's text or pending operation", () => {
    const disk = storage();
    const a = new DraftStore(disk, "david"), b = new DraftStore(disk, "david");
    a.edit("chat", "", "Visible in A");
    const visible = a.read("chat", "");
    b.edit("chat", "", "Different text in B");
    assert.throws(() => a.prepare("chat", "", "wrong-op", visible), e => e.code === "draft_changed");
    assert.equal(a.read("chat", "").pending, null);
    const visibleB = b.read("chat", "");
    b.prepare("chat", "", "b-op", visibleB);
    assert.throws(() => a.prepare("chat", "", "wrong-op", visibleB), e => e.code === "draft_changed");
    assert.equal(a.read("chat", "").pending.op_id, "b-op");
});
test("origin-wide locking serializes competing claims against the visible snapshot", async () => {
    const disk = storage();
    const a = new DraftStore(disk, "david"), b = new DraftStore(disk, "david");
    a.edit("chat", "", "One intended message");
    const visible = a.read("chat", "");
    let tail = Promise.resolve();
    const keys = [];
    const host = {navigator: {locks: {request(key, action) {
        keys.push(key);
        const result = tail.then(action);
        tail = result.catch(() => {});
        return result;
    }}}};
    const results = await Promise.allSettled([
        withDraftLock(host, a, "chat", "", () => a.prepare("chat", "", "first", visible)),
        withDraftLock(host, b, "chat", "", () => b.prepare("chat", "", "second", visible)),
    ]);
    assert.equal(keys[0], keys[1]);
    assert.equal(results[0].status, "fulfilled");
    assert.equal(results[1].status, "rejected");
    assert.equal(results[1].reason.code, "draft_changed");
    assert.equal(a.read("chat", "").pending.op_id, "first");
    assert.throws(() => withDraftLock({}, a, "chat", "", () => {}), /Web Locks/);
});
test("browser transport binds the displayed account and uses existing session/CSRF", async () => {
    const calls = [];
    const client = createClient({AbortController, setTimeout, clearTimeout,
        document: {cookie: "other=value; MMCSRF=fixture-csrf"},
        fetch: async (...args) => { calls.push(args); return {ok: true, json: async () => ({status: "complete"})}; }});
    client.bindUser("human-one");
    await client.request("/posts", {method: "POST", body: '{"message":"A request"}'});
    assert.equal(calls[0][0], "/plugins/murmur/operator/v1/posts");
    assert.equal(calls[0][1].credentials, "same-origin");
    assert.equal(calls[0][1].headers["X-CSRF-Token"], "fixture-csrf");
    assert.equal(calls[0][1].headers["X-Murmur-User-Id"], "human-one");
    assert.equal(calls[0][1].headers.Authorization, undefined);
});
test("HTTP failure is explicit and cannot be reported as a posted message", async () => {
    const client = createClient({AbortController, setTimeout, clearTimeout, document: {cookie: ""},
        fetch: async () => ({ok: false, status: 401, json: async () => ({error: "Sign in again"})})});
    await assert.rejects(client.request("/posts", {method: "POST"}), e => e.status === 401 && /Sign in again/.test(e.message));
});

test("rejected-send recovery preserves exact text and refuses stale windows or uncertain results", () => {
    const disk = storage(), drafts = new DraftStore(disk, "human");
    drafts.edit("chat", "root", "Exact request");
    const pending = drafts.prepare("chat", "root", "original");
    for (const result of [{status: "failed"}, {status: "failed", dispatch: "rejected", op_id: "other", error: "Denied"}]) {
        assert.throws(() => drafts.reject("chat", "root", "original", result), /not confirmed/);
    }
    const rejected = drafts.reject("chat", "root", "original",
        {status: "failed", dispatch: "rejected", op_id: "original", error: "Root deleted"});
    assert.throws(() => drafts.releaseRejected("chat", "root", pending), e => e.code === "draft_changed");
    assert.throws(() => drafts.prepare("chat", "root", "new", rejected), /Restore or discard/);
    assert.deepEqual(drafts.releaseRejected("chat", "root", rejected), {text: "Exact request", pending: null});
    drafts.edit("chat", "root", "A different request");
    drafts.prepare("chat", "root", "replacement");
    assert.throws(() => drafts.releaseRejected("chat", "root", rejected, true), e => e.code === "draft_changed");
    assert.equal(drafts.read("chat", "root").pending.op_id, "replacement");
});

test("browser transport retains the structured rejection while generic HTTP errors stay generic", async () => {
    const rejection = {status: "failed", dispatch: "rejected", op_id: "exact", error: "Root unavailable"};
    const client = createClient({AbortController, setTimeout, clearTimeout, document: {cookie: ""},
        fetch: async () => ({ok: false, status: 404, json: async () => rejection})});
    await assert.rejects(client.request("/posts", {method: "POST"}), e => e.status === 404 && e.result === rejection);
});

test("a rejection storage failure leaves the original send unresolved", () => {
    const disk = storage(), drafts = new DraftStore(disk, "human");
    drafts.edit("chat", "", "Keep this request");
    drafts.prepare("chat", "", "original");
    disk.setItem = () => { throw new Error("Storage unavailable"); };
    assert.throws(() => drafts.reject("chat", "", "original",
        {status: "failed", dispatch: "rejected", op_id: "original", error: "Denied before posting"}), /Storage unavailable/);
    assert.equal(drafts.read("chat", "").pending.op_id, "original");
    assert.equal(drafts.read("chat", "").rejected, undefined);
    assert.throws(() => drafts.releaseRejected("chat", "", drafts.read("chat", "")), /Only a confirmed rejected/);
});

test("attachments retain their own durable upload and frozen post identities across reload", () => {
    const disk=storage(),drafts=new DraftStore(disk,"human");
    const intent={op_id:"upload-1",channel_id:"chat",filename:"notes.pdf",size:3,sha256:"a".repeat(64)};
    const file={id:"file-1",name:"notes.pdf",size:3,mime_type:"application/pdf"};
    assert.throws(()=>drafts.prepareUpload("chat","root",{...intent,size:0},drafts.read("chat","root")),/Empty files/);
    const preparing=drafts.prepareUpload("chat","root",intent,drafts.read("chat","root"));
    assert.deepEqual(new DraftStore(disk,"human").read("chat","root").upload,intent);
    assert.throws(()=>drafts.prepare("chat","root","post-1",preparing,true),/saved upload/);
    assert.throws(()=>drafts.releaseUpload("chat","root",preparing),/Check the saved upload/);
    const staged=drafts.finishUpload("chat","root","upload-1",file);
    assert.throws(()=>drafts.prepare("chat","root","post-1",staged),/Open the Murmur workspace/,
        "the text-only console must not silently send files it does not display");
    const pending=drafts.prepare("chat","root","post-1",staged,true);
    assert.deepEqual(pending.pending,{op_id:"post-1",channel_id:"chat",root_id:"root",message:"",file_ids:["file-1"]});
    assert.throws(()=>drafts.removeFile("chat","root","file-1",pending),/existing send/);
    assert.throws(()=>drafts.prepare("chat","root","post-1",pending),/Open the Murmur workspace/);
    assert.deepEqual(drafts.complete("chat","root","post-1"),{text:"",pending:null});
});
test("stale attachment selections and corrupted frozen file IDs cannot become another send", () => {
    const disk=storage(),drafts=new DraftStore(disk,"human");
    const original=drafts.edit("chat","","Text");
    drafts.prepareUpload("chat","",{op_id:"upload-1",channel_id:"chat",filename:"image.png",size:1,sha256:"b".repeat(64)},original);
    const staged=drafts.finishUpload("chat","","upload-1",{id:"file-1",name:"image.png",size:1,mime_type:"image/png"});
    assert.throws(()=>drafts.prepare("chat","","post-1",original,true),/changed in another window/);
    const pending=drafts.prepare("chat","","post-1",staged,true);
    disk.setItem(drafts.key("chat"),JSON.stringify({...pending,pending:{...pending.pending,file_ids:["file-substitution"]}}));
    assert.throws(()=>drafts.read("chat"),/saved draft is invalid/);
});
