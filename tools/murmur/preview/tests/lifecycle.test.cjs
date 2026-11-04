const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const api = require('../api.js');

const id = char => char.repeat(26);
const channelA = id('a'), channelB = id('b'), rootId = id('r'), userId = id('u');
const privatePreview = 'PRIVATE:directory-preview', privateRoot = 'PRIVATE:thread-A';
const workspace = {
    user: {id:userId, username:'human'}, agents:[],
    conversations: [channelA, channelB].map((id, index) => ({id, name:`Room ${index}`, kind:'channel', preview:privatePreview})),
    capabilities: {root_board:true, exact_context:true, full_history_search:true},
};
const post = (channel, text = privateRoot) => ({id:rootId, channel_id:channel, user_id:userId,
    root_id:'', author:'Agent', message:text, create_at:10, reply_count:0});
const threadPage = () => ({root:post(channelA), posts:[], next_before:null});
const boardPath = channel => `conversations/${channel}/board?limit=50`;
const threadPath = `threads/${rootId}?limit=50`;
const turn = () => new Promise(resolve => setImmediate(resolve));

// Only DOM construction/events used by the reader, without layout or snapshots.
// Run app.js unchanged: requests, auth callbacks and route/lifecycle listeners are real.
class Element {
    constructor(tag = '', text = '') { this.tag=tag; this.text=text; this.children=[]; this.attributes={}; this.listeners={}; }
    append(...children) { for (let child of children) { child=child instanceof Element ? child : new Element('', String(child)); child.parent=this; this.children.push(child); } }
    prepend(...children) { const previous=this.children; this.children=[]; this.append(...children, ...previous); }
    replaceChildren(...children) { this.children=[]; this.append(...children); }
    remove() { if (this.parent) this.parent.children=this.parent.children.filter(child => child!==this); }
    setAttribute(key, value) { this.attributes[key]=String(value); }
    addEventListener(name, listener) { this.listeners[name]=listener; }
    get id() { return this.attributes.id; }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
    set textContent(value) { this.text=String(value); this.children=[]; }
    get value() { return this.inputValue ?? (this.tag==='select' ? this.children.find(child => child.selected)?.attributes.value || this.children[0]?.attributes.value : '') ?? ''; }
    set value(value) { this.inputValue=String(value); }
    all() { return [this, ...this.children.flatMap(child => child.all())]; }
    scrollIntoView() {}
    focus(options) {this.focused=true;this.focusOptions=options;}
    click() {this.clicked=true;}
    getBoundingClientRect() {return {top:0};}
    get clientHeight() {return 800;}
    get offsetHeight() {return 100;}
}

function harness(t, {compose = false, initialRoute = {view:'conversations'}} = {}) {
    const mount = new Element('div'), windowEvents = {}, documentEvents = {}, calls = [], created = [], blobs = new Map(), revoked = [];
    const document = {
        visibilityState:'visible', cookie:'MMCSRF=fixture',
        createElement: tag => {const node=new Element(tag);created.push(node);return node;}, createElementNS: (_,tag) => new Element(tag), createTextNode: text => new Element('', text),
        getElementById: id => id==='app' ? mount : mount.all().find(node => node.id===id),
        addEventListener: (name, listener) => {documentEvents[name]=listener;},
    };
    let hash='#'+new URLSearchParams(initialRoute);
    const location = {get hash() {return hash;}, set hash(value) {hash=value.startsWith('#') ? value : '#'+value;}, pathname:'/reader', origin:'http://fixture.invalid'};
    const window = {
        document, AbortController, setTimeout, clearTimeout, scrollY:0, scrollTo(x, y) {this.scrollY=y;}, scrollBy() {},
        URL:{createObjectURL(blob){const url='blob:fixture/'+(created.length+blobs.size);blobs.set(url,blob);return url;},revokeObjectURL(url){revoked.push(url);blobs.delete(url);}},
        addEventListener: (name, listener) => {windowEvents[name]=listener;},
        // Deliberately ignore abort so late-result rejection must work independently.
        fetch: (url, options) => new Promise(resolve => {
            calls.push({route:url.replace('/plugins/murmur/operator/v1/', ''), options, resolve, settled:false});
        }),
    };
    if (compose) {
        const values=new Map(), locks=new Map(); let operation=0;
        window.innerWidth=1024;
        window.localStorage={get length() {return values.size;}, key:index => [...values.keys()][index] ?? null,
            getItem:key => values.get(key) ?? null, setItem:(key, value) => values.set(key, value)};
        window.crypto={randomUUID:() => `lifecycle-operation-${++operation}`};
        window.navigator={locks:{request(key, action) {
            const result=(locks.get(key) || Promise.resolve()).then(action);
            locks.set(key, result.catch(() => {})); return result;
        }}};
    }
    function reply(route, data, status = 200, cache = 'private, no-store') {
        const call = calls.find(call => matchesRoute(call.route, route) && !call.settled);
        assert.ok(call, `expected pending ${route}`);
        call.settled=true;
        call.resolve({ok:status<400, status, headers:new Headers({'Cache-Control':cache}), json:async () => data, blob:async()=>data});
        return call;
    }
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8'), {
        MurmurReader:api, window, document, location, Node:Element, URL, URLSearchParams,
        navigator:{}, ...(compose ? {MurmurCompose:require('../compose.js')} : {}), requestAnimationFrame: callback => callback(),
    }, {filename:'app.js'});
    t.after(async () => {
        windowEvents.pagehide();
        for (const call of calls.filter(call => !call.settled)) {
            call.settled=true; call.resolve({ok:false, status:401});
        }
        await turn();
    });
    return {
        mount, calls, reply, host:window, created, blobs, revoked,
        text: () => mount.textContent,
        count: route => calls.filter(call => matchesRoute(call.route, route)).length,
        find(predicate) {const node=mount.all().find(predicate); assert.ok(node, 'expected visible control'); return node;},
        hash: () => location.hash,
        scroll(value) {const surface=document.getElementById('message-scroll') || document.getElementById('main');if(value!==undefined)surface.scrollTop=value;return surface.scrollTop||0;},
        async event(node, name = 'click') {
            assert.ok(node.listeners[name], `expected ${name} listener`);
            const previous=location.hash;
            node.listeners[name]({button:0, preventDefault() {}});
            if (location.hash!==previous) windowEvents.hashchange();
            await turn();
        },
        async search(query, scope) {
            const launch=mount.all().find(node=>node.className==='search-launch');
            if(launch) await this.event(launch);
            this.find(node => node.attributes['aria-label']==='Search Murmur').value=query;
            if (scope) this.find(node => node.attributes['aria-label']==='Search scope').value=scope;
            await this.event(this.find(node => node.tag==='form'), 'submit');
        },
        async start(data = workspace) { reply('bootstrap', data); await turn(); },
        async navigate(value) { location.hash='#'+new URLSearchParams(value); windowEvents.hashchange(); await turn(); },
        hide(mode) {
            if (mode==='visibility') {document.visibilityState='hidden'; documentEvents.visibilitychange();}
            else windowEvents.pagehide({persisted:true});
        },
        resume(mode) {
            if (mode==='visibility') {document.visibilityState='visible'; documentEvents.visibilitychange();}
            else windowEvents.pageshow({persisted:true});
        },
    };
}

