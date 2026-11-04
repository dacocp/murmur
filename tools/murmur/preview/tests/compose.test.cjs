const test = require('node:test');
const assert = require('node:assert/strict');
const Compose = require('../compose.js');
const Legacy = require('../../webapp/model.js');

function deferred() { let resolve, reject; const promise = new Promise((a,b) => { resolve = a; reject = b; }); return {promise,resolve,reject}; }
function fixture() {
    const values = new Map(), queues = new Map(), lockNames = [], calls = [];
    const storage = {
        get length() { return values.size; }, key(n) { return [...values.keys()][n] ?? null; },
        getItem(key) { return values.get(key) ?? null; }, setItem(key, value) { values.set(key, value); },
    };
    let next = 0;
    const host = {localStorage: storage, crypto: {randomUUID: () => 'op-' + ++next}, navigator: {locks: {
        request(key, action) {
            lockNames.push(key);
            const task = (queues.get(key) || Promise.resolve()).then(action);
            queues.set(key, task.catch(() => {}));
            return task;
        },
    }}};
    const client = {
        async postMessage(pending) { calls.push(['post', {...pending}]); return {post: {id: 'posted', user_id: 'human', ...pending}}; },
        async operation(id) { calls.push(['check', id]); return {status: 'complete', result: {post_id: 'posted', channel_id: 'chat'}}; },
        async openDM(request) { calls.push(['dm', {...request}]); return {conversation: {id: 'dm', kind: 'dm', name: 'Human and agent'}}; },
    };
    const attachmentPolicy = {enabled:true,max_file_size:1024,max_files_per_post:10};
    const create = (userId = 'human') => Compose.create({host, client, userId, attachmentPolicy:()=>attachmentPolicy});
    const controller = create();
    return {host, client, controller, create, values, calls, lockNames, attachmentPolicy,
        async draft(text = 'Please review this', channel = 'chat', root = '') {
            return controller.save(channel, root, text, controller.read(channel, root));
        }};
}

test('legacy and preview use the same persisted draft and lock, with distinct root/reply and human scopes', async () => {
    const f = fixture(), legacy = new Legacy.DraftStore(f.host.localStorage, 'human');
    await Legacy.withDraftLock(f.host, legacy, 'chat', '', () => legacy.edit('chat', '', 'Native draft'));
    const visible = f.controller.read('chat');
    assert.equal(visible.text, 'Native draft');
    await f.controller.save('chat', '', 'Edited in preview', visible);
    await f.draft('Thread reply', 'chat', 'root');
    assert.equal(legacy.read('chat', '').text, 'Edited in preview');
    assert.equal(f.create('other').read('chat').text, '');
    assert.deepEqual(f.controller.list().map(x => [x.channel,x.root,x.text]), [['chat','','Edited in preview'],['chat','root','Thread reply']]);
    assert.deepEqual(f.lockNames, ['murmur.operator.human.chat:', 'murmur.operator.human.chat:', 'murmur.operator.human.chat:root']);
    assert.equal(f.calls.length, 0);
});

test('stale edits and sends cannot replace a draft changed in the current console', async () => {
    const f = fixture(), before = await f.draft();
    const legacy = new Legacy.DraftStore(f.host.localStorage, 'human');
    await Legacy.withDraftLock(f.host, legacy, 'chat', '', () => legacy.edit('chat', '', 'Different current-console draft'));
    await assert.rejects(f.controller.save('chat', '', 'Stale typing', before), /changed in another window/);
    await assert.rejects(f.controller.send('chat', '', before), /changed in another window/);
    assert.equal(legacy.read('chat').text, 'Different current-console draft');
    assert.equal(f.calls.length, 0);
});

