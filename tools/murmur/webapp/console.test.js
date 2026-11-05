"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const Model = require("./model.js");

// Execute the actual Console closures with deterministic React hooks and HTTP.
// No extracted copies of pagination/refresh logic or browser dependency.
function consoleHarness(request, options = {}) {
    const slots = [], effects = [], cleanups = [], values = options.values || new Map();
    let cursor = 0, first = true, tree;
    const React = {
        createElement: (type, props, ...children) => ({type, props: props || {}, children: children.flat(Infinity)}),
        useRef(value) { const i = cursor++; if (!(i in slots)) slots[i] = {current: value}; return slots[i]; },
        useState(value) { const i = cursor++; if (!(i in slots)) slots[i] = value;
            return [slots[i], next => { slots[i] = typeof next === "function" ? next(slots[i]) : next; }]; },
        useEffect(effect) { cursor++; if (first) effects.push(effect); },
        useLayoutEffect() { cursor++; },
    };
    const host = {React, registerPlugin() {}, crypto: {randomUUID: options.uuid || (() => "op")},
        navigator: {locks: options.locks || {request: async (key, action) => action()}},
        localStorage: options.storage || {getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value)}};
    const context = vm.createContext({window: host, document: {hidden: false},
        setInterval() {}, clearInterval() {},
        Model: {...Model, createClient: () => ({request, bindUser() {}})}});
    vm.runInContext(fs.readFileSync(__dirname + "/index.js", "utf8"), context);
    const Console = vm.runInContext("Console", context);
    function render() { cursor = 0; tree = Console({onClose() {}}); first = false; return tree; }
    function nodes(node = tree) {
        if (!node || typeof node !== "object") return [];
        return [node, ...node.children.flatMap(child => nodes(child))];
    }
    function find(predicate) {
        const node = nodes().find(predicate);
        assert.ok(node, "Expected UI control not found");
        return node;
    }
    async function settle() { await new Promise(resolve => setImmediate(resolve)); render(); }
    render(); effects.forEach(effect => cleanups.push(effect()));
    return {settle, render, find, nodes, values, close: () => cleanups.forEach(fn => fn?.()),
        button: name => find(n => n.type === "button" && n.children.includes(name)),
        message: () => find(n => n.type === "textarea")};
}

function server() {
    const p = n => ({id: `p${n}`, create_at: n, author: "Agent", message: `Message ${n}`, root_id: ""});
    const state = {posts: Array.from({length: 120}, (_, i) => p(i + 1)), calls: [], afterPage: null};
    state.request = async path => {
        state.calls.push(path);
        if (path === "/bootstrap") return {user: {id: "human", username: "David"}, agents: [],
            conversations: [{id: "chat", name: "Review", kind: "channel", members: [], observers: []}]};
        const before = new URL("http://fixture" + path).searchParams.get("before");
        const end = before ? state.posts.findIndex(p => p.id === before) : state.posts.length;
        if (end < 0) throw Object.assign(new Error("post unavailable"), {status: 404});
        const posts = state.posts.slice(Math.max(0, end - 50), end);
        const result = {posts, next_before: end > 50 ? posts[0].id : null};
        if (state.afterPage) { const action = state.afterPage; state.afterPage = null; action(); }
        return result;
    };
    return state;
}

async function opened(state) {
    const ui = consoleHarness(state.request);
    await ui.settle();
    await ui.find(n => n.type === "button" && n.props.className?.startsWith("murmur-conversation")).props.onClick();
    ui.render();
    return ui;
}

test("actual refresh replaces deleted complete history without clearing a draft", async () => {
    const state = server(); state.posts = state.posts.slice(0, 2);
    const ui = await opened(state);
    await ui.message().props.onChange({target: {value: "Unsent draft"}}); ui.render();
    state.posts = state.posts.slice(1);
    await ui.button("Refresh").props.onClick(); ui.render();
    assert.deepEqual(ui.nodes().filter(n => n.props["data-post-id"]).map(n => n.props["data-post-id"]), ["p2"]);
    assert.equal(ui.message().props.value, "Unsent draft");
});