function matchesRoute(actual, expected) {return typeof expected==='function' ? expected(actual) : actual===expected;}
function searchRoute(query, filters = {}) {
    return route => {
        if (!route.startsWith('search?')) return false;
        const params=new URLSearchParams(route.slice(7));
        return params.get('q')===query && Object.entries(filters).every(([key, value]) => (params.get(key) || '')===value);
    };
}
const buttonNamed = (h, label) => h.find(node => node.tag==='button' && (label==='Undo handle'?node.textContent.startsWith('Undo · '):node.textContent===label));
const searchHit = (h, text) => h.find(node => node.tag==='a' && node.className?.split(' ').includes('search-hit') && node.textContent.includes(text));
const personalState = revision => ({version:1, revision, favorites:[], follows:[]});
const attentionItem = (post, revision = 1) => ({post_id:post.id, channel_id:post.channel_id, root_id:post.root_id || post.id, revision, reasons:['mention'], post});
const inboxPage = items => ({items, total:items.length, next_before:null, complete:true});
const inboxPath = 'workspace/inbox?limit=50';
async function startPersonal(h, saved = []) {
    await h.start({...workspace, capabilities:{...workspace.capabilities, personal_workspace:true}});
    h.reply('workspace', personalState(1)); await turn();
    h.reply('workspace/saved', {revision:1, items:saved}); await turn();
}
const handleButton = (h, message) => h.find(node => node.tag==='article' && node.className?.includes('inbox-card') && node.textContent.includes(message)).all()
    .find(node => node.tag==='button' && node.textContent==='Mark handled');

test('late session failures conceal private content after the requesting route is abandoned', async t => {
    for (const status of [401,403,409,426]) await t.test(String(status), async t => {
        const h = harness(t); await h.start();
        await h.navigate({view:'conversation', conversation:channelA});
        await h.navigate({view:'conversations'});
        h.reply('bootstrap',workspace);await turn();
        assert.ok(h.text().includes(privatePreview), 'directory is visible before the denial');
        if (status===426) h.reply(boardPath(channelA), {}, 200, 'public, max-age=300');
        else h.reply(boardPath(channelA), {}, status);
        await turn();
        assert.equal(h.text().includes(privatePreview), false, 'an abandoned route must still invalidate the visible session');
        assert.ok(h.text().includes('Workspace locked'));
        await h.navigate({view:'conversations'});
        assert.equal(h.text().includes(privatePreview), false, 'navigation cannot reopen the invalidated workspace');
    });
});

test('a cached thread is reused only within its canonical conversation', async t => {
    const h = harness(t); await h.start();
    await h.navigate({view:'thread', conversation:channelA, thread:rootId});
    h.reply(threadPath, threadPage()); await turn();
    assert.ok(h.text().includes(privateRoot));
    await h.navigate({view:'conversations'});
    await h.navigate({view:'thread', conversation:channelA, thread:rootId});
    assert.equal(h.count(threadPath), 1, 'same conversation can reuse the loaded thread');
    assert.ok(h.text().includes(privateRoot));
    await h.navigate({view:'thread', conversation:channelB, thread:rootId});
    assert.equal(h.count(threadPath), 2, 'another channel must not reuse the cached thread');
    assert.equal(h.text().includes(privateRoot), false);
    h.reply(threadPath, threadPage()); await turn();
    assert.equal(h.text().includes(privateRoot), false, 'canonical channel mismatch cannot render the message');
    assert.ok(h.mount.all().some(node => node.attributes.role==='alert'));
});

for (const mode of ['pagehide', 'visibility']) test(`${mode} conceals immediately and resume requires fresh auth and content`, async t => {
    const h = harness(t); await h.start();
    await h.navigate({view:'thread', conversation:channelA, thread:rootId});
    h.reply(threadPath, threadPage()); await turn();
    assert.ok(h.text().includes(privateRoot));
    await h.navigate({view:'conversation', conversation:channelB});
    const pending = h.calls.find(call => call.route===boardPath(channelB));
    h.hide(mode);
    assert.equal(h.text().includes('PRIVATE:'), false, 'concealment is synchronous');
    assert.equal(pending.options.signal.aborted, true);
    h.resume(mode);
    assert.equal(h.count('bootstrap'), 2, 'resume must revalidate the human session');
    assert.equal(h.text().includes('PRIVATE:'), false, 'no private content before revalidation');
    h.reply(boardPath(channelB), {posts:[post(channelB, 'PRIVATE:stale-result')], next_before:null});
    await turn();
    assert.equal(h.text().includes('PRIVATE:'), false, 'late result cannot undo concealment');
    h.reply('bootstrap', workspace); await turn();
    assert.equal(h.count(boardPath(channelB)), 2, 'resume must reread the current board');
    assert.equal(h.text().includes('PRIVATE:'), false, 'old result cannot supply the resumed board');
    h.reply(boardPath(channelB), {posts:[post(channelB, 'PRIVATE:fresh-result')], next_before:null});
    await turn();
    assert.ok(h.text().includes('PRIVATE:fresh-result'));
    assert.equal(h.text().includes('PRIVATE:stale-result'), false);
    await h.navigate({view:'thread', conversation:channelA, thread:rootId});
    assert.equal(h.count(threadPath), 2, 'suspension must discard the previously cached thread');
    assert.equal(h.text().includes(privateRoot), false);
    h.reply(threadPath, threadPage()); await turn();
    assert.ok(h.text().includes(privateRoot));
});

