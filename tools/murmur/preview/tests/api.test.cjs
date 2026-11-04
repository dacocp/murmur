const {test} = require('node:test');
const assert = require('node:assert/strict');
const {createClient, mergePosts} = require('../api.js');
const id = char => char.repeat(26);
const root = {id:id('a'), channel_id:id('b'), user_id:id('c'), root_id:'', author:'Agent', message:'Hello', create_at:10, reply_count:0};
const bootstrap = {user:{id:id('c'), username:'human'}, agents:[], conversations:[], capabilities:{root_board:true, exact_context:true}};
function response(data, status = 200, cache = 'private, no-store') {
    return {ok:status < 400, status, headers:new Headers({'Cache-Control':cache}), json:async () => data};
}
function setup(fetch) {
    const calls = [];
    const client = createClient({fetch:async (...args) => {calls.push(args); return fetch(...args);},
        document:{cookie:'MMCSRF=token'}, AbortController, setTimeout, clearTimeout});
    return {client,calls};
}
test('only named read routes, with bound human, CSRF, no-store and no redirects', async () => {
    const {client,calls} = setup(async path => response(path.endsWith('bootstrap') ? bootstrap : path.includes('context') ? {post:root,root} : {posts:[root],root,next_before:null}));
    await client.bootstrap(); await client.board(id('b')); await client.thread(id('b'),id('a')); await client.context(id('a'));
    assert.equal(client.request, undefined); assert.equal(client.send, undefined);
    for (const [,opts] of calls) {
        assert.equal(opts.method,'GET'); assert.equal(opts.cache,'no-store'); assert.equal(opts.credentials,'same-origin'); assert.equal(opts.redirect,'error');
        assert.equal(opts.headers['X-CSRF-Token'],'token'); assert.equal(opts.body,undefined);
    }
    assert.equal(calls[1][1].headers['X-Murmur-User-Id'],id('c'));
    await assert.rejects(client.context('../operations/secret'),/Invalid/);
    assert.equal(calls.length,4);
});
test('private responses require both cache directives before reading payload', async () => {
    for (const header of ['', 'no-store', 'private', 'public, max-age=300']) {
        let decoded = false;
        const {client} = setup(async () => ({...response({},200,header),json:async () => {decoded=true;return bootstrap;}}));
        await assert.rejects(client.bootstrap(), error => error.status===426);
        assert.equal(decoded,false);
    }
});
test('401,403 and changed user never return content', async () => {
    for (const status of [401,403,409]) {
        const {client} = setup(async () => response(bootstrap,status));
        await assert.rejects(client.bootstrap(),e => e.status===status);
    }
    let count = 0;
    const {client} = setup(async () => response(count++ ? {...bootstrap,user:{id:id('d'),username:'other'}} : bootstrap));
    await client.bootstrap(); await assert.rejects(client.bootstrap(),e => e.status===409);
});
test('native DM pair is optional for older servers but must be two distinct user identities when supplied', async () => {
    const chat = {id:id('b'), name:'Human / Agent', kind:'dm', members:['codex:agent']};
    for (const entry of [chat, {...chat,dm_user_ids:[id('c'),id('d')]}]) {
        const {client} = setup(async () => response({...bootstrap,conversations:[entry]}));
        assert.deepEqual((await client.bootstrap()).conversations[0],entry);
    }
    for (const pair of [null, 'human', [], [id('c')], [id('c'),id('c')], [id('c'),'codex:agent'], [id('c'),id('d'),id('e')]]) {
        const {client} = setup(async () => response({...bootstrap,conversations:[{...chat,dm_user_ids:pair}]}));
        await assert.rejects(client.bootstrap(),/direct-message participants/);
    }
    const {client} = setup(async () => response({...bootstrap,conversations:[{...chat,kind:'channel',dm_user_ids:[id('c'),id('d')]}]}));
    await assert.rejects(client.bootstrap(),/direct-message participants/);
});
test('suspension discards a response even when the network ignores abort', async () => {
    let resolve;
    const {client,calls} = setup(() => new Promise(done => {resolve=done;}));
    const pending = client.bootstrap(); client.cancel(); resolve(response(bootstrap));
    await assert.rejects(pending,/superseded/); assert.equal(calls[0][1].signal.aborted,true);
});
test('cross-channel, reply-as-board, mismatched roots and contexts fail closed', async () => {
    const {client} = setup(async () => response({posts:[{...root,channel_id:id('d')}],root,next_before:null}));
    await assert.rejects(client.board(id('b')),/another conversation/);
    const badBoard = setup(async () => response({posts:[{...root,root_id:id('e')}],next_before:null})).client;
    await assert.rejects(badBoard.board(id('b')),/root-message/);
    const badThread = setup(async () => response({posts:[],root:{...root,id:id('e')},next_before:null})).client;
    await assert.rejects(badThread.thread(id('b'),id('a')),/does not belong/);
    const badContext = setup(async () => response({post:{...root,root_id:id('e')},root})).client;
    await assert.rejects(badContext.context(id('a')),/context/);
});
test('overlapping pages deduplicate by canonical ID and preserve arrival order', () => {
    const newer={...root,message:'edited'};
    const earlier={...root,id:id('z'),create_at:1};
    const later={...root,id:id('d'),create_at:20};
    const posts=mergePosts([earlier,root],[newer,later]);
    assert.deepEqual(posts.map(p=>p.id),[earlier.id,root.id,later.id]);
    assert.equal(posts[1].message,'edited');
});
test('a late authentication failure invalidates the session independently of route listeners', async () => {
    for (const status of [401,403,409]) {
        let resolve, invalidated = null;
        const client = createClient({fetch:() => new Promise(done => {resolve=done;}),
            document:{cookie:''}, AbortController,setTimeout,clearTimeout}, error => {invalidated=error.status;client.cancel();});
        const abandonedRouteRequest = client.board(id('b'));
        resolve(response({},status));
        await assert.rejects(abandonedRouteRequest,e=>e.status===status);
        assert.equal(invalidated,status);
    }
});