test("actual refresh repairs a deleted first-page cursor before Load older", async () => {
    const state = server(), ui = await opened(state);
    state.posts = state.posts.filter(p => p.id !== "p71");
    await ui.button("Refresh").props.onClick(); ui.render();
    await ui.button("Load older messages").props.onClick(); ui.render();
    assert.ok(state.calls.at(-1).endsWith("before=p70"));
    assert.ok(ui.nodes().some(n => n.props["data-post-id"] === "p20"));
});

test("a cursor deleted after older pages load recovers without losing the draft", async () => {
    const state = server(), ui = await opened(state);
    await ui.button("Load older messages").props.onClick(); ui.render(); // p21…p120, cursor p21
    await ui.message().props.onChange({target: {value: "Keep this draft"}}); ui.render();
    state.posts = state.posts.filter(p => p.id !== "p21");
    await ui.button("Refresh").props.onClick(); ui.render();
    assert.ok(ui.nodes().some(n => n.props["data-post-id"] === "p22")); // loaded older page preserved
    await ui.button("Load older messages").props.onClick(); ui.render(); // cursor 404, canonical recovery
    assert.ok(state.calls.includes("/conversations/chat/posts?limit=50&before=p21"));
    assert.equal(ui.message().props.value, "Keep this draft");
    assert.ok(ui.nodes().some(n => n.children.includes("History changed; reloaded the latest page. You can load older messages again.")));
    await ui.button("Load older messages").props.onClick(); ui.render();
    assert.ok(state.calls.at(-1).endsWith("before=p71"));
    assert.ok(ui.nodes().some(n => n.props["data-post-id"] === "p20"));
    assert.ok(!ui.nodes().some(n => n.props["data-post-id"] === "p21"));
});

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return {promise, resolve, reject};
}
function dmServer() {
    const state = {human: "human", calls: [], opens: [], sends: [], conversations: [
        {id: "chat", name: "Existing channel", kind: "channel", members: ["a", "b"], observers: []},
    ]};
    state.ready = agent => {
        const conversation = {id: `dm-${agent}`, name: `Human / ${agent}`, kind: "dm", members: [agent], observers: []};
        state.conversations.push(conversation);
        return {conversation};
    };
    state.request = async (path, options) => {
        state.calls.push({path, options});
        if (path === "/bootstrap") return {user: {id: state.human, username: state.human},
            agents: [{id: "a", name: "Same name"}, {id: "b", name: "Same name"}], conversations: [...state.conversations]};
        if (path === "/dms") {
            const call = {body: JSON.parse(options.body), ...deferred()};
            state.opens.push(call);
            return call.promise;
        }
        if (path === "/posts") {
            const call = {body: JSON.parse(options.body), ...deferred()};
            state.sends.push(call);
            return call.promise;
        }
        if (path.startsWith("/operations/")) return state.status(path);
        if (path.startsWith("/threads/")) {
            const url = new URL("http://fixture" + path), id = decodeURIComponent(url.pathname.split("/").at(-1));
            const root = (state.posts || []).find(p => p.id === id && !p.root_id);
            if (!root) throw Object.assign(new Error("post unavailable"), {status: 404});
            const all = state.posts.filter(p => p.id === id || p.root_id === id).sort(Model.comparePosts);
            const before = url.searchParams.get("before");
            const end = before ? all.findIndex(p => p.id === before) : all.length;
            if (end < 0) throw Object.assign(new Error("post unavailable"), {status: 404});
            const posts = all.slice(Math.max(0, end - 50), end);
            return {root, posts, next_before: end > 50 ? posts[0].id : null};
        }
        return {posts: state.posts || [], next_before: null};
    };
    return state;
}
function selectAgent(ui, agent) {
    ui.find(n => n.props["aria-label"] === "Filter by agent").props.onChange({target: {value: agent}});
    ui.render();
}
function selectChat(ui, id) {
    return ui.find(n => n.type === "button" && n.props.className?.startsWith("murmur-conversation") &&
        ui.nodes(n).some(c => c.children.includes(id === "chat" ? "Existing channel" : `Human / ${id.slice(3)}`))).props.onClick();
}
const selectedName = ui => {
    if (!ui.nodes().some(n => n.type === "textarea")) return undefined;
    const pane = ui.nodes().find(n => n.type === "main");
    return ui.nodes(pane).find(c => c.type === "h2")?.children[0];
};
async function dmUI(state, options) {
    const ui = consoleHarness(state.request, options);
    await ui.settle(); selectAgent(ui, "a"); return ui;
}