test('a newer query and scope win over a late search response', async t => {
    const h=harness(t); await h.start();
    await h.navigate({view:'conversation', conversation:channelA});
    h.reply(boardPath(channelA), {posts:[], next_before:null}); await turn();
    await h.search('old phrase');
    assert.equal(h.count(searchRoute('old phrase', {conversation:channelA, root:''})), 1);
    h.find(node => node.attributes['aria-label']==='Search Murmur').value='new phrase';
    const scope=h.find(node => node.attributes['aria-label']==='Search scope'); scope.value='everywhere';
    await h.event(scope, 'change');
    h.reply(searchRoute('new phrase', {conversation:'', root:''}), {posts:[post(channelB, 'new phrase: current match')], next_before:null});
    await turn();
    assert.ok(h.text().includes('new phrase: current match'));
    h.reply(searchRoute('old phrase'), {posts:[post(channelA, 'old phrase: stale match')], next_before:null});
    await turn();
    assert.ok(h.text().includes('new phrase: current match'));
    assert.equal(h.text().includes('old phrase: stale match'), false);
    assert.equal(h.find(node => node.attributes['aria-label']==='Search Murmur').value, 'new phrase');
    assert.equal(h.find(node => node.attributes['aria-label']==='Search scope').value, 'everywhere');
    assert.equal(h.hash().includes('phrase'), false, 'query text stays out of route URLs');
});

test('directory widening and nested contextual search preserve the earlier paginated results', async t => {
    const h=harness(t); await h.start();
    await h.search('Room');
    assert.equal(h.count(searchRoute('Room')), 0, 'directory defaults must not silently search messages');
    await h.event(buttonNamed(h, 'Search messages everywhere →'));
    const first=post(channelA, 'Room: first historical root');
    h.reply(searchRoute('Room', {before:'', conversation:'', root:''}), {posts:[first], next_before:'opaque-bound-cursor'});
    await turn();
    assert.equal(h.find(node => node.attributes['aria-label']==='Search Murmur').value, 'Room');
    await h.event(buttonNamed(h, 'Load more matching messages'));
    const reply={...post(channelA, 'Room: second-page reply'), id:id('s'), root_id:rootId};
    h.reply(searchRoute('Room', {before:'opaque-bound-cursor'}), {posts:[reply], next_before:null}); await turn();
    assert.ok(h.text().includes(first.message)); assert.ok(h.text().includes(reply.message));
    h.scroll(640);
    await h.event(searchHit(h, reply.message));
    let target=new URLSearchParams(h.hash().slice(1));
    assert.equal(target.get('view'), 'thread'); assert.equal(target.get('thread'), rootId); assert.equal(target.get('post'), reply.id);
    h.reply(threadPath, {root:first, posts:[reply], next_before:null}); await turn();
    assert.ok(h.find(node => node.className==='search-launch').attributes['aria-label'].includes('This thread'));
    await h.search('nested phrase');
    h.reply(searchRoute('nested phrase', {conversation:channelA, root:rootId}), {posts:[], next_before:null}); await turn();
    await h.event(buttonNamed(h, '← Close search'));
    target=new URLSearchParams(h.hash().slice(1));
    assert.equal(target.get('view'), 'thread'); assert.equal(target.get('post'), reply.id);
    await h.event(h.find(node => node.tag==='a' && node.textContent==='← Return to search'));
    assert.equal(h.find(node => node.attributes['aria-label']==='Search Murmur').value, 'Room');
    assert.equal(h.find(node => node.attributes['aria-label']==='Search scope').value, 'everywhere');
    assert.ok(h.text().includes(first.message)); assert.ok(h.text().includes(reply.message));
    assert.equal(h.count(searchRoute('Room')), 2, 'return retains both result pages without refetching');
    assert.equal(h.scroll(), 640, 'return restores the earlier result position');
    assert.equal(h.hash().includes('Room'), false); assert.equal(h.hash().includes('nested'), false);
    await h.event(searchHit(h, first.message));
    target=new URLSearchParams(h.hash().slice(1));
    assert.equal(target.get('view'), 'conversation'); assert.equal(target.get('post'), first.id);
    h.reply(boardPath(channelA), {posts:[first], next_before:null}); await turn();
});

test('suspension discards search pages and a late pagination response cannot repopulate them', async t => {
    const h=harness(t); await h.start(); await h.search('private phrase', 'everywhere');
    h.reply(searchRoute('private phrase'), {posts:[post(channelA, 'PRIVATE:old-search-match')], next_before:'old-search-cursor'});
    await turn(); assert.ok(h.text().includes('PRIVATE:old-search-match'));
    await h.event(buttonNamed(h, 'Load more matching messages'));
    h.hide('pagehide'); assert.equal(h.text().includes('PRIVATE:'), false);
    h.resume('pagehide'); assert.equal(h.text().includes('PRIVATE:'), false);
    h.reply('bootstrap', workspace); await turn();
    assert.equal(h.count(searchRoute('private phrase', {before:''})), 2, 'resume rereads the first page rather than reusing private search results');
    assert.equal(h.text().includes('PRIVATE:'), false);
    h.reply(searchRoute('private phrase', {before:''}), {posts:[post(channelA, 'PRIVATE:fresh-search-match')], next_before:null});
    await turn(); assert.ok(h.text().includes('PRIVATE:fresh-search-match'));
    h.reply(searchRoute('private phrase', {before:'old-search-cursor'}), {posts:[{...post(channelA, 'PRIVATE:late-search-match'), id:id('t')}], next_before:null});
    await turn();
    assert.ok(h.text().includes('PRIVATE:fresh-search-match'));
    assert.equal(h.text().includes('PRIVATE:old-search-match'), false);
    assert.equal(h.text().includes('PRIVATE:late-search-match'), false);
});

test('a send waiting for autosave cannot dispatch after suspend and same-user resume', async t => {
    const h=harness(t, {compose:true}); await h.start();
    await h.navigate({view:'conversation', conversation:channelA});
    h.reply(boardPath(channelA), {posts:[], next_before:null}); await turn();
    let release;
    const held=new Promise(resolve => {release=resolve;});
    const blocker=h.host.navigator.locks.request(`murmur.operator.${userId}.${channelA}:`, () => held);
    const input=h.find(node => node.tag==='textarea'); input.value='Instruction still saving';
    await h.event(input, 'input');
    await h.event(buttonNamed(h, 'Post message'));
    assert.equal(h.count('posts'), 0, 'the send is waiting for the persisted draft');
    h.hide('pagehide'); h.resume('pagehide');
    h.reply('bootstrap', workspace); await turn();
    h.reply(boardPath(channelA), {posts:[], next_before:null}); await turn();
    release(); await blocker; await turn();
    assert.equal(h.count('posts'), 0, 'resuming must require a new deliberate send action');
});

