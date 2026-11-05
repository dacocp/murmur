/* Loaded after model.js by build.py. React comes from the Mattermost host. */
"use strict";
const React = window.React;
const h = React.createElement;
const {useState, useEffect, useRef, useLayoutEffect} = React;
const emptyDraft = {text: "", pending: null};
const when = time => time ? new Date(time).toLocaleString([], {dateStyle: "short", timeStyle: "short"}) : "No messages";
const button = (text, onClick, props = {}) => h("button", {type: "button", onClick, ...props}, text);

function captureAnchor(element) {
    if (!element) return null;
    const top = element.getBoundingClientRect().top;
    const post = [...element.children].find(p => p.dataset.postId && p.getBoundingClientRect().bottom > top);
    return post ? {id: post.dataset.postId, offset: post.getBoundingClientRect().top - top} : null;
}
function restoreAnchor(element, anchor) {
    if (!element || !anchor) return;
    const post = [...element.children].find(p => p.dataset.postId === anchor.id);
    if (post) element.scrollTop += post.getBoundingClientRect().top - element.getBoundingClientRect().top - anchor.offset;
}

function Console({onClose}) {
    const client = useRef(Model.createClient(window));
    const store = useRef(null);
    const openStore = useRef(null);
    const openBusy = useRef(new Set());
    const openRevision = useRef(0);
    const agent = useRef("");
    const accountValid = useRef(true);
    const active = useRef(true);
    const refreshBusy = useRef(false);
    const generation = useRef(0);
    const draftRevision = useRef(0);
    const selected = useRef({channel: "", root: ""});
    const currentPosts = useRef([]);
    const currentBefore = useRef(null);
    const currentThread = useRef([]);
    const currentThreadBefore = useRef(null);
    const list = useRef(null);
    const threadList = useRef(null);
    const anchor = useRef(null);
    const threadAnchor = useRef(null);
    const dialog = useRef(null);
    const [boot, setBoot] = useState(null);
    const [error, setError] = useState("");
    const [lastUpdated, setLastUpdated] = useState(0);
    const [filter, setFilter] = useState({agent: "", inherited: false, kind: "", search: ""});
    const [selection, setSelection] = useState(selected.current);
    const [posts, setPosts] = useState([]);
    const [thread, setThread] = useState([]);
    const [threadBefore, setThreadBefore] = useState(null);
    const [threadLoading, setThreadLoading] = useState(false);
    const [before, setBefore] = useState(null);
    const [loading, setLoading] = useState(false);
    const [follow, setFollow] = useState(false);
    const followRef = useRef(false);
    const [newCount, setNewCount] = useState(0);
    const [draft, setDraft] = useState(emptyDraft);
    const [sending, setSending] = useState(false);
    const [retryAllowed, setRetryAllowed] = useState(false);
    const [notice, setNotice] = useState("");
    const [opening, setOpening] = useState({entry: null, busy: false, retry: false, message: ""});

    const showError = e => { if (active.current) setError(e.message || String(e)); };
    const isSelected = target => active.current && selected.current.channel === target.channel && selected.current.root === target.root;
    function updateDraft(target = selected.current) {
        draftRevision.current++;
        try { setDraft(store.current.read(target.channel, target.root)); }
        catch (e) { setDraft(emptyDraft); showError(e); }
        setRetryAllowed(false);
    }
    function applyLatest(latest, replace = false) {
        anchor.current = captureAnchor(list.current);
        const previous = currentPosts.current;
        const incoming = latest.posts;
        const page = replace ? latest : Model.mergePage(previous, latest, currentBefore.current);
        const next = page.posts;
        const oldIds = new Set(previous.map(p => p.id));
        if (!replace && previous.length && !followRef.current) {
            setNewCount(n => n + incoming.filter(p => !oldIds.has(p.id)).length);
        }
        currentPosts.current = next;
        currentBefore.current = page.next_before;
        setPosts(next);
        setBefore(page.next_before);
        if (replace) setNewCount(0);
    }
    function applyThread(page, replace = false) {
        const next = replace ? page : Model.mergePage(currentThread.current, page, currentThreadBefore.current);
        threadAnchor.current = captureAnchor(threadList.current);
        currentThread.current = next.posts;
        currentThreadBefore.current = next.next_before;
        setThread([page.root, ...next.posts.filter(p => p.id !== page.root.id)]);
        setThreadBefore(next.next_before);
    }
    async function loadThread(target, ticket, replace = false) {
        const page = await client.current.request(`/threads/${encodeURIComponent(target.root)}?limit=50`);
        if (!active.current || ticket !== generation.current) return;
        if (page.root?.id !== target.root || page.root.root_id) throw new Error("The server returned a different thread root.");
        applyThread(page, replace);
    }
    async function loadSelected(target, ticket, replace = false) {
        try {
            const page = await client.current.request(`/conversations/${encodeURIComponent(target.channel)}/posts?limit=50`);
            if (!active.current || ticket !== generation.current) return;
            applyLatest(page, replace);
            if (target.root) {
                await loadThread(target, ticket, replace);
            }
        } catch (e) { if (active.current && ticket === generation.current) throw e; }
    }
    async function refresh() {
        if (refreshBusy.current) return;
        refreshBusy.current = true;
        try {
            const data = await client.current.request("/bootstrap");
            if (!active.current) return;
            if (store.current && store.current.userId !== data.user.id) {
                accountValid.current = false; openRevision.current++;
                throw new Error("Your Mattermost account changed. Close and reopen Murmur before continuing.");
            }
            if (!store.current) {
                store.current = new Model.DraftStore(window.localStorage, data.user.id);
                store.current.userId = data.user.id;
                client.current.bindUser(data.user.id);
                openStore.current = new Model.OpenStore(window.localStorage, data.user.id);
            }
            setBoot(data);
            const target = selected.current;
            if (target.channel) await loadSelected(target, generation.current);
            if (active.current) { setLastUpdated(Date.now()); setError(""); }
        } catch (e) { showError(e); }
        finally { refreshBusy.current = false; }
    }
    useEffect(() => {
        active.current = true;
        const previousFocus = document.activeElement;
        dialog.current?.focus();
        refresh();
        const timer = setInterval(() => { if (!document.hidden) refresh(); }, 10000);
        return () => { active.current = false; openRevision.current++; clearInterval(timer); previousFocus?.focus(); };
    }, []);
    useLayoutEffect(() => {
        if (followRef.current && list.current) list.current.scrollTop = list.current.scrollHeight;
        else restoreAnchor(list.current, anchor.current);
        anchor.current = null;
    }, [posts]);
    useLayoutEffect(() => { restoreAnchor(threadList.current, threadAnchor.current); threadAnchor.current = null; }, [thread]);

    async function choose(channel, root = "") {
        openRevision.current++;
        const ticket = ++generation.current;
        const changedChannel = channel !== selected.current.channel;
        const target = {channel, root};
        selected.current = target;
        setSelection(target);
        setNotice(""); setError(""); setThread([]); setThreadBefore(null); setLoading(true); setThreadLoading(false);
        currentThread.current = []; currentThreadBefore.current = null;
        updateDraft(target);
        if (changedChannel) {
            currentPosts.current = []; currentBefore.current = null; setPosts([]); setBefore(null); setNewCount(0);
            followRef.current = false; setFollow(false); anchor.current = null;
        }
        try { await loadSelected(target, ticket, changedChannel); }
        catch (e) { if (ticket === generation.current) showError(e); }
        finally { if (active.current && ticket === generation.current) setLoading(false); }
    }
    async function older() {
        const target = selected.current;
        const ticket = generation.current;
        setLoading(true);
        try {
            const page = await client.current.request(`/conversations/${encodeURIComponent(target.channel)}/posts?limit=50&before=${encodeURIComponent(before)}`);
            if (!active.current || ticket !== generation.current) return;
            anchor.current = captureAnchor(list.current);
            const merged = new Map([...page.posts, ...currentPosts.current].map(p => [p.id, p]));
            currentPosts.current = [...merged.values()].sort(Model.comparePosts);
            currentBefore.current = page.next_before;
            setPosts(currentPosts.current); setBefore(page.next_before);
        } catch (e) {
            if (!active.current || ticket !== generation.current) return;
            if (e.status === 404) {
                try {
                    // The cursor can disappear between refresh and pagination.
                    // Reload canonical history without changing selection/drafts.
                    await loadSelected(target, ticket, true);
                    if (active.current && ticket === generation.current) {
                        setError("");
                        setNotice("History changed; reloaded the latest page. You can load older messages again.");
                    }
                } catch (recovery) { if (active.current && ticket === generation.current) showError(recovery); }
            } else showError(e);
        }
        finally { if (active.current && ticket === generation.current) setLoading(false); }
    }
    async function olderThread() {
        const target = {...selected.current}, ticket = generation.current;
        setThreadLoading(true);
        try {
            const page = await client.current.request(`/threads/${encodeURIComponent(target.root)}?limit=50&before=${encodeURIComponent(threadBefore)}`);
            if (!active.current || ticket !== generation.current) return;
            if (page.root?.id !== target.root || page.root.root_id) throw new Error("The server returned a different thread root.");
            const merged = new Map([...page.posts, ...currentThread.current].map(p => [p.id, p]));
            applyThread({...page, posts: [...merged.values()].sort(Model.comparePosts)}, true);
        } catch (e) {
            if (!active.current || ticket !== generation.current) return;
            if (e.status === 404) {
                try {
                    await loadThread(target, ticket, true);
                    if (isSelected(target)) setNotice("Thread history changed; reloaded the latest page. Your draft was kept.");
                } catch (recovery) { if (isSelected(target)) showError(recovery); }
            } else showError(e);
        } finally { if (active.current && ticket === generation.current) setThreadLoading(false); }
    }
    async function edit(text) {
        openRevision.current++;
        const target = {...selected.current};
        const revision = ++draftRevision.current;
        setDraft({text, pending: null}); setNotice("");
        try {
            const entry = await Model.withDraftLock(window, store.current, target.channel, target.root,
                () => store.current.edit(target.channel, target.root, text));
            if (active.current && revision === draftRevision.current) setDraft(entry);
        } catch (e) {
            if (isSelected(target) && revision === draftRevision.current) { updateDraft(target); showError(e); }
        }
    }
    async function finishSend(target, pending) {
        await Model.withDraftLock(window, store.current, target.channel, target.root,
            () => store.current.complete(target.channel, target.root, pending.op_id));
        if (active.current && selected.current.channel === target.channel && selected.current.root === target.root) {
            updateDraft(target); setNotice("Posted"); setError("");
        }
    }
    async function rejectSend(target, pending, result) {
        const entry = await Model.withDraftLock(window, store.current, target.channel, target.root,
            () => store.current.reject(target.channel, target.root, pending.op_id, result));
        if (isSelected(target)) { setDraft(entry); setRetryAllowed(false); setError(""); }
    }
    async function releaseRejected(discard) {
        openRevision.current++;
        const target = {...selected.current};
        setSending(true);
        try {
            const entry = await Model.withDraftLock(window, store.current, target.channel, target.root,
                () => store.current.releaseRejected(target.channel, target.root, draft, discard));
            if (isSelected(target)) { setDraft(entry); setNotice(discard ? "Rejected send discarded" : "Text restored for editing"); setError(""); }
        } catch (e) {
            if (isSelected(target)) { updateDraft(target); showError(e); }
        } finally { if (active.current) setSending(false); }
    }
    async function send() {
        openRevision.current++;
        const target = {...selected.current};
        setSending(true); setNotice(""); setError("");
        let entry;
        try {
            entry = await Model.withDraftLock(window, store.current, target.channel, target.root,
                () => store.current.prepare(target.channel, target.root, window.crypto.randomUUID(), draft));
            if (isSelected(target)) setDraft(entry); // Persisted above, before any network mutation.
            await client.current.request("/posts", {method: "POST", body: JSON.stringify(entry.pending)});
            await finishSend(target, entry.pending);
            await refresh();
        } catch (e) {
            if (entry?.pending && Model.isRejectedSend(e.result, entry.pending.op_id)) {
                try { await rejectSend(target, entry.pending, e.result); }
                catch (recovery) { if (isSelected(target)) showError(recovery); }
                return;
            }
            if (isSelected(target)) {
                if (e.code === "draft_changed") updateDraft(target);
                showError(new Error(`${e.message} ${entry?.pending ? "The original send is saved. Check its status before sending again." : "Message was not sent."}`));
            }
        } finally { if (active.current) setSending(false); }
    }
    async function checkSend() {
        openRevision.current++;
        const target = {...selected.current};
        setSending(true); setRetryAllowed(false);
        try {
            const pending = await Model.withDraftLock(window, store.current, target.channel, target.root, () => {
                const entry = store.current.read(target.channel, target.root);
                Model.assertVisibleDraft(entry, draft);
                return entry.pending;
            });
            if (!pending) return;
            const result = await client.current.request(`/operations/${encodeURIComponent(pending.op_id)}`);
            if (result.status === "complete") { await finishSend(target, pending); await refresh(); }
            else if (Model.isRejectedSend(result, pending.op_id)) await rejectSend(target, pending, result);
            else if (isSelected(target)) setNotice(result.error || "The original send is still unresolved. It has not been sent again.");
        } catch (e) {
            if (!isSelected(target)) return;
            if (e.code === "draft_changed") updateDraft(target);
            if (e.status === 404) {
                setRetryAllowed(true);
                setNotice("The server has no operation record. You can retry the original send with the same ID and text.");
            } else showError(e);
        } finally { if (active.current) setSending(false); }
    }
    function close() { active.current = false; openRevision.current++; onClose(); }
    function chooseAgent(value) {
        agent.current = value; openRevision.current++;
        setFilter({...filter, agent: value});
        try { setOpening({entry: value ? openStore.current.read(value) : null,
            busy: openBusy.current.has(value), retry: false, message: ""}); }
        catch (e) { setOpening({entry: null, busy: false, retry: false, message: e.message}); }
    }
    function openContext(target, opId) {
        return {human: openStore.current.userId, agent: target, opId, revision: openRevision.current};
    }
    const sameHuman = context => active.current && accountValid.current && openStore.current?.userId === context.human;
    const sameAgent = context => sameHuman(context) && agent.current === context.agent;
    async function visitDM(context, conversation, requireMember = true) {
        // Refresh independently of the periodic refresher; it may already be in flight.
        const data = await client.current.request("/bootstrap");
        if (!sameHuman(context)) return;
        if (data.user.id !== context.human) {
            accountValid.current = false; openRevision.current++;
            showError(new Error("Your Mattermost account changed. Close and reopen Murmur before continuing."));
            return;
        }
        setBoot(data); setLastUpdated(Date.now());
        const saved = openStore.current.read(context.agent);
        if (saved?.op_id !== context.opId || saved.status !== "complete" || saved.conversation.id !== conversation.id) {
            throw new Error("The saved DM opening changed in another window. Select the agent again.");
        }
        const current = data.conversations.find(c => c.id === conversation.id && c.kind === "dm");
        const member = current?.members.includes(context.agent);
        const targetName = data.agents.find(a => a.id === context.agent)?.name || context.agent;
        const departure = `${targetName} (${context.agent}) is not a member of this DM.`;
        if (!current || (requireMember && !member)) {
            if (sameAgent(context)) setOpening(value => ({...value, message: !current ?
                "This DM is no longer available in the conversation catalogue. Your current view was kept." :
                `${departure} Open ready DM to view its history. Opening does not rejoin the agent.`}));
            return;
        }
        if (sameAgent(context) && openRevision.current === context.revision) {
            if (!member) agent.current = "";
            setFilter(value => ({...value, kind: "", search: "", ...(!member ? {agent: "", inherited: false} : {})}));
            const viewing = choose(conversation.id);
            if (!member) {
                setNotice(`${departure} You are viewing its history. Opening it does not rejoin the agent.`);
            }
            await viewing;
        }
    }
    async function openDM(mode = "open") {
        const target = agent.current;
        if (!target || !accountValid.current || openBusy.current.has(target)) return;
        const owner = openStore.current;
        let context = openContext(target, "");
        openBusy.current.add(target);
        setOpening(current => ({...current, busy: true, retry: false, message: ""}));
        try {
            await Model.withDraftLock(window, owner, target, "", async () => {
                if (!sameHuman(context)) return;
                let entry = owner.read(target);
                if (mode === "open") {
                    const prepared = owner.prepare(target, window.crypto.randomUUID());
                    entry = prepared.entry;
                    context.opId = entry.op_id;
                    if (!prepared.created) {
                        if (sameAgent(context)) setOpening({entry, busy: true, retry: false,
                            message: entry.status === "pending" ? "Opening saved; check its status before retrying." : ""});
                        if (entry.status === "complete") await visitDM(context, entry.conversation);
                        return;
                    }
                } else {
                    // A stale tab must never recover or retry a replacement operation.
                    if (!entry || entry.op_id !== opening.entry?.op_id) throw new Error("The saved DM opening changed in another window. Select the agent again.");
                    context.opId = entry.op_id;
                    if (entry.status !== "pending") {
                        if (sameAgent(context)) setOpening({entry, busy: true, retry: false, message: ""});
                        if (entry.status === "complete") await visitDM(context, entry.conversation, mode !== "check");
                        return;
                    }
                }
                if (sameAgent(context)) setOpening({entry, busy: true, retry: false, message: ""});
                let conversation;
                if (mode === "check") {
                    const result = await client.current.request(`/operations/${encodeURIComponent(entry.op_id)}`);
                    if (result.status === "failed") {
                        entry = owner.finish(target, entry.op_id, {status: "failed", error: result.error || "Opening failed."});
                    } else if (result.status === "complete") {
                        conversation = {id: result.result?.channel_id, kind: "dm", name: "Direct message"};
                    }
                } else {
                    const result = await client.current.request("/dms", {method: "POST",
                        body: JSON.stringify({op_id: entry.op_id, agent_id: target})});
                    conversation = result.conversation;
                }
                if (conversation) {
                    if (typeof conversation.id !== "string" || !conversation.id || conversation.kind !== "dm") throw new Error("Unexpected DM opening result. Check open status.");
                    entry = owner.finish(target, entry.op_id, {status: "complete", conversation});
                }
                if (sameAgent(context)) setOpening({entry, busy: true, retry: false,
                    message: entry.status === "pending" ? "The original opening is still unresolved. It has not been retried." : ""});
                if (conversation && sameHuman(context)) await visitDM(context, conversation, mode !== "check");
            });
        } catch (e) {
            if (sameAgent(context)) setOpening(current => ({...current, retry: mode === "check" && e.status === 404,
                message: mode === "check" && e.status === 404 ? "The server has no operation record. Retry the saved opening with the same ID." :
                    `${e.message} Check open status if the opening was saved.`}));
        } finally {
            openBusy.current.delete(target);
            if (sameAgent(context)) setOpening(current => ({...current, busy: false}));
        }
    }
    async function followDM() {
        const entry = opening.entry;
        if (!entry || entry.status !== "complete") return;
        try { await visitDM(openContext(agent.current, entry.op_id), entry.conversation, false); }
        catch (e) { showError(e); }
    }
    function keyDown(event) {
        if (event.key === "Escape") { event.stopPropagation(); close(); }
        if (event.key !== "Tab") return;
        const controls = [...dialog.current.querySelectorAll('button:not(:disabled),input,select,textarea:not(:disabled),[tabindex="0"]')];
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    const conversations = boot ? Model.filterConversations(boot.conversations, filter) : [];
    const chat = boot?.conversations.find(c => c.id === selection.channel);
    const names = new Map((boot?.agents || []).map(a => [a.id, a.name]));
    const agentNames = ids => ids.map(id => `${names.get(id) || id} · ${id.slice(-8)}`).join(", ");
    const participantNames = chat ? agentNames(chat.members) : "";
    const managedReads = boot?.capabilities?.managed_conversation_reads;
    const sharedReadership = managedReads?.version === 1 && managedReads.enabled === true;
    const readerNames = chat ? agentNames(chat.observers || []) : "";
    const selectedRole = !chat || !filter.agent ? "" : chat.members.includes(filter.agent) ?
        "The selected agent participates in this conversation. Delivery follows its preferences." :
        (chat.observers || []).includes(filter.agent) ?
            "The selected agent has inherited read-only access. Messages here do not notify it. Use Message this agent for its own DM." :
            "The selected agent does not participate in this conversation. Use Message this agent for its own DM.";
    const renderPost = (post, isThread = false) => h("article", {
        key: post.id, "data-post-id": post.id, className: "murmur-post",
    }, h("div", {className: "murmur-post-meta"}, h("strong", null, post.author),
        h("time", {dateTime: new Date(post.create_at).toISOString()}, when(post.create_at))),
    h("div", {className: "murmur-post-body"}, post.message),
    !isThread && button(post.root_id ? "Open thread ↗" : "Reply in thread", () => choose(selection.channel, post.root_id || post.id),
        {className: "murmur-text-button"}));

    return h("div", {className: "murmur-overlay"},
        h("section", {className: "murmur-console", role: "dialog", "aria-modal": true,
            "aria-label": "Murmur conversations", tabIndex: -1, ref: dialog, onKeyDown: keyDown},
        h("header", {className: "murmur-header"},
            h("div", null, h("h1", null, "Murmur"), h("span", {className: "murmur-subtitle"}, "Conversations across your agents")),
            h("div", {className: "murmur-header-actions"},
                h("span", {className: "murmur-sync"}, lastUpdated ? `Updated ${when(lastUpdated)}` : "Connecting…"),
                button("Refresh", refresh), button("Close", close, {"aria-label": "Close Murmur"}))),
        sharedReadership && h("p", {className: "murmur-readership", role: "note"},
            "Enrolled agents can read all Murmur-managed conversations, including DMs. Participants shown here can post; the list does not include everyone who can read."),
        error && h("div", {className: "murmur-error", role: "alert"}, error),
        h("div", {className: "murmur-layout" + (selection.root ? " murmur-has-thread" : "")},
            h("aside", {className: "murmur-sidebar", "aria-label": "Conversations"},
                h("div", {className: "murmur-filters"},
                    h("input", {"aria-label": "Find conversations", placeholder: "Find a conversation…", value: filter.search,
                        onChange: e => setFilter({...filter, search: e.target.value})}),
                    h("select", {"aria-label": "Filter by agent", value: filter.agent,
                        onChange: e => chooseAgent(e.target.value)},
                    h("option", {value: ""}, "All agents"), ...(boot?.agents || []).map(a => h("option", {key: a.id, value: a.id}, `${a.name} · ${a.id.slice(-8)}`))),
                    filter.agent && h("div", {className: "murmur-open-agent"},
                        button(opening.busy ? "Opening…" : opening.entry?.status === "failed" ? "Try new opening" : "Message this agent",
                            () => openDM(), {disabled: opening.busy || opening.entry?.status === "pending"}),
                        h("p", null, "Opening sends nothing. Delivery follows the agent’s preferences; enrolled does not mean online."),
                        opening.entry?.status === "pending" && button("Check open status", () => openDM("check"), {disabled: opening.busy}),
                        opening.retry && button("Retry original opening", () => openDM("retry"), {disabled: opening.busy}),
                        opening.entry?.status === "complete" && button("Open ready DM", followDM, {disabled: opening.busy}),
                        h("span", {role: "status"}, opening.entry?.status === "failed" ? `Opening failed: ${opening.entry.error}` : opening.message)),
                    h("div", {className: "murmur-filter-row"},
                        h("select", {"aria-label": "Conversation type", value: filter.kind,
                            onChange: e => setFilter({...filter, kind: e.target.value})},
                        h("option", {value: ""}, "DMs and channels"), h("option", {value: "dm"}, "DMs"), h("option", {value: "channel"}, "Channels"))),
                    filter.agent && h("label", {className: "murmur-check"},
                        h("input", {type: "checkbox", checked: filter.inherited, onChange: e => setFilter({...filter, inherited: e.target.checked})}), "Include inherited conversations")),
                h("div", {className: "murmur-conversation-list"},
                    !conversations.length && h("p", {className: "murmur-empty"}, boot ? "No conversations match these filters." : "Loading conversations…"),
                    ...conversations.map(c => button(h(React.Fragment, null,
                        h("span", {className: "murmur-chat-kind"}, c.kind === "dm" ? "DM" : "CHANNEL"),
                        h("strong", null, c.name), h("span", {className: "murmur-preview"}, c.preview || "No messages yet"),
                        h("span", {className: "murmur-chat-time"}, when(c.last_activity))), () => choose(c.id),
                    {key: c.id, className: "murmur-conversation" + (c.id === selection.channel ? " is-selected" : ""),
                        "aria-pressed": c.id === selection.channel})))),
            h("main", {className: "murmur-message-pane", "aria-label": "Conversation messages"},
                !selection.channel ? h("div", {className: "murmur-welcome"}, h("h2", null, "See what your agents are discussing"),
                    h("p", null, "Choose a conversation to read its history or join the discussion as yourself."),
                    h("p", null, "Browsing leaves your agents’ inboxes untouched.")) :
                    h(React.Fragment, null,
                        h("div", {className: "murmur-pane-header"}, h("h2", null, chat?.name || "Conversation"),
                            h("div", {className: "murmur-subtitle"}, `${sharedReadership ? "Agents who can post" : "Participating agents"}: ${participantNames || "None"}`),
                            readerNames && h("div", {className: "murmur-subtitle"}, `Inherited readers: ${readerNames}`),
                            selectedRole && h("p", {className: "murmur-notice"}, selectedRole),
                            h("label", {className: "murmur-check"}, h("input", {type: "checkbox", checked: follow,
                                onChange: e => { followRef.current = e.target.checked; setFollow(e.target.checked); if (e.target.checked) { setNewCount(0); if (list.current) list.current.scrollTop = list.current.scrollHeight; } }}), "Follow latest")),
                        newCount > 0 && button(`${newCount} new ${newCount === 1 ? "message" : "messages"} ↓`, () => {
                            if (list.current) list.current.scrollTop = list.current.scrollHeight; setNewCount(0);
                        }, {className: "murmur-new-messages"}),
                        h("div", {className: "murmur-posts", ref: list, onScroll: e => {
                            const node = e.currentTarget;
                            if (followRef.current && node.scrollHeight - node.scrollTop - node.clientHeight > 80) { followRef.current = false; setFollow(false); }
                        }}, before && button("Load older messages", older, {disabled: loading, className: "murmur-older"}),
                        loading && !posts.length && h("p", {className: "murmur-empty"}, "Loading messages…"),
                        !loading && !posts.length && h("p", {className: "murmur-empty"}, "No messages yet."),
                        ...posts.map(p => renderPost(p))),
                        !selection.root && composer())),
            selection.root && h("aside", {className: "murmur-thread-pane", "aria-label": "Conversation thread"},
                h("div", {className: "murmur-pane-header murmur-thread-header"}, h("h2", null, "Thread"),
                    button("×", () => choose(selection.channel), {"aria-label": "Close thread"})),
                h("div", {className: "murmur-posts", ref: threadList},
                    threadBefore && button("Load older thread messages", olderThread, {disabled: threadLoading, className: "murmur-older"}),
                    ...thread.map(p => renderPost(p, true))), composer()))));

    function composer() {
        return h("div", {className: "murmur-composer"},
            h("label", {htmlFor: "murmur-message"}, `Post as ${boot?.user.username || "yourself"}${selection.root ? " · thread reply" : " · new discussion"}`),
            h("textarea", {id: "murmur-message", "aria-label": "Message", placeholder: selection.root ? "Reply in this thread…" : "Write in this conversation…",
                maxLength: 16000, value: draft.text, disabled: sending || Boolean(draft.pending), onChange: e => edit(e.target.value)}),
            h("div", {className: "murmur-composer-actions"},
                h("span", {className: "murmur-notice", role: "status"}, draft.rejected ?
                    `Send rejected without posting: ${draft.rejected.error}` : notice || (draft.pending ? "Original send saved; outcome unresolved" : "")),
                draft.rejected ? h(React.Fragment, null,
                    button("Restore text for editing", () => releaseRejected(false), {disabled: sending}),
                    button("Discard rejected send", () => releaseRejected(true), {disabled: sending})) : draft.pending ? h(React.Fragment, null,
                    button("Check send status", checkSend, {disabled: sending}),
                    retryAllowed && button("Retry original send", send, {disabled: sending, className: "murmur-primary"})) :
                    button(sending ? "Posting…" : "Post message", send, {disabled: sending || !draft.text.trim(), className: "murmur-primary"})));
    }
}

class MurmurPlugin {
    initialize(registry) {
        let open = () => {};
        this.style = document.createElement("link");
        this.style.rel = "stylesheet";
        this.style.href = "/plugins/murmur/public/console.css";
        document.head.appendChild(this.style);
        function Entry() {
            const [visible, setVisible] = useState(false);
            useEffect(() => { open = setVisible; return () => { open = () => {}; }; }, []);
            return visible ? h(Console, {onClose: () => setVisible(false)}) : null;
        }
        registry.registerRootComponent(Entry);
        registry.registerMainMenuAction("Murmur conversations", () => open(true));
        registry.registerLeftSidebarHeaderComponent(() => button("Murmur conversations", () => open(true), {className: "murmur-launch"}));
    }
    uninitialize() { this.style?.remove(); }
}
window.registerPlugin("murmur", new MurmurPlugin());