test("opening the exact actor clears hiding filters, selects a root DM and preserves existing drafts without posting", async () => {
    const state = dmServer();
    state.posts = [{id: "root", create_at: 1, author: "Agent", message: "Existing thread", root_id: ""}];
    const ui = await dmUI(state);
    await selectChat(ui, "chat"); ui.render();
    await ui.button("Reply in thread").props.onClick(); ui.render();
    await ui.message().props.onChange({target: {value: "Keep my channel draft"}}); ui.render();
    ui.find(n => n.props["aria-label"] === "Find conversations").props.onChange({target: {value: "hidden"}}); ui.render();
    ui.find(n => n.props["aria-label"] === "Conversation type").props.onChange({target: {value: "channel"}}); ui.render();
    const pending = ui.button("Message this agent").props.onClick(); await ui.settle();
    assert.deepEqual(state.opens[0].body, {op_id: "op", agent_id: "a"});
    state.opens[0].resolve(state.ready("a")); await pending; ui.render();
    assert.equal(selectedName(ui), "Human / a");
    assert.equal(ui.find(n => n.props["aria-label"] === "Find conversations").props.value, "");
    assert.equal(ui.find(n => n.props["aria-label"] === "Conversation type").props.value, "");
    assert.equal(ui.message().props.value, "");
    assert.ok(ui.nodes().some(n => n.children.includes("Post as human · new discussion")));
    await selectChat(ui, "chat"); ui.render();
    await ui.button("Reply in thread").props.onClick(); ui.render();
    assert.equal(ui.message().props.value, "Keep my channel draft");
    assert.equal(state.calls.filter(c => c.path === "/posts").length, 0);
});

test("a ready-DM follow cannot navigate after another window replaces its operation record", async () => {
    const state = dmServer(), ui = await dmUI(state);
    const pending = ui.button("Message this agent").props.onClick(); await ui.settle();
    state.opens[0].resolve(state.ready("a")); await pending; ui.render();
    await selectChat(ui, "chat"); ui.render();
    const follow = ui.button("Open ready DM").props.onClick();
    const store = new Model.OpenStore({getItem: key => ui.values.get(key), setItem: (key, value) => ui.values.set(key, value)}, "human");
    store.write("a", {op_id: "replacement", agent_id: "a", status: "pending"});
    await follow; ui.render();
    assert.equal(selectedName(ui), "Existing channel");
    assert.ok(ui.nodes().some(n => n.children.includes("The saved DM opening changed in another window. Select the agent again.")));
    await ui.button("Message this agent").props.onClick(); ui.render();
    assert.equal(state.opens.length, 1);
    assert.ok(ui.button("Check open status"));
});

test("cached Message action holds a departed target; explicit history reveals the DM without rejoining or posting", async () => {
    const state = dmServer(), ui = await dmUI(state);
    const pending = ui.button("Message this agent").props.onClick(); await ui.settle();
    state.opens[0].resolve(state.ready("a")); await pending; ui.render();
    await selectChat(ui, "chat"); ui.render();
    await ui.message().props.onChange({target: {value: "Keep this channel draft"}}); ui.render();
    state.conversations.find(c => c.id === "dm-a").members = [];
    const completed = ui.values.get("murmur.operator.open.human.a");
    await ui.button("Message this agent").props.onClick(); ui.render();
    assert.equal(selectedName(ui), "Existing channel");
    assert.equal(ui.message().props.value, "Keep this channel draft");
    assert.ok(ui.nodes().some(n => n.props.role === "status" && n.children.some(c => c.includes("Same name (a) is not a member"))));
    assert.equal(ui.values.get("murmur.operator.open.human.a"), completed);
    ui.find(n => n.props["aria-label"] === "Find conversations").props.onChange({target: {value: "hidden history"}}); ui.render();
    ui.find(n => n.props["aria-label"] === "Conversation type").props.onChange({target: {value: "channel"}}); ui.render();
    await ui.button("Open ready DM").props.onClick(); ui.render();
    assert.equal(selectedName(ui), "Human / a");
    assert.equal(ui.find(n => n.props["aria-label"] === "Filter by agent").props.value, "");
    assert.equal(ui.find(n => n.props["aria-label"] === "Conversation type").props.value, "");
    assert.equal(ui.find(n => n.props["aria-label"] === "Find conversations").props.value, "");
    assert.ok(ui.nodes().some(n => n.type === "button" && n.props["aria-pressed"]));
    assert.ok(ui.nodes().some(n => n.props.role === "status" && n.children.some(c => c.includes("Same name (a) is not a member") && c.includes("viewing its history"))));
    assert.equal(ui.message().props.disabled, false);
    assert.deepEqual(state.conversations.find(c => c.id === "dm-a").members, []);
    assert.equal(ui.values.get("murmur.operator.open.human.a"), completed);
    assert.equal(state.opens.length, 1);
    assert.equal(state.calls.filter(c => c.path === "/posts").length, 0);
    await selectChat(ui, "chat"); ui.render();
    assert.equal(ui.message().props.value, "Keep this channel draft");
});