test('an unclassifiable mutation409 cannot leave private messages visible', async t => {
    const h=harness(t, {compose:true}); await h.start();
    await h.navigate({view:'conversation', conversation:channelA});
    h.reply(boardPath(channelA), {posts:[post(channelA)], next_before:null}); await turn();
    assert.ok(h.text().includes(privateRoot));
    const input=h.find(node => node.tag==='textarea'); input.value='Preserve this uncertain send';
    await h.event(input, 'input'); await h.event(buttonNamed(h, 'Post message'));
    const sending=h.calls.find(call => call.route==='posts' && !call.settled);
    assert.ok(sending); sending.settled=true;
    sending.resolve({ok:false, status:409, json:async () => {throw Error('Truncated response');}});
    await turn();
    assert.equal(h.text().includes(privateRoot), false, 'unknown409 must not be presumed to be a harmless workspace conflict');
    assert.ok(h.text().includes('Workspace locked'));
    const held=JSON.parse(h.host.localStorage.getItem(`murmur.operator.${userId}.${channelA}:`));
    assert.equal(held.pending.message, 'Preserve this uncertain send', 'concealing the workspace must retain the original operation');
});

test('personal reads leave Inbox untouched; Handle and Undo target one exact item', async t => {
    const h=harness(t); await startPersonal(h);
    const a=attentionItem(post(channelA, 'First incoming message'));
    const b=attentionItem({...post(channelA, 'Second incoming message'), id:id('s')});
    await h.navigate({view:'inbox'}); h.reply(inboxPath, inboxPage([a,b])); await turn();
    assert.ok(h.text().includes(a.post.message)); assert.ok(h.text().includes(b.post.message));
    assert.ok(h.calls.every(call => call.options.method==='GET'), 'boot and Inbox viewing must not mutate state');
    await h.event(handleButton(h, a.post.message));
    const handled=h.reply(`workspace/inbox/${a.post_id}/handle`, {undo_token:'undo-first'});
    assert.deepEqual(JSON.parse(handled.options.body), {revision:a.revision}); await turn();
    h.reply('workspace', personalState(2)); await turn();
    h.reply(inboxPath, inboxPage([b])); await turn();
    assert.equal(h.text().includes(a.post.message), false); assert.ok(h.text().includes(b.post.message));
    await h.event(buttonNamed(h, 'Undo handle'));
    const undone=h.reply(`workspace/inbox/${a.post_id}/undo`, {});
    assert.deepEqual(JSON.parse(undone.options.body), {token:'undo-first'}); await turn();
    h.reply(inboxPath, inboxPage([a,b])); await turn();
    assert.ok(h.mount.all().some(node => node.tag==='a' && node.textContent==='Inbox2'), 'Undo refreshes the navigation count immediately, without waiting for the poll');
    h.reply(inboxPath, inboxPage([a,b])); await turn();
    assert.ok(h.text().includes(a.post.message)); assert.ok(h.text().includes(b.post.message));
    assert.deepEqual(h.calls.filter(call => call.options.method!=='GET').map(call => call.route),
        [`workspace/inbox/${a.post_id}/handle`, `workspace/inbox/${a.post_id}/undo`]);
});

test('suspension conceals private saved notes and ignores a late save response', async t => {
    const h=harness(t), saved={post_id:rootId, post:post(channelA), note:'PRIVATE:original-note'};
    await startPersonal(h, [saved]); await h.navigate({view:'saved'});
    const note=h.find(node => node.tag==='textarea' && node.attributes['aria-label']==='Private note');
    assert.equal(note.value, saved.note);
    assert.ok(h.calls.every(call => call.options.method==='GET'));
    note.value='PRIVATE:edited-note'; await h.event(buttonNamed(h, 'Save note'));
    h.reply('workspace', personalState(2)); await turn();
    h.hide('pagehide'); assert.equal(h.text().includes('PRIVATE:'), false);
    assert.equal(h.mount.all().some(node => node.value?.includes('PRIVATE:')), false, 'private inputs are removed with the workspace');
    h.reply(`workspace/saved/${rootId}`, {saved:true}); await turn();
    assert.equal(h.text().includes('PRIVATE:'), false);
    h.resume('pagehide'); h.reply('bootstrap', {...workspace, capabilities:{...workspace.capabilities, personal_workspace:true}}); await turn();
    assert.equal(h.text().includes('PRIVATE:'), false);
    h.reply('workspace', personalState(2)); await turn();
    h.reply('workspace/saved', {revision:2, items:[{...saved,note:'PRIVATE:fresh-note'}]}); await turn();
    assert.equal(h.find(node => node.tag==='textarea' && node.attributes['aria-label']==='Private note').value, 'PRIVATE:fresh-note');
    assert.equal(h.calls.filter(call => call.options.method!=='GET').length, 1, 'resume must not replay the save');
});

test('a delayed Undo completion cannot discard a newer message handling action', async t => {
    const h=harness(t); await startPersonal(h);
    const a=attentionItem(post(channelA, 'Handle first'));
    const b=attentionItem({...post(channelA, 'Handle second'), id:id('s')});
    await h.navigate({view:'inbox'}); h.reply(inboxPath, inboxPage([a,b])); await turn();
    await h.event(handleButton(h, a.post.message));
    h.reply(`workspace/inbox/${a.post_id}/handle`, {undo_token:'undo-first'}); await turn();
    h.reply('workspace', personalState(2)); await turn(); h.reply(inboxPath, inboxPage([b])); await turn();
    await h.event(buttonNamed(h, 'Undo handle'));
    await h.event(handleButton(h, b.post.message));
    h.reply(`workspace/inbox/${b.post_id}/handle`, {undo_token:'undo-second'}); await turn();
    h.reply('workspace', personalState(3)); await turn(); h.reply(inboxPath, inboxPage([])); await turn();
    h.reply(`workspace/inbox/${a.post_id}/undo`, {}); await turn(); h.reply(inboxPath, inboxPage([a])); await turn();
    h.reply(inboxPath, inboxPage([a])); await turn();
    const newerUndo=h.mount.all().find(node => node.tag==='button' && node.textContent.startsWith('Undo · '));
    assert.ok(newerUndo, 'finishing Undo A must retain the Undo action for later Handle B');
    await h.event(newerUndo);
    const undone=h.reply(`workspace/inbox/${b.post_id}/undo`, {});
    assert.deepEqual(JSON.parse(undone.options.body), {token:'undo-second'});
    await turn(); h.reply(inboxPath, inboxPage([a,b])); await turn();
    h.reply(inboxPath, inboxPage([a,b])); await turn();
});