test('search preserves literal query and opaque cursor while enforcing selected scope', async () => {
    const {client,calls} = setup(async () => response({posts:[root],next_before:'opaque_search_cursor'}));
    const query = '  100%_ C:\\logs + café  ';
    const result = await client.search(query,{conversation:root.channel_id,root:root.id,before:'cursor/+=?'});
    const url = new URL(calls[0][0], 'https://fixture.invalid');
    assert.equal(url.pathname,'/plugins/murmur/operator/v1/search');
    assert.equal(url.searchParams.get('q'),query.trim());
    assert.equal(url.searchParams.get('before'),'cursor/+=?');
    assert.equal(url.searchParams.get('conversation'),root.channel_id);
    assert.equal(url.searchParams.get('root'),root.id);
    assert.equal(result.next_before,'opaque_search_cursor');
    assert.equal(calls[0][1].method,'GET');
    assert.equal(calls[0][1].cache,'no-store');
    assert.equal(calls[0][1].body,undefined);
    await assert.rejects(client.search('  '),/phrase/);
    await assert.rejects(client.search('x'.repeat(257)),/phrase/);
    await assert.rejects(client.search('phrase',{conversation:'../operations'}),/Invalid/);
    assert.equal(calls.length,1,'invalid scopes and queries never reach the server');
    await client.search('😀'.repeat(256));
    assert.equal(calls.length,2,'the query bound counts Unicode characters');
});

test('search rejects malformed or cross-scope results without exposing them', async () => {
    const badResults = [
        {posts:[{...root,channel_id:id('x')}],next_before:null},
        {posts:[{...root,root_id:id('x')}],next_before:null},
        {posts:[root],next_before:12},
        {posts:Array(51).fill(root),next_before:null},
    ];
    for (const data of badResults) {
        const {client} = setup(async () => response(data));
        await assert.rejects(client.search('hello',{conversation:root.channel_id,root:root.id}),/search page|another conversation/i);
    }
});

test('writes retain the bound human and CSRF while local revision conflicts retain recovery evidence', async () => {
    const rejection = {status:'failed',dispatch:'rejected',op_id:'frozen-id',error:'Destination changed'};
    const {client,calls} = setup(async path => response(path.endsWith('bootstrap') ? bootstrap : rejection,path.endsWith('bootstrap') ? 200 : 409));
    await assert.rejects(client.postMessage({}),error=>error.status===401);
    assert.equal(calls.length,0,'no anonymous mutation is dispatched');
    await client.bootstrap();
    await assert.rejects(client.postMessage({op_id:'frozen-id',channel_id:root.channel_id,root_id:'',message:'hello'}),error=>{
        assert.deepEqual(error.result,rejection); assert.equal(error.status,409); assert.doesNotMatch(error.message,/account changed/i);return true;
    });
    const [,options] = calls[1];
    assert.equal(options.method,'POST'); assert.equal(options.headers['X-Murmur-User-Id'],bootstrap.user.id);
    assert.equal(options.headers['X-CSRF-Token'],'token'); assert.equal(options.cache,'no-store');
    assert.equal(JSON.parse(options.body).op_id,'frozen-id');
});