test("a fresh Message result cannot navigate after the target leaves before catalogue refresh", async () => {
    const state = dmServer(), ui = await dmUI(state);
    await selectChat(ui, "chat"); ui.render();
    const pending = ui.button("Message this agent").props.onClick(); await ui.settle();
    const ready = state.ready("a"); ready.conversation.members = [];
    state.opens[0].resolve(ready); await pending; ui.render();
    assert.equal(selectedName(ui), "Existing channel");
    assert.ok(ui.nodes().some(n => n.props.role === "status" && n.children.some(c => c.includes("is not a member"))));
    assert.ok(ui.button("Open ready DM"));
    assert.equal(state.calls.filter(c => c.path === "/posts").length, 0);
});

test("reversed A/B responses and a changed draft cannot hijack the current view; ready DM requires a fresh follow action", async () => {
    const state = dmServer(), ui = await dmUI(state, {uuid: (() => { let i = 0; return () => `op-${++i}`; })()});
    const a = ui.button("Message this agent").props.onClick(); await ui.settle();
    selectAgent(ui, "b");
    const b = ui.button("Message this agent").props.onClick(); await ui.settle();
    state.opens[1].resolve(state.ready("b")); await b; ui.render();
    await ui.message().props.onChange({target: {value: "B draft"}}); ui.render();
    state.opens[0].resolve(state.ready("a")); await a; ui.render();
    assert.equal(selectedName(ui), "Human / b"); assert.equal(ui.message().props.value, "B draft");
    selectAgent(ui, "a");
    const follow = ui.button("Open ready DM").props.onClick();
    await ui.message().props.onChange({target: {value: "Still editing B"}});
    await follow; ui.render();
    assert.equal(selectedName(ui), "Human / b");
    await ui.button("Open ready DM").props.onClick(); ui.render();
    assert.equal(selectedName(ui), "Human / a");
    selectAgent(ui, "b"); await ui.button("Open ready DM").props.onClick(); ui.render();
    assert.equal(ui.message().props.value, "Still editing B");
});

test("switching conversations during an opening preserves that view and exposes the ready DM", async () => {
    const state = dmServer(), ui = await dmUI(state);
    const pending = ui.button("Message this agent").props.onClick(); await ui.settle();
    await selectChat(ui, "chat"); ui.render();
    state.opens[0].resolve(state.ready("a")); await pending; ui.render();
    assert.equal(selectedName(ui), "Existing channel");
    await ui.button("Open ready DM").props.onClick(); ui.render();
    assert.equal(selectedName(ui), "Human / a");
});