test('returning to cached Inbox search retains its incomplete-history warning', async t => {
    const h=harness(t); await startPersonal(h);
    const item=attentionItem(post(channelA, 'Known historical match'));
    const partial={...inboxPage([item]), complete:false, coverage_warning:'Some incoming history could not be reconstructed.'};
    await h.navigate({view:'inbox'}); h.reply(inboxPath, partial); await turn();
    await h.search('Known historical');
    const queryRoute=route => route.startsWith('workspace/inbox?') && new URLSearchParams(route.split('?')[1]).get('q')==='Known historical';
    h.reply(queryRoute, partial); await turn();
    assert.ok(h.text().includes(partial.coverage_warning));
    await h.event(searchHit(h, item.post.message));
    h.reply(boardPath(channelA), {posts:[item.post], next_before:null}); await turn();
    await h.event(h.find(node => node.tag==='a' && node.textContent==='← Return to search'));
    assert.ok(h.text().includes(item.post.message));
    assert.equal(h.count(queryRoute), 1, 'the earlier search uses its retained results');
    assert.ok(h.text().includes(partial.coverage_warning), 'cached results must retain their coverage qualification');
});

test('personal action after posting refreshes the capture revision and retains its explicit intent', async t => {
    const h=harness(t, {compose:true}); await startPersonal(h);
    await h.navigate({view:'conversation', conversation:channelA});
    h.reply(boardPath(channelA), {posts:[], next_before:null}); await turn();
    const input=h.find(node => node.tag==='textarea'); input.value='A new human request';
    await h.event(input, 'input'); await h.event(buttonNamed(h, 'Post message'));
    const created=post(channelA, input.value);
    h.reply('posts', {post:created}); await turn();
    // Deliver the browser hashchange caused by the asynchronous send response.
    await h.navigate(Object.fromEntries(new URLSearchParams(h.hash().slice(1))));
    h.reply(boardPath(channelA), {posts:[created], next_before:null}); await turn();
    await h.event(buttonNamed(h, 'Favorite'));
    assert.equal(h.count(`workspace/favorites/${channelA}`), 0, 'mutation waits for a fresh workspace revision');
    // Capture or another window advanced revision and already favorited the same board.
    // The user's explicit Add must remain Add rather than toggling the new state.
    h.reply('workspace', {...personalState(9), favorites:[channelA]}); await turn();
    const changed=h.reply(`workspace/favorites/${channelA}`, {});
    assert.deepEqual(JSON.parse(changed.options.body), {enabled:true, revision:9}); await turn();
    h.reply('workspace', {...personalState(10), favorites:[channelA]}); await turn();
    h.reply('workspace/saved', {revision:10, items:[]}); await turn();
    assert.ok(h.mount.all().some(node => node.tag==='button' && node.textContent==='Favorited'));
    assert.equal(h.count(`workspace/favorites/${channelA}`), 1, 'the explicit action dispatches once');
});

test('suspension during personal-action preflight prevents mutation after same-user resume', async t => {
    const h=harness(t); await startPersonal(h);
    await h.navigate({view:'conversation', conversation:channelA});
    h.reply(boardPath(channelA), {posts:[post(channelA)], next_before:null}); await turn();
    await h.event(buttonNamed(h, 'Save for later'));
    assert.equal(h.count(`workspace/saved/${rootId}`), 0);
    h.hide('pagehide'); h.resume('pagehide');
    h.reply('workspace', personalState(2)); await turn();
    assert.equal(h.calls.filter(call => call.options.method!=='GET').length, 0);
    h.reply('bootstrap', {...workspace, capabilities:{...workspace.capabilities, personal_workspace:true}}); await turn();
    h.reply('workspace', personalState(3)); await turn();
    h.reply('workspace/saved', {revision:3, items:[]}); await turn();
    h.reply(boardPath(channelA), {posts:[post(channelA)], next_before:null}); await turn();
    assert.equal(h.calls.filter(call => call.options.method!=='GET').length, 0, 'reauthentication must not replay the interrupted action');
});

test('a newly arrived Inbox root refreshes a cached board and retains canonical order', async t => {
    const h=harness(t); await startPersonal(h);
    const earlier=post(channelA, 'Earlier board message');
    const incoming={...post(channelA, 'Newly arrived message'), id:id('s'), create_at:20};
    await h.navigate({view:'conversation', conversation:channelA});
    h.reply(boardPath(channelA), {posts:[earlier], next_before:null}); await turn();
    await h.navigate({view:'inbox'}); h.reply(inboxPath, inboxPage([attentionItem(incoming)])); await turn();
    await h.event(h.find(node => node.tag==='a' && node.textContent.includes(incoming.message)));
    assert.equal(h.count(boardPath(channelA)), 2, 'the new arrival must refresh the board cached before it existed');
    h.reply(boardPath(channelA), {posts:[earlier,incoming], next_before:null}); await turn();
    const messages=h.mount.all().filter(node => node.tag==='article' && node.className?.split(' ').includes('message'));
    assert.equal(messages.length, 2);
    assert.ok(messages[0].textContent.includes(earlier.message));
    assert.ok(messages[1].textContent.includes(incoming.message));
    assert.equal(h.count(`context/${incoming.id}`), 0, 'a current-page arrival does not require a separate out-of-page gap');
});

test('thread parent return restores the board scroll surface instead of the document', async t => {
    const h=harness(t); await h.start();
    await h.navigate({view:'conversation',conversation:channelA});
    h.reply(boardPath(channelA),{posts:[post(channelA)],next_before:null});await turn();
    h.scroll(420);
    await h.event(h.find(node=>node.tag==='a' && node.textContent==='Open thread →'));
    h.reply(threadPath,threadPage());await turn();
    await h.event(h.find(node=>node.tag==='a' && node.className?.includes('board-back')));
    assert.equal(new URLSearchParams(h.hash().slice(1)).get('view'),'conversation');
    assert.equal(h.scroll(),420,'parent restores the original board pane position');
    assert.equal(h.host.scrollY,0,'the document itself never scrolls');
    for (let cycle=0;cycle<4;cycle++) {
        const position=500+cycle*100;h.scroll(position);
        await h.event(h.find(node=>node.tag==='a' && node.textContent==='Open thread →'));
        const params=new URLSearchParams(h.hash().slice(1));
        assert.equal(params.get('boardOrigin').includes('boardOrigin'),false,'return provenance must not nest on each visit');
        await h.event(h.find(node=>node.tag==='a' && node.className?.includes('board-back')));
        assert.equal(h.scroll(),position,'each repeated return restores its most recent reading position');
    }
});