test('send persists immutable authority before network and clears only after canonical confirmation', async () => {
    const f = fixture(), visible = await f.draft('Hello'), gate = deferred();
    f.client.postMessage = async pending => {
        const stored = new Legacy.DraftStore(f.host.localStorage, 'human').read('chat', '');
        assert.deepEqual(stored.pending, pending);
        assert.equal(pending.message, 'Hello');
        return gate.promise;
    };
    const sending = f.controller.send('chat', '', visible);
    await Promise.resolve(); await Promise.resolve();
    assert.equal(f.controller.read('chat').pending.op_id, 'op-1');
    gate.resolve({post: {id:'p1', user_id:'human', channel_id:'chat', root_id:'', message:'Hello'}});
    const result = await sending;
    assert.equal(result.postId, 'p1');
    assert.deepEqual(result.draft, {text:'',pending:null});
});

test('lost response survives reload and cannot become a new send; explicit recovery uses original operation', async () => {
    const f = fixture(), visible = await f.draft();
    f.client.postMessage = async pending => { f.calls.push(['post',pending]); throw Error('Response lost'); };
    await assert.rejects(f.controller.send('chat','',visible), /Response lost/);
    const reload = f.create(), held = reload.read('chat');
    assert.equal(held.pending.op_id,'op-1');
    assert.equal(reload.list().length,1);
    await assert.rejects(reload.send('chat','',held), /original send is saved/);
    const checked = await reload.recover('chat','',held);
    assert.equal(checked.postId,'posted');
    assert.deepEqual(f.calls.map(c => c[0]),['post','check']);
    assert.equal(f.calls[1][1],'op-1');
});

test('same-partition concurrent windows cannot create two IDs for one visible send', async () => {
    const f = fixture(), visible = await f.draft(), second = f.create();
    const outcomes = await Promise.allSettled([f.controller.send('chat','',visible),second.send('chat','',visible)]);
    assert.equal(outcomes.filter(x => x.status === 'fulfilled').length,1);
    assert.match(outcomes.find(x => x.status === 'rejected').reason.message,/changed in another window/);
    assert.equal(f.calls.filter(x => x[0] === 'post').length,1);
});

test('only explicit check404 permits retry of the same immutable payload and ID', async () => {
    const f = fixture(), visible = await f.draft('Original');
    const post = f.client.postMessage;
    f.client.postMessage = async pending => { f.calls.push(['lost',pending]); throw Error('Offline'); };
    await assert.rejects(f.controller.send('chat','',visible), /Offline/);
    const pending = f.controller.read('chat');
    await assert.rejects(f.controller.retry('chat','',pending),/Check the original/);
    f.client.operation = async () => { throw Object.assign(Error('No operation'),{status:404}); };
    const checked = await f.controller.recover('chat','',pending);
    assert.equal(checked.retryAllowed,true);
    f.client.postMessage = post;
    const result = await f.controller.retry('chat','',pending);
    assert.equal(result.postId,'posted');
    assert.deepEqual(f.calls[0][1],f.calls[1][1]);
});

test('unconfirmed errors and malformed confirmations retain the original pending send', async () => {
    for (const failure of [Object.assign(Error('Unauthorized'),{status:401}),Object.assign(Error('Conflict'),{status:409}),Error('Timeout')]) {
        const f = fixture(), visible = await f.draft();
        f.client.postMessage = async () => { throw failure; };
        await assert.rejects(f.controller.send('chat','',visible), e => e === failure);
        const held = f.controller.read('chat');
        assert.ok(held.pending); assert.equal(held.rejected,undefined);
        await assert.rejects(f.controller.releaseRejected('chat','',held),/Only a confirmed/);
    }
    const f = fixture(), visible = await f.draft();
    f.client.postMessage = async () => ({post:{id:'wrong',user_id:'another',channel_id:'chat',root_id:'',message:visible.text}});
    await assert.rejects(f.controller.send('chat','',visible),/Unexpected send confirmation/);
    const held = f.controller.read('chat');
    f.client.operation = async () => ({status:'complete',result:{post_id:'wrong',channel_id:'other'}});
    await assert.rejects(f.controller.recover('chat','',held),/Unexpected recovery/);
    assert.deepEqual(f.controller.read('chat'),held);
});