test("posting while an earlier DM open is pending retains the active composer and send uncertainty", async () => {
    const state = dmServer(), ui = await dmUI(state);
    await selectChat(ui, "chat"); ui.render();
    await ui.message().props.onChange({target: {value: "Keep this exact request"}}); ui.render();
    const opening = ui.button("Message this agent").props.onClick(); await ui.settle();
    const sending = ui.button("Post message").props.onClick(); await ui.settle();
    assert.equal(state.sends.length, 1);
    state.opens[0].resolve(state.ready("a")); await opening; ui.render();
    assert.equal(selectedName(ui), "Existing channel");
    assert.equal(ui.message().props.value, "Keep this exact request");
    state.sends[0].reject(new Error("Lost post response")); await sending; ui.render();
    assert.ok(ui.nodes().some(n => n.props.role === "alert" && n.children.some(c => c.includes("Lost post response"))));
    assert.equal(ui.message().props.value, "Keep this exact request");
    assert.equal(ui.message().props.disabled, true);
    assert.ok(ui.button("Check send status"));
    assert.ok(ui.button("Open ready DM"));
});

test("checking a send while an earlier DM open is pending retains recovery errors and the original destination", async () => {
    const state = dmServer(), ui = await dmUI(state);
    await selectChat(ui, "chat"); ui.render();
    await ui.message().props.onChange({target: {value: "Uncertain channel request"}}); ui.render();
    const sending = ui.button("Post message").props.onClick(); await ui.settle();
    state.sends[0].reject(new Error("Lost response")); await sending; ui.render();
    const opening = ui.button("Message this agent").props.onClick(); await ui.settle();
    const recovery = deferred();
    state.status = () => recovery.promise;
    const checking = ui.button("Check send status").props.onClick(); await ui.settle();
    assert.equal(state.calls.at(-1).path, "/operations/op");
    state.opens[0].resolve(state.ready("a")); await opening; ui.render();
    assert.equal(selectedName(ui), "Existing channel");
    assert.equal(ui.message().props.value, "Uncertain channel request");
    recovery.reject(new Error("Recovery unavailable")); await checking; ui.render();
    assert.ok(ui.nodes().some(n => n.props.role === "alert" && n.children.includes("Recovery unavailable")));
    assert.equal(ui.message().props.value, "Uncertain channel request");
    assert.equal(ui.message().props.disabled, true);
    assert.equal(state.sends.length, 1);
    assert.equal(state.sends[0].body.channel_id, "chat");
    assert.ok(ui.button("Check send status"));
});

test("closing invalidates late responses; reopen can recover persisted completion without automatic navigation", async () => {
    const state = dmServer(), values = new Map(), ui = await dmUI(state, {values});
    const pending = ui.button("Message this agent").props.onClick(); await ui.settle();
    ui.button("Close").props.onClick(); ui.close();
    state.opens[0].resolve(state.ready("a")); await pending;
    const reopened = await dmUI(state, {values});
    assert.equal(selectedName(reopened), undefined);
    await reopened.button("Open ready DM").props.onClick(); reopened.render();
    assert.equal(selectedName(reopened), "Human / a"); assert.equal(state.opens.length, 1);
});

test("timeout and reload retain the pair and operation ID; recovery completion uses durable channel_id", async () => {
    const state = dmServer(), values = new Map(), ui = await dmUI(state, {values});
    const pending = ui.button("Message this agent").props.onClick(); await ui.settle();
    state.opens[0].reject(new Error("Timed out")); await pending; ui.render();
    assert.ok(ui.button("Check open status")); ui.close();
    const reloaded = await dmUI(state, {values, uuid: () => "must-not-use"});
    state.status = () => { state.ready("a"); return {status: "complete", result: {channel_id: "dm-a"}}; };
    await reloaded.button("Check open status").props.onClick(); reloaded.render();
    assert.equal(selectedName(reloaded), "Human / a");
    assert.ok(state.calls.some(c => c.path === "/operations/op"));
    assert.equal(state.opens.length, 1);
});

test("a missing operation requires an explicit retry of the saved ID, while terminal failure permits a new intent", async () => {
    const state = dmServer(), ui = await dmUI(state, {uuid: (() => { let i = 0; return () => `op-${++i}`; })()});
    let pending = ui.button("Message this agent").props.onClick(); await ui.settle();
    state.opens[0].reject(new Error("Lost response")); await pending; ui.render();
    state.status = () => { throw Object.assign(new Error("Missing"), {status: 404}); };
    await ui.button("Check open status").props.onClick(); ui.render();
    assert.equal(state.opens.length, 1);
    pending = ui.button("Retry original opening").props.onClick(); await ui.settle();
    assert.deepEqual(state.opens[1].body, state.opens[0].body);
    state.opens[1].reject(new Error("Still uncertain")); await pending; ui.render();
    state.status = () => ({status: "failed", error: "Agent mapping changed"});
    await ui.button("Check open status").props.onClick(); ui.render();
    assert.ok(ui.nodes().some(n => n.children.includes("Opening failed: Agent mapping changed")));
    pending = ui.button("Try new opening").props.onClick(); await ui.settle();
    assert.notEqual(state.opens[2].body.op_id, state.opens[0].body.op_id);
    state.opens[2].resolve(state.ready("a")); await pending;
});