test('message formatting preserves literal untrusted HTML and only creates safe links', async t => {
    const h=harness(t);await h.start();await h.navigate({view:'conversation',conversation:channelA});
    const text='**Decision**\n\n- first\n- second\n\n<script>alert(1)</script> [unsafe](javascript:alert) [safe](https://example.com)\n\n```js\n<img src=x onerror=alert(1)>\n```';
    h.reply(boardPath(channelA),{posts:[post(channelA,text)],next_before:null});await turn();
    const body=h.find(node=>node.className==='body');
    assert.ok(body.all().some(node=>node.tag==='strong' && node.textContent==='Decision'));
    assert.equal(body.all().filter(node=>node.tag==='li').length,2);
    assert.ok(body.textContent.includes('<script>alert(1)</script>'));
    assert.ok(body.textContent.includes('<img src=x onerror=alert(1)>'));
    assert.equal(body.all().some(node=>['script','img'].includes(node.tag)),false);
    assert.deepEqual(body.all().filter(node=>node.tag==='a').map(node=>node.attributes.href),['https://example.com/']);
});

test('a thread found independently returns to its exact root, not an earlier board position', async t => {
    const h=harness(t);await h.start();
    await h.navigate({view:'conversation',conversation:channelA});
    h.reply(boardPath(channelA),{posts:[post(channelA)],next_before:null});await turn();h.scroll(500);
    await h.search('another topic','everywhere');
    const found={...post(channelA,'another topic'),id:id('z')};
    h.reply(searchRoute('another topic'),{posts:[found],next_before:null});await turn();
    await h.navigate({view:'thread',conversation:channelA,thread:found.id,post:found.id,from:'1'});
    h.reply(`threads/${found.id}?limit=50`,{root:found,posts:[],next_before:null});await turn();
    const parent=h.find(node=>node.tag==='a' && node.className?.includes('board-back'));
    const target=new URLSearchParams(parent.attributes.href.slice(1));
    assert.equal(target.get('post'),found.id);
    assert.equal(target.has('boardReturn'),false,'independent thread must not inherit a board-return claim');
});

test('Inbox source text is concealed through suspension until freshly authorized context returns', async t => {
    const h=harness(t);await startPersonal(h);
    const incoming=attentionItem({...post(channelA,'Private source before suspension'),id:id('s'),root_id:rootId});
    await h.navigate({view:'inbox'});h.reply(inboxPath,inboxPage([incoming]));await turn();
    await h.event(h.find(node=>node.tag==='a' && node.className==='inbox-message'));
    h.reply(threadPath,{root:post(channelA),posts:[incoming.post],next_before:null});await turn();
    assert.ok(h.text().includes(incoming.post.message));
    h.hide('page');assert.equal(h.text().includes(incoming.post.message),false);h.resume('page');
    h.reply('bootstrap',{...workspace,capabilities:{...workspace.capabilities,personal_workspace:true}});await turn();
    h.reply('workspace',personalState(2));await turn();h.reply('workspace/saved',{revision:2,items:[]});await turn();
    assert.equal(h.text().includes(incoming.post.message),false,'source text must not render while authorized context is pending');
    h.reply(inboxPath,inboxPage([]));await turn();
    assert.equal(h.text().includes(incoming.post.message),false);
    h.reply(`context/${incoming.post_id}`,{},404);await turn();
    h.reply(threadPath,{root:post(channelA),posts:[],next_before:null});await turn();
    h.reply(`context/${incoming.post_id}`,{},404);await turn();
    assert.equal(h.text().includes(incoming.post.message),false,'unavailable source cannot reappear from the old snapshot');
});

test('fresh Inbox-source entry restores authorized pending controls and labels its new source snapshot', async t => {
    const incoming=attentionItem({...post(channelA,'Fresh reply after reload'),id:id('s'),root_id:rootId});
    const h=harness(t,{initialRoute:{view:'thread',conversation:channelA,thread:rootId,post:incoming.post_id,source:'inbox'}});
    await startPersonal(h);
    assert.equal(h.text().includes(incoming.post.message),false,'private content waits for fresh Inbox authorization');
    h.reply(inboxPath,inboxPage([incoming]));await turn();
    h.reply(threadPath,{root:post(channelA),posts:[incoming.post],next_before:null});await turn();
    assert.ok(h.text().includes('Current Inbox after reopening'));
    assert.ok(h.text().includes(incoming.post.message));
    assert.ok(buttonNamed(h,'Handle and next'));
    assert.equal(h.calls.filter(call=>call.options.method!=='GET').length,0,'opening the source does not handle anything');
});

test('return to search restores the opened result focus without moving retained scroll', async t => {
    const h=harness(t);await h.start();
    await h.search('focus result','everywhere');
    const found=post(channelA,'focus result');
    h.reply(searchRoute('focus result'),{posts:[found],next_before:null});await turn();
    h.scroll(360);await h.event(searchHit(h,found.message));
    h.reply(boardPath(channelA),{posts:[found],next_before:null});await turn();
    await h.event(h.find(node=>node.tag==='a'&&node.textContent==='← Return to search'));
    const result=searchHit(h,found.message);
    assert.equal(result.focused,true);
    assert.equal(result.focusOptions.preventScroll,true);
    assert.equal(h.scroll(),360);
});

test('directory search results also recover keyboard focus after returning from their board', async t => {
    const h=harness(t);await h.start();await h.search('Room 0','conversations');
    const hit=h.find(node=>node.id==='search-conversation-'+channelA);
    await h.event(hit);h.reply(boardPath(channelA),{posts:[],next_before:null});await turn();
    await h.event(h.find(node=>node.tag==='a'&&node.textContent==='← Return to search'));
    assert.equal(h.find(node=>node.id==='search-conversation-'+channelA).focused,true);
});

test('Saved-scoped search keeps its own return and focus context', async t => {
    const found=post(channelA,'A saved decision');
    const h=harness(t);await startPersonal(h,[{post_id:found.id,note:'release note',post:found}]);
    await h.search('release note','saved');await h.event(searchHit(h,found.message));
    h.reply(boardPath(channelA),{posts:[found],next_before:null});await turn();
    await h.event(h.find(node=>node.tag==='a'&&node.textContent==='← Return to search'));
    assert.equal(searchHit(h,found.message).focused,true);
    assert.equal(h.find(node=>node.attributes['aria-label']==='Search scope').value,'saved');
});