test('only canonical rejected marker unlocks explicit editing or discard', async () => {
    const f = fixture(), visible = await f.draft('Rejected payload');
    f.client.postMessage = async pending => { throw Object.assign(Error('Unavailable'),{status:409,result:{status:'failed',dispatch:'rejected',op_id:pending.op_id,error:'Channel unavailable'}}); };
    const result = await f.controller.send('chat','',visible);
    assert.equal(result.draft.rejected.error,'Channel unavailable');
    await assert.rejects(f.controller.save('chat','','Changed',result.draft),/Check the existing send/);
    const restored = await f.controller.releaseRejected('chat','',result.draft);
    assert.deepEqual(restored,{text:'Rejected payload',pending:null});
    const rejectedAgain = await f.controller.send('chat','',restored);
    assert.equal(rejectedAgain.draft.pending.op_id,'op-2');
    assert.deepEqual(await f.controller.releaseRejected('chat','',rejectedAgain.draft,true),{text:'',pending:null});
});

test('storage failure prevents dispatch and does not discard already-saved text', async () => {
    const f = fixture(), visible = await f.draft();
    f.host.localStorage.setItem = () => { throw Error('Storage full'); };
    await assert.rejects(f.controller.send('chat','',visible),/Storage full/);
    assert.equal(f.calls.length,0);
    assert.deepEqual(f.controller.read('chat'),visible);
    await assert.rejects(f.controller.openDM('codex:agent'),/Storage full/);
    assert.equal(f.calls.length,0);
});

test('missing Web Locks and installed-app mode disable mutations without hiding existing drafts', async () => {
    const f = fixture(), visible = await f.draft();
    f.host.navigator.locks = undefined;
    assert.equal(f.controller.availability().enabled,false);
    assert.throws(() => f.controller.send('chat','',visible),/Web Locks/);
    assert.deepEqual(f.controller.read('chat'),visible);
    f.host.navigator.locks = {request:(_,fn) => fn()}; f.host.navigator.standalone = true;
    assert.match(f.controller.availability().reason,/browser tab/);
    assert.throws(() => f.controller.send('chat','',visible),/browser tab/);
    assert.equal(f.calls.length,0);
});

test('suspension prevents queued dispatch and stale404 cannot grant retry after reauthentication', async () => {
    const f = fixture(), visible = await f.draft(), hold = deferred();
    const blocked = f.host.navigator.locks.request('murmur.operator.human.chat:',() => hold.promise);
    const sending = f.controller.send('chat','',visible);
    f.controller.suspend(); hold.resolve(); await blocked;
    await assert.rejects(sending,/Sign in again/);
    assert.equal(f.calls.length,0);
    assert.throws(() => f.controller.read('chat'),/Sign in again/);
    assert.throws(() => f.controller.resume('other'),/account changed/);
    f.controller.resume('human');
    f.client.postMessage = async () => { throw Error('Lost'); };
    await assert.rejects(f.controller.send('chat','',visible),/Lost/);
    const held = f.controller.read('chat'), check = deferred();
    f.client.operation = () => check.promise;
    const recovery = f.controller.recover('chat','',held);
    await Promise.resolve(); await Promise.resolve();
    f.controller.suspend(); f.controller.resume('human');
    check.reject(Object.assign(Error('Old404'),{status:404}));
    await assert.rejects(recovery,/Old404/);
    await assert.rejects(f.controller.retry('chat','',held),/Check the original/);
});

test('reauthentication does not revive sends or DM openings queued before suspension', async () => {
    const f = fixture(), visible = await f.draft(), hold = deferred();
    const blockedSend = f.host.navigator.locks.request('murmur.operator.human.chat:', () => hold.promise);
    const blockedDM = f.host.navigator.locks.request('murmur.operator.open.human.codex:one', () => hold.promise);
    const sending = f.controller.send('chat', '', visible);
    const opening = f.controller.openDM('codex:one');
    f.controller.suspend();
    f.controller.resume('human');
    hold.resolve();
    await Promise.all([blockedSend, blockedDM]);
    await assert.rejects(sending, /action was interrupted/);
    await assert.rejects(opening, /action was interrupted/);
    assert.deepEqual(f.calls, []);
    assert.deepEqual(f.controller.read('chat'), visible);
    assert.equal(f.controller.readDM('codex:one'), null);
});