test("double click and two windows serialize one durable pair intent before any HTTP", async () => {
    const state = dmServer(), values = new Map(), tails = new Map(), keys = [];
    const locks = {request(key, action) {
        keys.push(key);
        const result = (tails.get(key) || Promise.resolve()).then(action);
        tails.set(key, result.catch(() => {})); return result;
    }};
    const a = await dmUI(state, {values, locks, uuid: () => "window-a"});
    const b = await dmUI(state, {values, locks, uuid: () => "window-b"});
    const click = a.button("Message this agent").props.onClick;
    const first = click(), duplicate = click(), second = b.button("Message this agent").props.onClick();
    await a.settle();
    assert.equal(state.opens.length, 1);
    assert.equal(JSON.parse(values.get(keys[0])).op_id, "window-a");
    state.opens[0].resolve(state.ready("a")); await Promise.all([first, duplicate, second]);
    assert.equal(state.opens.length, 1); assert.equal(keys[0], keys[1]);
});

test("a storage failure prevents opening HTTP and an unresolved open does not lock an unrelated draft", async () => {
    const state = dmServer(), values = new Map();
    const ui = await dmUI(state, {storage: {getItem: key => values.get(key), setItem(key, value) {
        if (key.startsWith("murmur.operator.open.")) throw new Error("Storage unavailable");
        values.set(key, value);
    }}});
    await ui.button("Message this agent").props.onClick(); ui.render();
    assert.equal(state.opens.length, 0);
    assert.ok(ui.nodes().some(n => n.children.some(c => typeof c === "string" && c.includes("Storage unavailable"))));
    const next = await dmUI(state);
    const pending = next.button("Message this agent").props.onClick(); await next.settle();
    await selectChat(next, "chat"); next.render();
    await next.message().props.onChange({target: {value: "Independent draft"}}); next.render();
    assert.equal(next.message().props.disabled, false);
    assert.equal(next.message().props.value, "Independent draft");
    state.opens[0].reject(new Error("Timeout")); await pending;
});

test("account changes cannot follow or recover another human's saved intent", async () => {
    const state = dmServer(), values = new Map(), ui = await dmUI(state, {values});
    const pending = ui.button("Message this agent").props.onClick(); await ui.settle();
    state.human = "other-human";
    await ui.button("Refresh").props.onClick(); ui.render();
    state.opens[0].resolve(state.ready("a")); await pending; ui.render();
    assert.equal(selectedName(ui), undefined);
    await ui.button("Check open status").props.onClick();
    assert.ok(!state.calls.some(c => c.path.startsWith("/operations/")));
    ui.close();
    const other = await dmUI(state, {values, uuid: () => "other-op"});
    assert.ok(!other.nodes().some(n => n.children.includes("Check open status")));
    const otherPending = other.button("Message this agent").props.onClick(); await other.settle();
    assert.deepEqual(state.opens[1].body, {op_id: "other-op", agent_id: "a"});
    state.opens[1].resolve(state.ready("a")); await otherPending;
});