test('Drafts-scoped search returns to its result after resuming a board draft', async t => {
    const h=harness(t,{compose:true});await startPersonal(h);
    await h.navigate({view:'conversation',conversation:channelA});h.reply(boardPath(channelA),{posts:[],next_before:null});await turn();
    const input=h.find(node=>node.tag==='textarea');input.value='Draft needing review';await h.event(input,'input');
    await h.search('Draft needing','drafts');
    await h.event(h.find(node=>node.tag==='a'&&node.textContent==='Resume draft →'));
    assert.equal(h.find(node=>node.tag==='textarea').value,'Draft needing review');
    await h.event(h.find(node=>node.tag==='a'&&node.textContent==='← Return to search'));
    assert.equal(h.find(node=>node.tag==='a'&&node.textContent==='Resume draft →').focused,true);
    assert.equal(h.find(node=>node.attributes['aria-label']==='Search scope').value,'drafts');
});

test('returning to the directory refreshes activity even without a new Inbox item', async t => {
    const h=harness(t);await startPersonal(h);
    await h.navigate({view:'inbox'});h.reply(inboxPath,inboxPage([]));await turn();
    await h.navigate({view:'conversations'});
    assert.equal(h.count('bootstrap'),2,'directory entry refreshes independently of Inbox count');
    h.reply('bootstrap',{...workspace,conversations:workspace.conversations.map(chat=>({...chat,preview:chat.id===channelA?'New incoming preview':chat.preview}))});await turn();
    assert.ok(h.text().includes('New incoming preview'));
});


test('opening an uncached incoming reply refreshes the thread before exact-context fallback', async t => {
    const h=harness(t);await h.start();
    const first=threadPage();
    await h.navigate({view:'thread',conversation:channelA,thread:rootId});
    h.reply(threadPath,first);await turn();
    const arrival={...post(channelA,'New reply in arrival order'),id:id('n'),root_id:rootId,create_at:20};
    await h.navigate({view:'thread',conversation:channelA,thread:rootId,post:arrival.id});
    assert.equal(h.count(threadPath),2);
    h.reply(threadPath,{...first,posts:[arrival]});await turn();
    assert.ok(h.text().includes(arrival.message));
    assert.equal(h.count('context/'+arrival.id),0,'current page resolves the new reply without detached context');
});

const fileId=id('f');
const fileMetadata={id:fileId,name:'notes.pdf',size:3,mime_type:'application/pdf'};
const attachmentWorkspace={...workspace,capabilities:{...workspace.capabilities,attachments:{enabled:true,max_file_size:1024,max_files_per_post:10}}};
const uploadPath=route=>route.startsWith('files?');
const chooseFile=()=>({name:'notes.pdf',size:3,arrayBuffer:async()=>new Uint8Array([1,2,3]).buffer});
async function chooseAttachment(h) {
    h.host.crypto.subtle={digest:async()=>new Uint8Array(32).buffer};
    const picker=h.find(node=>node.attributes['aria-label']==='Choose an attachment');
    picker.files=[chooseFile()];await h.event(picker,'change');
}
test('web controls upload and send file-only roots and replies with frozen IDs', async t => {
    for(const mode of ['conversation','thread'])await t.test(mode,async t=>{
        const h=harness(t,{compose:true});await h.start(attachmentWorkspace);
        await h.navigate({view:mode,conversation:channelA,...(mode==='thread'?{thread:rootId}:{})});
        h.reply(mode==='thread'?threadPath:boardPath(channelA),mode==='thread'?threadPage():{posts:[],next_before:null});await turn();
        await chooseAttachment(h);
        const uploading=h.calls.find(call=>uploadPath(call.route));assert.ok(uploading);
        assert.equal(uploading.options.body.byteLength,3);
        h.reply(uploadPath,{file:fileMetadata,channel_id:channelA});await turn();
        assert.ok(h.text().includes('notes.pdf'));
        await h.event(buttonNamed(h,mode==='thread'?'Post reply':'Post message'));
        const sending=h.calls.find(call=>call.route==='posts'&&!call.settled);assert.ok(sending);
        const body=JSON.parse(sending.options.body);
        assert.equal(body.message,'');assert.equal(body.root_id,mode==='thread'?rootId:'');assert.deepEqual(body.file_ids,[fileId]);
        h.reply('posts',{post:{...post(channelA,''),id:id('n'),root_id:body.root_id,file_ids:[fileId],files:[fileMetadata]}});await turn();
        const draft=JSON.parse(h.host.localStorage.getItem(`murmur.operator.${userId}.${channelA}:${body.root_id}`));
        assert.deepEqual(draft,{text:'',pending:null});
    });
});
test('removing the staged file disables an empty send without a server deletion or post', async t=>{
    const h=harness(t,{compose:true});await h.start(attachmentWorkspace);
    await h.navigate({view:'conversation',conversation:channelA});h.reply(boardPath(channelA),{posts:[],next_before:null});await turn();
    await chooseAttachment(h);h.reply(uploadPath,{file:fileMetadata,channel_id:channelA});await turn();
    await h.event(h.find(node=>node.attributes['aria-label']==='Remove notes.pdf'));
    assert.equal(h.text().includes('notes.pdf'),false);
    assert.ok('disabled' in buttonNamed(h,'Post message').attributes);
    assert.equal(h.count('posts'),0);assert.equal(h.calls.filter(call=>call.options.method==='DELETE').length,0);
});
test('older servers disable upload controls while leaving text messaging available',async t=>{
    const h=harness(t,{compose:true});await h.start();
    await h.navigate({view:'conversation',conversation:channelA});h.reply(boardPath(channelA),{posts:[],next_before:null});await turn();
    assert.equal(h.find(node=>node.attributes['aria-label']==='Choose an attachment').disabled,true);
    const input=h.find(node=>node.tag==='textarea');input.value='Text still works';await h.event(input,'input');
    await h.event(buttonNamed(h,'Post message'));assert.equal(h.count('posts'),1);
    assert.equal(h.calls.some(call=>uploadPath(call.route)),false);
});
test('an upload response after suspension stays held and recovers without uploading another copy',async t=>{
    const h=harness(t,{compose:true});await h.start(attachmentWorkspace);
    await h.navigate({view:'conversation',conversation:channelA});h.reply(boardPath(channelA),{posts:[],next_before:null});await turn();
    await chooseAttachment(h);
    const stored=JSON.parse(h.host.localStorage.getItem(`murmur.operator.${userId}.${channelA}:`));assert.ok(stored.upload.op_id);
    h.hide('pagehide');h.resume('pagehide');h.reply('bootstrap',attachmentWorkspace);await turn();
    h.reply(boardPath(channelA),{posts:[],next_before:null});await turn();
    h.reply(uploadPath,{file:fileMetadata,channel_id:channelA});await turn();
    assert.ok(h.text().includes('needs confirmation'));
    assert.equal(JSON.parse(h.host.localStorage.getItem(`murmur.operator.${userId}.${channelA}:`)).files,undefined);
    await h.event(buttonNamed(h,'Check upload status'));
    h.reply('operations/'+stored.upload.op_id,{status:'complete',result:{channel_id:channelA,file:fileMetadata}});await turn();
    assert.deepEqual(JSON.parse(h.host.localStorage.getItem(`murmur.operator.${userId}.${channelA}:`)).files,[fileMetadata]);
    assert.equal(h.count(uploadPath),1);
});
test('attachment names are text, downloads are authenticated, and suspension revokes private object URLs',async t=>{
    const h=harness(t);await h.start(attachmentWorkspace);
    await h.navigate({view:'conversation',conversation:channelA});
    const name='PRIVATE:<script>run()</script>.html';
    h.reply(boardPath(channelA),{posts:[{...post(channelA,''),file_ids:[fileId,id('z')],files:[{...fileMetadata,name,mime_type:'text/html'},{id:id('z'),available:false}]}],next_before:null});await turn();
    assert.ok(h.text().includes(name));assert.ok(h.text().includes('Attachment unavailable'));
    assert.equal(h.mount.all().some(node=>['script','iframe','img','object','embed'].includes(node.tag)),false);
    const download=h.find(node=>node.attributes['aria-label']==='Download '+name);
    await h.event(download);
    const path='posts/'+rootId+'/files/'+fileId;
    const data=new Blob(['private file bytes']);const call=h.reply(path,data);await turn();
    assert.equal(call.options.headers['X-Murmur-User-Id'],userId);
    assert.deepEqual([...h.blobs.values()],[data]);
    assert.ok(h.created.some(node=>node.tag==='a'&&node.attributes.download===name&&node.clicked));
    await h.event(download);h.hide('pagehide');
    assert.equal(h.blobs.size,0);assert.equal(h.revoked.length,1);assert.equal(h.text().includes(name),false);
    h.reply(path,new Blob(['late private file']));await turn();
    assert.equal(h.blobs.size,0);assert.equal(h.revoked.length,1,'late responses must not create more object URLs');
});