test('a canonical confirmation after suspension can settle only its already-dispatched send', async () => {
    const f = fixture(), visible = await f.draft('Already dispatched'), dispatched = deferred(), response = deferred();
    f.client.postMessage = async pending => {
        f.calls.push(['post', {...pending}]);
        dispatched.resolve();
        return response.promise;
    };
    const sending = f.controller.send('chat', '', visible);
    await dispatched.promise;
    f.controller.suspend();
    response.resolve({post: {id: 'canonical', user_id: 'human', channel_id: 'chat', root_id: '', message: visible.text}});
    assert.equal((await sending).postId, 'canonical');
    assert.throws(() => f.controller.read('chat'), /Sign in again/);
    f.controller.resume('human');
    assert.deepEqual(f.controller.read('chat'), {text: '', pending: null});
    assert.equal(f.calls.length, 1);
});

test('DM creation reuses exact legacy identity and does not send a message', async () => {
    const f = fixture();
    const first = await f.controller.openDM('codex:same-name');
    assert.equal(first.status,'complete');
    assert.equal(first.agent_id,'codex:same-name');
    assert.deepEqual(await f.create().openDM('codex:same-name'),first);
    await f.controller.openDM('claude:same-name');
    assert.deepEqual(f.calls.map(c => [c[0],c[1].agent_id]),[['dm','codex:same-name'],['dm','claude:same-name']]);
    assert.deepEqual(f.controller.listDM().map(x => x.agent),['codex:same-name','claude:same-name']);
    assert.equal(f.controller.list().length,0);
    assert.equal(f.lockNames[0],'murmur.operator.open.human.codex:same-name');
});

test('lost DM response holds its identity across reload and recovery does not create a new DM intent', async () => {
    const f = fixture();
    f.client.openDM = async request => { f.calls.push(['dm',request]); throw Error('Lost response'); };
    await assert.rejects(f.controller.openDM('codex:one'),/Lost response/);
    const reload = f.create(), pending = reload.readDM('codex:one');
    assert.equal((await reload.openDM('codex:one')).op_id,pending.op_id);
    f.client.operation = async id => { f.calls.push(['check',id]); return {status:'complete',result:{channel_id:'exact-dm'}}; };
    const recovered = await reload.recoverDM('codex:one',pending);
    assert.equal(recovered.entry.conversation.id,'exact-dm');
    assert.deepEqual(f.calls.map(c => c[0]),['dm','check']);
    await assert.rejects(reload.recoverDM('codex:one',pending),/changed in another window/);
});

test('DM retry requires check404 and retains exact frozen agent and operation', async () => {
    const f = fixture(), send = f.client.openDM;
    f.client.openDM = async request => { f.calls.push(['lost',request]); throw Error('Offline'); };
    await assert.rejects(f.controller.openDM('codex:one'),/Offline/);
    const entry = f.controller.readDM('codex:one');
    await assert.rejects(f.controller.retryDM('codex:one',entry),/Check the saved/);
    f.client.operation = async () => { throw Object.assign(Error('Not found'),{status:404}); };
    assert.equal((await f.controller.recoverDM('codex:one',entry)).retryAllowed,true);
    f.client.openDM = send;
    assert.equal((await f.controller.retryDM('codex:one',entry)).entry.status,'complete');
    assert.deepEqual(f.calls[0][1],f.calls[1][1]);
});