test("a durable rejected send survives reload and restores its text without reposting", async () => {
    const state = dmServer(), values = new Map(), ui = await dmUI(state, {values});
    state.posts = [{id: "root", create_at: 1, author: "Agent", message: "Old thread", root_id: ""}];
    await selectChat(ui, "chat"); ui.render();
    await ui.button("Reply in thread").props.onClick(); ui.render();
    await ui.message().props.onChange({target: {value: "Preserve this exact request"}}); ui.render();
    const sending = ui.button("Post message").props.onClick(); await ui.settle();
    const pending = state.sends[0].body;
    state.sends[0].reject(new Error("Lost response")); await sending; ui.render();
    state.status = () => ({status: "failed", dispatch: "rejected", op_id: pending.op_id, error: "Thread was deleted"});
    await ui.button("Check send status").props.onClick(); ui.render();
    assert.ok(ui.button("Restore text for editing"));
    assert.ok(ui.button("Discard rejected send"));
    assert.ok(!ui.nodes().some(n => n.children.includes("Retry original send")));
    ui.close();
    const reloaded = await dmUI(state, {values});
    await selectChat(reloaded, "chat"); reloaded.render();
    await reloaded.button("Reply in thread").props.onClick(); reloaded.render();
    assert.equal(reloaded.message().props.disabled, true);
    await reloaded.button("Restore text for editing").props.onClick(); reloaded.render();
    assert.equal(reloaded.message().props.value, "Preserve this exact request");
    assert.equal(reloaded.message().props.disabled, false);
    assert.equal(state.sends.length, 1);
    assert.equal(state.sends[0].body.root_id, "root");
});

test("only an exact durable no-dispatch marker permits discarding a failed send", async () => {
    const state = dmServer(), ui = await dmUI(state);
    await selectChat(ui, "chat"); ui.render();
    await ui.message().props.onChange({target: {value: "Original text"}}); ui.render();
    const sending = ui.button("Post message").props.onClick(); await ui.settle();
    state.sends[0].reject(Object.assign(new Error("Denied"), {status: 403,
        result: {status: "failed", dispatch: "rejected", op_id: "other-operation", error: "Denied"}}));
    await sending; ui.render();
    assert.ok(ui.button("Check send status"));
    assert.ok(!ui.nodes().some(n => n.children.includes("Restore text for editing")));
    state.status = () => ({status: "failed", error: "Unclassified failure"});
    await ui.button("Check send status").props.onClick(); ui.render();
    assert.equal(ui.message().props.disabled, true);
    state.status = () => { throw Object.assign(new Error("Missing operation"), {status: 404}); };
    await ui.button("Check send status").props.onClick(); ui.render();
    assert.ok(ui.button("Retry original send"));
    assert.ok(!ui.nodes().some(n => n.children.includes("Discard rejected send")));
    state.status = () => ({status: "failed", dispatch: "rejected", op_id: "op", error: "Rejected before posting"});
    await ui.button("Check send status").props.onClick(); ui.render();
    await ui.button("Discard rejected send").props.onClick(); ui.render();
    assert.equal(ui.message().props.value, "");
    assert.equal(ui.message().props.disabled, false);
    assert.equal(state.sends.length, 1);
});

test("a structured POST rejection is persisted before exposing restore actions", async () => {
    const state = dmServer(), ui = await dmUI(state);
    await selectChat(ui, "chat"); ui.render();
    await ui.message().props.onChange({target: {value: "No post was attempted"}}); ui.render();
    const sending = ui.button("Post message").props.onClick(); await ui.settle();
    state.sends[0].reject(Object.assign(new Error("Root unavailable"), {status: 404,
        result: {status: "failed", dispatch: "rejected", op_id: "op", error: "Root unavailable"}}));
    await sending; ui.render();
    assert.equal(JSON.parse(ui.values.get("murmur.operator.human.chat:")).rejected.op_id, "op");
    assert.ok(ui.button("Restore text for editing"));
    assert.equal(state.calls.filter(c => c.path.startsWith("/operations/")).length, 0);
});

test("the selected inherited reader is visibly distinct from participating agents", async () => {
    const state = dmServer();
    state.conversations = [{id: "chat", name: "Existing channel", kind: "dm", members: ["b"], observers: ["a"]}];
    const ui = await dmUI(state);
    ui.find(n => n.type === "input" && n.props.type === "checkbox").props.onChange({target: {checked: true}}); ui.render();
    await selectChat(ui, "chat"); ui.render();
    const main = ui.nodes().find(n => n.type === "main");
    const text = ui.nodes(main).flatMap(n => n.children.filter(c => typeof c === "string")).join(" ");
    assert.match(text, /Participating agents: Same name · b/);
    assert.match(text, /Inherited readers: Same name · a/);
    assert.match(text, /inherited read-only access.*do not notify it/);
    assert.equal(ui.message().props.disabled, false);
});