test('only an actual session-change conflict invalidates the session on a mutation', async () => {
    let invalidations=0, errorText='Inbox item changed; refresh before handling';
    const client=createClient({document:{cookie:''},AbortController,setTimeout,clearTimeout,fetch:async path=>response(path.endsWith('bootstrap') ? bootstrap : {error:errorText},path.endsWith('bootstrap') ? 200:409)},()=>invalidations++);
    await client.bootstrap(); await assert.rejects(client.handle(root.id,1)); assert.equal(invalidations,0);
    errorText='operator session changed; reload before continuing';
    await assert.rejects(client.handle(root.id,1)); assert.equal(invalidations,1);
});

test('saved and Inbox items must point to their canonical returned message', async () => {
    const {client}=setup(async path=>response(path.includes('saved') ? {revision:1,items:[{post_id:id('x'),note:'private',post:root}]} : {total:1,complete:true,items:[{post_id:root.id,channel_id:id('x'),revision:1,reasons:['mention'],post:root}]}));
    await assert.rejects(client.saved(),/does not match/); await assert.rejects(client.inbox(),/does not match/);
});

const attachment={id:id('f'),name:'briefing.html',size:3,mime_type:'text/html'};
test('raw upload freezes descriptor query and body with bound-human and CSRF authorization', async () => {
    const {client,calls}=setup(async path=>response(path.endsWith('bootstrap')?bootstrap:{file:attachment,channel_id:root.channel_id}));
    await client.bootstrap();
    const descriptor={op_id:'original-upload',channel_id:root.channel_id,filename:attachment.name,size:3,sha256:'a'.repeat(64)};
    const bytes=new Uint8Array([97,98,99]).buffer;
    assert.deepEqual((await client.uploadFile(descriptor,bytes)).file,attachment);
    const [path,options]=calls[1],url=new URL(path,'https://fixture.invalid');
    assert.equal(url.pathname,'/plugins/murmur/operator/v1/files');
    for(const [key,value] of Object.entries(descriptor))assert.equal(url.searchParams.get(key),String(value));
    assert.equal(options.body,bytes);assert.equal(options.headers['Content-Type'],'application/octet-stream');
    assert.equal(options.headers['X-Murmur-User-Id'],bootstrap.user.id);assert.equal(options.headers['X-CSRF-Token'],'token');
    assert.equal(options.cache,'no-store');assert.equal(options.redirect,'error');
    await assert.rejects(client.uploadFile({...descriptor,size:4},bytes),/Invalid saved upload/);
    await assert.rejects(client.uploadFile({...descriptor,size:0},new ArrayBuffer(0)),/Invalid saved upload/);
    assert.equal(calls.length,2,'descriptor/body mismatch is rejected before dispatch');
});
test('message attachment metadata cannot substitute another post file or malformed bytes', async () => {
    for(const changes of [
        {file_ids:[id('f'),id('f')],files:[attachment]},
        {file_ids:[id('f')],files:[{...attachment,id:id('x')}]},
        {file_ids:[id('f')],files:[{...attachment,size:-1}]},
    ]) {
        const {client}=setup(async()=>response({posts:[{...root,...changes}],next_before:null}));
        await assert.rejects(client.board(root.channel_id),/attachment/i);
    }
    const {client}=setup(async()=>response({posts:[{...root,message:'',file_ids:[id('f')],files:[attachment]}],next_before:null}));
    assert.equal((await client.board(root.channel_id)).posts[0].files[0].name,'briefing.html');
});
test('downloads use exact post/file authorization and reject stale binary responses after suspension', async () => {
    let startBlob,finishBlob;
    const entered=new Promise(resolve=>{startBlob=resolve;});
    const bytes=new Blob(['abc']);
    const {client,calls}=setup(async path=>path.endsWith('bootstrap')?response(bootstrap):{
        ...response(null),blob:()=>{startBlob();return new Promise(resolve=>{finishBlob=resolve;});},
    });
    await client.bootstrap();
    const reading=client.downloadFile(root.id,attachment.id);
    await entered;
    assert.equal(calls[1][0],'/plugins/murmur/operator/v1/posts/'+root.id+'/files/'+attachment.id);
    assert.equal(calls[1][1].headers['X-Murmur-User-Id'],bootstrap.user.id);
    assert.equal(calls[1][1].credentials,'same-origin');
    client.cancel();finishBlob(bytes);
    await assert.rejects(reading,/superseded/);
    await assert.rejects(client.downloadFile('../outside',attachment.id),/Invalid/);
    assert.equal(calls.length,2);
});
test('file download privacy headers are checked before consuming binary content', async () => {
    let consumed=false;
    const {client}=setup(async()=>({...response(null,200,'public'),blob:async()=>{consumed=true;return new Blob(['private']);}}));
    await assert.rejects(client.downloadFile(root.id,attachment.id),error=>error.status===426);
    assert.equal(consumed,false);
});