const fileId = 'f'.repeat(26);
const fileInfo = {id:fileId,name:'example.txt',size:3,mime_type:'text/plain'};
function selectedFile(content = 'abc', name = 'example.txt') {
    const bytes = new TextEncoder().encode(content);
    return {name,size:bytes.byteLength,arrayBuffer:async()=>bytes.slice().buffer};
}
function fileFixture(f) {
    f.host.crypto.subtle = require('node:crypto').webcrypto.subtle;
    f.client.uploadFile = async (request, bytes) => {
        f.calls.push(['upload', {...request}, [...new Uint8Array(bytes)]]);
        assert.deepEqual(f.controller.read('chat').upload,request,'intent must be durable before any upload bytes');
        return {file:{...fileInfo,name:request.filename,size:request.size},channel_id:request.channel_id};
    };
}
test('file-only draft persists metadata, freezes attachment IDs, and clears only after the exact canonical post', async () => {
    const f=fixture();fileFixture(f);
    const uploaded=await f.controller.upload('chat','',selectedFile(),f.controller.read('chat'));
    assert.equal(f.calls[0][1].sha256,'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
    assert.deepEqual(f.calls[0][2],[97,98,99]);
    assert.equal(uploaded.draft.upload,undefined);
    assert.deepEqual(f.create().read('chat').files,[fileInfo]);
    assert.equal(f.controller.list().length,1,'file-only draft is discoverable');
    const result=await f.controller.send('chat','',uploaded.draft);
    assert.deepEqual(f.calls.find(call=>call[0]==='post')[1].file_ids,[fileId]);
    assert.equal(result.postId,'posted');assert.deepEqual(result.draft,{text:'',pending:null});
});
test('reverse-order staged IDs become a canonical sorted request before native post normalization',async()=>{
    const f=fixture();fileFixture(f);const selectedIds=['z'.repeat(26),'a'.repeat(26)];let next=0;
    f.client.uploadFile=async request=>({channel_id:'chat',file:{...fileInfo,id:selectedIds[next++],name:request.filename,size:request.size}});
    const first=(await f.controller.upload('chat','',selectedFile('abc','first.txt'),f.controller.read('chat'))).draft;
    const second=(await f.controller.upload('chat','',selectedFile('def','second.txt'),first)).draft;
    assert.deepEqual(second.files.map(file=>file.id),selectedIds,'display selection order stays separate');
    f.client.postMessage=async pending=>{
        f.calls.push(['post',pending]);
        assert.deepEqual(pending.file_ids,['a'.repeat(26),'z'.repeat(26)],'native canonical IDs are frozen before dispatch');
        const stored=f.create().read('chat');
        assert.deepEqual(stored.pending,pending);assert.deepEqual(stored.files.map(file=>file.id),selectedIds);
        return{post:{id:'posted',user_id:'human',...pending,file_ids:[...pending.file_ids].sort()}};
    };
    assert.equal((await f.controller.send('chat','',second)).postId,'posted');
    assert.equal(f.calls.length,1);
});
test('multi-file confirmation rejects duplicate, missing, substituted or noncanonical IDs',async()=>{
    const a='a'.repeat(26),z='z'.repeat(26);
    for(const observed of [[a,a],[a],[a,'x'.repeat(26)],[z,a]]) {
        const f=fixture();
        const draft={text:'Both files',pending:null,files:[{...fileInfo,id:z},{...fileInfo,id:a,name:'second.txt'}]};
        f.values.set('murmur.operator.human.chat:',JSON.stringify(draft));
        f.client.postMessage=async pending=>({post:{id:'posted',user_id:'human',...pending,file_ids:observed}});
        await assert.rejects(f.controller.send('chat','',draft),/Unexpected send confirmation/);
        assert.deepEqual(f.controller.read('chat').pending.file_ids,[a,z]);
    }
});
test('removing a staged file preserves text and never sends or deletes a server file', async () => {
    const f=fixture();fileFixture(f);
    const draft=await f.draft('Keep this text');
    const attached=(await f.controller.upload('chat','',selectedFile(),draft)).draft;
    const removed=await f.controller.removeFile('chat','',fileId,attached);
    assert.equal(removed.text,'Keep this text');assert.deepEqual(removed.files,[]);
    assert.deepEqual(f.calls.map(call=>call[0]),['upload']);
});
test('lost upload response survives reload and checks its original operation without reuploading', async () => {
    const f=fixture();fileFixture(f);
    f.client.uploadFile=async request=>{f.calls.push(['upload',request]);throw Error('Upload response lost');};
    await assert.rejects(f.controller.upload('chat','',selectedFile(),f.controller.read('chat')),/response lost/);
    const reload=f.create(),held=reload.read('chat');
    await assert.rejects(reload.send('chat','',held),/saved upload/);
    f.client.operation=async op=>{assert.equal(op,held.upload.op_id);f.calls.push(['check',op]);return{status:'complete',result:{channel_id:'chat',file:fileInfo}};};
    const result=await reload.recoverUpload('chat','',held);
    assert.deepEqual(result.draft.files,[fileInfo]);
    assert.deepEqual(f.calls.map(call=>call[0]),['upload','check']);
    assert.ok(![...f.values.values()].some(value=>value.includes('ArrayBuffer')));
});
test('an upload retries only after check404 and only with the same descriptor and exact bytes', async () => {
    const f=fixture();fileFixture(f);
    const upload=f.client.uploadFile;
    f.client.uploadFile=async request=>{f.calls.push(['lost',request]);throw Error('Lost');};
    await assert.rejects(f.controller.upload('chat','',selectedFile(),f.controller.read('chat')),/Lost/);
    const held=f.controller.read('chat');
    await assert.rejects(f.controller.retryUpload('chat','',selectedFile(),held),/Check the original upload/);
    f.client.operation=async()=>{throw Object.assign(Error('Not found'),{status:404});};
    assert.equal((await f.controller.recoverUpload('chat','',held)).uploadRetryAllowed,true);
    await assert.rejects(f.controller.retryUpload('chat','',selectedFile('abd'),held),/same original file/);
    assert.equal(f.calls.length,1,'a changed file must not dispatch bytes');
    f.client.uploadFile=upload;
    await f.controller.retryUpload('chat','',selectedFile(),held);
    assert.deepEqual(f.calls[0][1],f.calls[1][1],'retry retains the same immutable upload operation');
});
test('attachment substitution in a direct post confirmation keeps the original send held', async () => {
    const f=fixture();fileFixture(f);
    const attached=(await f.controller.upload('chat','',selectedFile(),f.controller.read('chat'))).draft;
    const wrong={id:'posted',user_id:'human',channel_id:'chat',root_id:'',message:'',file_ids:['x'.repeat(26)]};
    f.client.postMessage=async()=>({post:wrong});
    await assert.rejects(f.controller.send('chat','',attached),/Unexpected send confirmation/);
    const held=f.create().read('chat');assert.deepEqual(held.pending.file_ids,[fileId]);
    assert.deepEqual(f.controller.read('chat').pending,held.pending);
});
test('owned completed-operation metadata settles an attachment send even after the post is unavailable',async()=>{
    const f=fixture();fileFixture(f);
    const attached=(await f.controller.upload('chat','',selectedFile(),f.controller.read('chat'))).draft;
    f.client.postMessage=async pending=>{f.calls.push(['post',pending]);throw Error('Post response lost');};
    await assert.rejects(f.controller.send('chat','',attached),/response lost/);
    const held=f.create().read('chat');
    f.client.operation=async op=>{assert.equal(op,held.pending.op_id);f.calls.push(['check',op]);return{status:'complete',result:{post_id:'deleted-post',channel_id:'chat'}};};
    f.client.context=async()=>{throw Object.assign(Error('Deleted or no longer accessible'),{status:404});};
    const recovered=await f.controller.recover('chat','',held);
    assert.equal(recovered.postId,'deleted-post');assert.deepEqual(recovered.draft,{text:'',pending:null});
    assert.equal(recovered.retryAllowed,undefined);
    assert.deepEqual(f.calls.map(call=>call[0]),['upload','post','check'],'completed recovery never reads content or reposts');
});
test('only a canonical upload rejection releases its intent, retaining other draft content', async () => {
    const f=fixture();fileFixture(f);await f.draft('Preserve text');
    f.client.uploadFile=async request=>{throw Object.assign(Error('Rejected'),{result:{op_id:request.op_id,status:'failed',dispatch:'rejected',error:'Uploads disabled'}});};
    const result=await f.controller.upload('chat','',selectedFile(),f.controller.read('chat'));
    assert.equal(result.draft.uploadRejected.error,'Uploads disabled');
    const restored=await f.controller.releaseUpload('chat','',result.draft);
    assert.equal(restored.text,'Preserve text');assert.equal(restored.upload,undefined);
});
test('suspension during local hashing or an upload response cannot stage stale file content', async () => {
    for(const phase of ['hash','response']) {
        const f=fixture();fileFixture(f);const gate=deferred(),entered=deferred();
        if(phase==='hash')f.host.crypto.subtle={digest:()=>{entered.resolve();return gate.promise;}};
        else f.client.uploadFile=async request=>{f.calls.push(['upload',request]);entered.resolve();return gate.promise;};
        const work=f.controller.upload('chat','',selectedFile(),f.controller.read('chat'));
        await entered.promise;
        f.controller.suspend();f.controller.resume('human');
        gate.resolve(phase==='hash'?new Uint8Array(32).buffer:{file:fileInfo});
        await assert.rejects(work,/interrupted/);
        const stored=f.controller.read('chat');assert.equal(stored.files,undefined);
        assert.equal(f.calls.length,phase==='hash'?0:1);
        assert.equal(!!stored.upload,phase==='response');
    }
});
test('server attachment capabilities and limits are checked before reading or uploading a file', async () => {
    const f=fixture();fileFixture(f);let reads=0;
    const file={name:'large.txt',size:2000,arrayBuffer:async()=>{reads++;return new ArrayBuffer(2000);}};
    await assert.rejects(f.controller.upload('chat','',file,f.controller.read('chat')),/upload limit/);
    f.attachmentPolicy.enabled=false;
    await assert.rejects(f.controller.upload('chat','',selectedFile(),f.controller.read('chat')),/unavailable/);
    assert.equal(reads,0);assert.equal(f.calls.length,0);
});
test('zero-byte files fail before persistence, hashing or any upload request',async()=>{
    const f=fixture();fileFixture(f);let reads=0;
    const empty={name:'empty.txt',size:0,arrayBuffer:async()=>{reads++;return new ArrayBuffer(0);}};
    await assert.rejects(f.controller.upload('chat','',empty,f.controller.read('chat')),/Empty files/);
    assert.equal(reads,0);assert.equal(f.values.size,0);assert.equal(f.calls.length,0);
});
test('explicitly removing an uncertain upload preserves completed files and text without canceling or changing a pending post',async()=>{
    const f=fixture();fileFixture(f);await f.draft('Keep this text');
    const staged=(await f.controller.upload('chat','',selectedFile(),f.controller.read('chat'))).draft;
    f.client.uploadFile=async request=>{f.calls.push(['lost-upload',request]);throw Error('Response lost');};
    await assert.rejects(f.controller.upload('chat','',selectedFile('def','second.txt'),staged),/Response lost/);
    const uncertain=f.controller.read('chat'),op=uncertain.upload.op_id;
    const removed=await f.controller.abandonUpload('chat','',uncertain);
    assert.equal(removed.text,'Keep this text');assert.deepEqual(removed.files,[fileInfo]);assert.equal(removed.upload,undefined);
    assert.deepEqual(f.calls.map(call=>call[0]),['upload','lost-upload'],'no cancellation or deletion request is sent');
    assert.throws(()=>new Legacy.DraftStore(f.host.localStorage,'human').finishUpload('chat','',op,{...fileInfo,id:'g'.repeat(26),name:'second.txt'}),/saved upload changed/);
    f.client.postMessage=async pending=>{f.calls.push(['post',pending]);throw Error('Post response lost');};
    await assert.rejects(f.controller.send('chat','',removed),/Post response lost/);
    const pending=f.controller.read('chat');assert.deepEqual(pending.pending.file_ids,[fileId]);
    await assert.rejects(f.controller.abandonUpload('chat','',pending),/no saved upload/);
    assert.deepEqual(f.controller.read('chat').pending,pending.pending,'a pending post is never abandoned with an upload');
});