test("bounded thread pages retain their root and recover a deleted cursor without losing the draft", async () => {
    const state = dmServer();
    state.posts = [{id: "root", create_at: 1, author: "Agent", message: "Thread root", root_id: ""},
        ...Array.from({length: 120}, (_, i) => ({id: `r${i + 1}`, create_at: i + 2, author: "Agent", root_id: "root", message: `Reply ${i + 1}`}))];
    const ui = await dmUI(state);
    await selectChat(ui, "chat"); ui.render();
    await ui.button("Reply in thread").props.onClick(); ui.render();
    const threadIDs = () => ui.nodes(ui.find(n => n.props["aria-label"] === "Conversation thread"))
        .filter(n => n.props["data-post-id"]).map(n => n.props["data-post-id"]);
    assert.equal(threadIDs().length, 51);
    assert.equal(threadIDs().filter(id => id === "root").length, 1);
    await ui.button("Load older thread messages").props.onClick(); ui.render();
    assert.equal(threadIDs().length, 101);
    await ui.message().props.onChange({target: {value: "Keep this thread draft"}}); ui.render();
    state.posts = state.posts.filter(p => p.id !== "r21");
    await ui.button("Load older thread messages").props.onClick(); ui.render();
    assert.equal(ui.message().props.value, "Keep this thread draft");
    assert.equal(threadIDs().length, 51);
    assert.ok(!threadIDs().includes("r21"));
    assert.ok(ui.nodes().some(n => n.children.includes("Thread history changed; reloaded the latest page. Your draft was kept.")));
    await ui.button("Load older thread messages").props.onClick(); ui.render();
    assert.equal(threadIDs().filter(id => id === "root").length, 1);
});


test("managed conversation readership is disclosed for DMs and follows ordinary refresh", async () => {
    const state = dmServer(), request = state.request;
    state.conversations[0].kind = "dm";
    let capability = {version: 1, enabled: true};
    state.request = async (path, options) => {
        const data = await request(path, options);
        return path === "/bootstrap" ? {...data, capabilities: {managed_conversation_reads: capability}} : data;
    };
    const ui = await dmUI(state);
    await selectChat(ui, "chat"); ui.render();
    const disclosure = ui.find(n => n.props.className === "murmur-readership");
    assert.equal(disclosure.props.role, "note");
    assert.match(disclosure.children.join(" "), /Enrolled agents can read all Murmur-managed conversations, including DMs/);
    assert.match(disclosure.children.join(" "), /Participants shown here can post; the list does not include everyone who can read/);
    assert.ok(ui.nodes().some(n => n.children.includes("Agents who can post: Same name · a, Same name · b")));
    await ui.message().props.onChange({target: {value: "Keep my draft"}}); ui.render();
    capability = {version: 1, enabled: false};
    const before = state.calls.length;
    await ui.button("Refresh").props.onClick(); ui.render();
    assert.equal(ui.nodes().some(n => n.props.className === "murmur-readership"), false);
    assert.ok(ui.nodes().some(n => n.children.includes("Participating agents: Same name · a, Same name · b")));
    assert.equal(ui.message().props.value, "Keep my draft");
    assert.equal(state.calls.length - before, 2, "reuse bootstrap and selected history refresh without another endpoint");
    assert.equal(state.calls.some(call => call.path.includes("health")), false);
    ui.close();
});

test("managed readership disclosure is absent without a supported enabled capability", async t => {
    const cases = [undefined, null, {version: 1, enabled: false}, {version: 2, enabled: true}, {version: 1, enabled: "true"}];
    for (const capability of cases) await t.test(JSON.stringify(capability) || "missing", async () => {
        const state = server(), request = state.request;
        state.request = async path => {
            const data = await request(path);
            return path === "/bootstrap" ? {...data, capabilities: {managed_conversation_reads: capability}} : data;
        };
        const ui = consoleHarness(state.request); await ui.settle();
        assert.equal(ui.nodes().some(n => n.props.className === "murmur-readership"), false);
        assert.deepEqual(state.calls, ["/bootstrap"]);
        ui.close();
    });
});
