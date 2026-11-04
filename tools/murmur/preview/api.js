/* Cookie-authenticated human API. Mutations are explicit, bound to the signed-in user. */
(function (scope) {
    'use strict';
    class ReaderError extends Error {
        constructor(message, status = 0) { super(message); this.status = status; }
    }
    const nativeId = value => typeof value === 'string' && /^[a-z0-9]{26}$/.test(value);
    function requireId(value) {
        if (!nativeId(value)) throw new ReaderError('Invalid message or conversation link.');
        return value;
    }
    function validateFile(file) {
        if (!file || !nativeId(file.id) || typeof file.name !== 'string' || !file.name ||
            !Number.isSafeInteger(file.size) || file.size < 0 || typeof file.mime_type !== 'string') {
            throw new ReaderError('The server returned invalid attachment details.');
        }
        return file;
    }
    function validatePost(post) {
        if (!post || !nativeId(post.id) || !nativeId(post.channel_id) || !nativeId(post.user_id) ||
            (post.root_id && !nativeId(post.root_id)) || typeof post.message !== 'string' ||
            typeof post.author !== 'string' || !Number.isFinite(post.create_at)) {
            throw new ReaderError('The server returned an invalid message.');
        }
        const ids = post.file_ids || [], files = post.files || [];
        if (!Array.isArray(ids) || ids.length > 10 || ids.some(id => !nativeId(id)) || new Set(ids).size !== ids.length ||
            !Array.isArray(files) || files.length > ids.length || new Set(files.map(file => file?.id)).size !== files.length) {
            throw new ReaderError('The server returned invalid message attachments.');
        }
        for (const file of files) {
            if (file?.available === false) requireId(file.id);
            else validateFile(file);
            if (!ids.includes(file.id)) throw new ReaderError('Attachment details do not belong to this message.');
        }
        return post;
    }
    function validatePage(data, channel, root) {
        if (!data || !Array.isArray(data.posts) || (data.next_before != null && !nativeId(data.next_before))) {
            throw new ReaderError('The server returned an invalid history page.');
        }
        for (const post of data.posts) {
            validatePost(post);
            if (post.channel_id !== channel || (root && post.root_id !== root && post.id !== root)) {
                throw new ReaderError('The history response belongs to another conversation.');
            }
        }
        if (root) {
            validatePost(data.root);
            if (data.root.id !== root || data.root.root_id || data.root.channel_id !== channel) {
                throw new ReaderError('The thread does not belong to this conversation.');
            }
        }
        return data;
    }
    function mergePosts(older, newer) {
        return [...new Map([...older, ...newer].map(post => [post.id, post])).values()]
            .sort((a, b) => a.create_at - b.create_at || a.id.localeCompare(b.id));
    }
    function createClient(host = globalThis, onSessionFailure = () => {}) {
        let boundUser = '', generation = 0;
        const controllers = new Set();
        function cancel() {
            generation++;
            for (const controller of controllers) controller.abort();
            controllers.clear();
        }
        async function request(path, method = 'GET', payload, conflictIsLocal = false, {raw = false, binary = false} = {}) {
            if (method !== 'GET' && !boundUser) throw new ReaderError('Sign in before changing your workspace.', 401);
            const issued = generation, controller = new host.AbortController();
            controllers.add(controller);
            const timer = host.setTimeout(() => controller.abort(), 30000);
            const headers = {'X-Requested-With': 'XMLHttpRequest'};
            const cookie = host.document.cookie.split(';').map(x => x.trim()).find(x => x.startsWith('MMCSRF='));
            if (cookie) headers['X-CSRF-Token'] = decodeURIComponent(cookie.slice(7));
            if (boundUser) headers['X-Murmur-User-Id'] = boundUser;
            if (payload !== undefined) headers['Content-Type'] = raw ? 'application/octet-stream' : 'application/json';
            try {
                const response = await host.fetch('/plugins/murmur/operator/v1/' + path, {
                    method, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
                    headers, signal: controller.signal, ...(payload === undefined ? {} : {body: raw ? payload : JSON.stringify(payload)}),
                });
                if (issued !== generation) throw new ReaderError('Request superseded.');
                if (!response.ok) {
                    let result;
                    try { result = await response.json(); } catch { /* Keep a useful status when no JSON is available. */ }
                    if (issued !== generation) throw new ReaderError('Request superseded.');
                    const messages = {401: 'Sign in to Mattermost to open Murmur.', 403: 'This reader requires a human Mattermost system administrator.',
                        409: 'Your account changed. Reload to continue with the current account.', 404: 'This message or conversation is unavailable.'};
                    const localConflict = response.status === 409 && conflictIsLocal && typeof result?.error === 'string' && result.error.length > 0 && !/operator session changed/i.test(result.error);
                    const error = new ReaderError(localConflict ? (result?.error || 'This item changed elsewhere. Refresh before trying again.') : messages[response.status] || result?.error || `The server could not complete this request (${response.status}).`, response.status);
                    error.result = result;
                    if ([401,403].includes(response.status) || (response.status === 409 && !localConflict)) onSessionFailure(error);
                    throw error;
                }
                const cache = response.headers.get('Cache-Control') || '';
                if (!/\bprivate\b/i.test(cache) || !/\bno-store\b/i.test(cache)) {
                    const error = new ReaderError('This server needs the reader privacy update before private content can be displayed.', 426);
                    onSessionFailure(error); throw error;
                }
                const result = binary ? await response.blob() : await response.json();
                if (issued !== generation) throw new ReaderError('Request superseded.');
                return result;
            } finally { host.clearTimeout(timer); controllers.delete(controller); }
        }
        const get = path => request(path);
        const change = (path, method, payload) => request(path, method, payload, true);
        function cursorParams(before = '', query = '') {
            if (typeof before !== 'string' || before.length > 4096) throw new ReaderError('Invalid page cursor.');
            const params = new URLSearchParams({limit: '50'});
            if (before) params.set('before', before);
            if (query) params.set('q', query);
            return params;
        }
        function postPage(data) {
            if (!data || !Array.isArray(data.posts) || data.posts.length > 50 || (data.next_before != null && typeof data.next_before !== 'string')) throw new ReaderError('Invalid message page.');
            data.posts.forEach(validatePost); return data;
        }
        function suffix(before) { return '?limit=50' + (before ? '&before=' + requireId(before) : ''); }
        return {
            cancel,
            async workspace() {
                const data = await get('workspace');
                if (data?.version !== 1 || !Number.isInteger(data.revision) || !Array.isArray(data.favorites) || data.favorites.some(id => !nativeId(id)) || !Array.isArray(data.follows)) throw new ReaderError('Invalid personal workspace response.');
                return data;
            },
            favorite(channel, enabled, revision) { return change('workspace/favorites/' + requireId(channel), 'PUT', {enabled, revision}); },
            follow(root, enabled, revision) { return change('workspace/follows/' + requireId(root), 'PUT', {enabled, revision}); },
            async saved() {
                const data = await get('workspace/saved');
                if (!data || !Array.isArray(data.items) || !Number.isInteger(data.revision)) throw new ReaderError('Invalid saved-message response.');
                for (const item of data.items) {
                    if (!nativeId(item.post_id) || typeof item.note !== 'string') throw new ReaderError('Invalid saved message.');
                    if (item.post) {validatePost(item.post); if (item.post.id !== item.post_id) throw new ReaderError('Saved message does not match its link.');}
                }
                return data;
            },
            savePost(post, saved, note, revision) { return change('workspace/saved/' + requireId(post), 'PUT', {saved, ...(note === undefined ? {} : {note}), revision}); },
            async inbox(before, query = '') {
                const data = await get('workspace/inbox?' + cursorParams(before, query));
                if (!data || !Array.isArray(data.items) || !Number.isInteger(data.total) || typeof data.complete !== 'boolean') throw new ReaderError('Inbox could not be read completely. Refresh to try again.');
                for (const item of data.items) {
                    if (!nativeId(item.post_id) || !nativeId(item.channel_id) || !Array.isArray(item.reasons) || !Number.isInteger(item.revision)) throw new ReaderError('Invalid Inbox item.');
                    if (item.post) {validatePost(item.post); if (item.post.id !== item.post_id || item.post.channel_id !== item.channel_id) throw new ReaderError('Inbox message does not match its link.');}
                }
                return data;
            },
            handle(post, revision) { return change('workspace/inbox/' + requireId(post) + '/handle', 'POST', {revision}); },
            undo(post, token) { return change('workspace/inbox/' + requireId(post) + '/undo', 'POST', {token}); },
            async yourMessages(query = '', before = '') { return postPage(await get('your-messages?' + cursorParams(before, query))); },
            agentActivity() { return get('agent-activity'); },
            postMessage(payload) { return change('posts', 'POST', payload); },
            async uploadFile(pending, bytes) {
                requireId(pending?.channel_id);
                if (typeof pending.op_id !== 'string' || !pending.op_id || pending.op_id.length > 200 ||
                    typeof pending.filename !== 'string' || !pending.filename || !Number.isSafeInteger(pending.size) || pending.size <= 0 ||
                    !/^[a-f0-9]{64}$/.test(pending.sha256) || bytes?.byteLength !== pending.size) {
                    throw new ReaderError('Invalid saved upload request.');
                }
                const params = new URLSearchParams({op_id: pending.op_id, channel_id: pending.channel_id,
                    filename: pending.filename, size: String(pending.size), sha256: pending.sha256});
                const data = await request('files?' + params, 'POST', bytes, true, {raw: true});
                validateFile(data?.file);
                if (data.channel_id !== pending.channel_id || data.file.name !== pending.filename || data.file.size !== pending.size) throw new ReaderError('Upload confirmation does not match the selected file.');
                return data;
            },
            async downloadFile(post, file) { return request('posts/' + requireId(post) + '/files/' + requireId(file), 'GET', undefined, false, {binary: true}); },
            operation(id) {
                if (typeof id !== 'string' || !/^[A-Za-z0-9_-]{1,200}$/.test(id)) throw new ReaderError('Invalid operation identifier.');
                return request('operations/' + encodeURIComponent(id), 'GET', undefined, true);
            },
            openDM(payload) { return change('dms', 'POST', payload); },
            async bootstrap() {
                const data = await get('bootstrap');
                if (!data || !nativeId(data.user?.id) || typeof data.user.username !== 'string' ||
                    !Array.isArray(data.agents) || !Array.isArray(data.conversations)) throw new ReaderError('Invalid workspace response.');
                if (boundUser && data.user.id !== boundUser) throw new ReaderError('Your account changed. Reload to continue.', 409);
                for (const agent of data.agents) if (typeof agent.id !== 'string' || typeof agent.name !== 'string') throw new ReaderError('Invalid agent identity.');
                for (const chat of data.conversations) {
                    if (!nativeId(chat.id) || typeof chat.name !== 'string' || !['dm', 'channel', 'group'].includes(chat.kind)) throw new ReaderError('Invalid conversation.');
                    if (chat.dm_user_ids !== undefined && (chat.kind !== 'dm' || !Array.isArray(chat.dm_user_ids) ||
                        chat.dm_user_ids.length !== 2 || chat.dm_user_ids.some(id => !nativeId(id)) || chat.dm_user_ids[0] === chat.dm_user_ids[1])) {
                        throw new ReaderError('Invalid direct-message participants.');
                    }
                }
                boundUser = data.user.id;
                return data;
            },
            async board(channel, before) {
                const data = validatePage(await get('conversations/' + requireId(channel) + '/board' + suffix(before)), channel);
                if (data.posts.some(p => p.root_id || !Number.isInteger(p.reply_count) || p.reply_count < 0)) throw new ReaderError('Invalid root-message board.');
                return data;
            },
            async thread(channel, root, before) {
                requireId(channel);
                return validatePage(await get('threads/' + requireId(root) + suffix(before)), channel, root);
            },
            async context(postId) {
                const data = await get('context/' + requireId(postId));
                validatePost(data?.post); validatePost(data?.root);
                if (data.post.id !== postId || data.root.root_id || data.post.channel_id !== data.root.channel_id ||
                    (data.post.root_id || data.post.id) !== data.root.id) throw new ReaderError('Invalid message context.');
                return data;
            },
            async search(query, {conversation = '', root = '', before = ''} = {}) {
                query = String(query).trim();
                if (!query || [...query].length > 256) throw new ReaderError('Enter a search phrase of 1–256 characters.');
                if (conversation) requireId(conversation);
                if (root) requireId(root);
                if (typeof before !== 'string' || before.length > 4096) throw new ReaderError('Invalid search cursor.');
                const params = new URLSearchParams({q: query, limit: '50'});
                if (conversation) params.set('conversation', conversation);
                if (root) params.set('root', root);
                if (before) params.set('before', before);
                const data = await get('search?' + params);
                if (!data || !Array.isArray(data.posts) || data.posts.length > 50 ||
                    (data.next_before != null && (typeof data.next_before !== 'string' || !data.next_before || data.next_before.length > 4096))) {
                    throw new ReaderError('The server returned an invalid search page.');
                }
                for (const post of data.posts) {
                    validatePost(post);
                    if ((conversation && post.channel_id !== conversation) || (root && (post.root_id || post.id) !== root)) {
                        throw new ReaderError('Search results belong to another conversation or thread.');
                    }
                }
                return data;
            },
        };
    }
    const api = {createClient, ReaderError, validatePost, validatePage, mergePosts, nativeId};
    if (typeof module !== 'undefined') module.exports = api;
    else scope.MurmurReader = api;
})(globalThis);