test('attachment-only Inbox, agent activity and shared recall cards have bounded filename/count previews',async t=>{
    const h=harness(t),longName='report-'.repeat(20)+'.pdf';
    const fileOnly={...post(channelA,''),file_ids:[fileId,id('g'),id('z')],files:[
        {...fileMetadata,name:longName},{...fileMetadata,id:id('g'),name:'plot.png',mime_type:'image/png'},{id:id('z'),available:false},
    ]};
    const unknown={...post(channelA,'  '),id:id('n'),file_ids:[id('y')],files:[{id:id('y'),available:false}]};
    await h.start({...workspace,agents:[{id:'codex:reviewer',name:'Reviewer'}],capabilities:{...workspace.capabilities,personal_workspace:true,agent_activity:true}});
    h.reply('workspace',personalState(1));await turn();
    h.reply('workspace/saved',{revision:1,items:[{post_id:fileOnly.id,note:'',post:fileOnly}]});await turn();
    h.reply('agent-activity',{agents:[{id:'codex:reviewer',latest:fileOnly}]});await turn();
    await h.navigate({view:'inbox'});h.reply(inboxPath,inboxPage([attentionItem(fileOnly),attentionItem(unknown)]));await turn();
    const summary='Attachments (3): '+longName.slice(0,60)+', plot.png · +1 more';
    assert.ok(h.text().includes(summary));assert.equal(h.text().includes(longName),false);
    assert.ok(h.mount.all().some(node=>node.tag==='p'&&node.textContent==='Attachment'),'missing metadata still shows a useful attachment label');
    await h.navigate({view:'agents'});assert.ok(h.text().includes(summary.slice(0,140)));
    await h.navigate({view:'saved'});assert.ok(h.text().includes(summary));
});
test('attachment previews preserve caption text and full-text search snippets beyond the preview boundary',async t=>{
    const h=harness(t);await startPersonal(h);
    const caption={...post(channelA,'Please review this plot'),file_ids:[fileId],files:[fileMetadata]};
    await h.navigate({view:'inbox'});h.reply(inboxPath,inboxPage([attentionItem(caption)]));await turn();
    assert.ok(h.text().includes('Please review this plot'));assert.equal(h.text().includes('Attachment: notes.pdf'),false);
    await h.search('needle','everywhere');
    h.reply(searchRoute('needle'),{posts:[{...caption,message:'prefix '.repeat(100)+'needle remains visible'}],next_before:null});await turn();
    assert.ok(h.text().includes('needle remains visible'),'normal search keeps its window around the original full message');
});
test('an unjournaled upload rejection can be explicitly removed without losing text or claiming server cancellation',async t=>{
    const h=harness(t,{compose:true});await h.start(attachmentWorkspace);
    await h.navigate({view:'conversation',conversation:channelA});h.reply(boardPath(channelA),{posts:[],next_before:null});await turn();
    const input=h.find(node=>node.tag==='textarea');input.value='Keep this text';await h.event(input,'input');
    await chooseAttachment(h);h.reply(uploadPath,{error:'Invalid filename'},400);await turn();
    assert.ok(h.text().includes('does not cancel the upload or delete a file'));
    const before=JSON.parse(h.host.localStorage.getItem(`murmur.operator.${userId}.${channelA}:`));assert.ok(before.upload);
    await h.event(buttonNamed(h,'Remove upload from draft'));
    const after=JSON.parse(h.host.localStorage.getItem(`murmur.operator.${userId}.${channelA}:`));
    assert.equal(after.upload,undefined);assert.equal(after.text,'Keep this text');assert.equal(after.pending,null);
    assert.equal(h.count(uploadPath),1);assert.equal(h.count('posts'),0);
    assert.equal(h.calls.some(call=>call.options.method==='DELETE'),false);
});
