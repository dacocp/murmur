/* The current console and this companion share one draft and operation authority. */
(function (scope, factory) {
    'use strict';
    const model = typeof module !== 'undefined' && module.exports ? require('../webapp/model.js') : scope.MurmurOperatorModel;
    const value = factory(model);
    if (typeof module !== 'undefined' && module.exports) module.exports = value;
    else scope.MurmurCompose = value;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (Model) {
    'use strict';
    function create({host = globalThis, client, userId, isActive = () => true, attachmentPolicy = () => null}) {
        if (!Model || typeof userId !== 'string' || !userId) throw new Error('A verified human account is required.');
        const drafts = new Model.DraftStore(host.localStorage, userId);
        const openings = new Model.OpenStore(host.localStorage, userId);
        const retries = new Set();
        let active = true, generation = 0;
        function assertActive() {
            if (!active || !isActive()) throw new Error('Sign in again before continuing. Your saved text is retained.');
        }
        function availability() {
            if (!host.navigator?.locks) return {enabled: false, reason: 'Sending needs a browser with Web Locks support.'};
            if (host.navigator?.standalone || host.matchMedia?.('(display-mode: standalone)').matches) {
                return {enabled: false, reason: 'Open Murmur in a browser tab to send. Installed-app send recovery is not available yet.'};
            }
            return {enabled: true, reason: ''};
        }
        function assertWritable() {
            assertActive();
            const mode = availability();
            if (!mode.enabled) throw new Error(mode.reason);
        }
        function attachmentAvailability() {
            const policy = attachmentPolicy();
            if (policy?.enabled !== true || !Number.isSafeInteger(policy.max_file_size) || policy.max_file_size < 1 ||
                !Number.isInteger(policy.max_files_per_post) || policy.max_files_per_post < 1 || policy.max_files_per_post > 10) {
                return {enabled: false, reason: 'File attachments are unavailable on this server.'};
            }
            return {...policy, reason: ''};
        }
        function requireAttachments(file, count) {
            const policy = attachmentAvailability();
            if (!policy.enabled) throw new Error(policy.reason);
            if (file && file.size > policy.max_file_size) throw new Error('The file exceeds this server’s '+policy.max_file_size+' byte upload limit.');
            if (count > policy.max_files_per_post) throw new Error('This server permits at most '+policy.max_files_per_post+' attachments per message.');
        }
        function destination(channel, root) {
            if (typeof channel !== 'string' || !channel || channel.includes(':') ||
                typeof root !== 'string' || root.includes(':')) throw new Error('Invalid draft destination.');
        }
        function read(channel, root = '') {
            assertActive(); destination(channel, root);
            return drafts.read(channel, root);
        }
        function list() {
            assertActive();
            const result = [], storage = host.localStorage;
            for (let n = 0; n < storage.length; n++) {
                const key = storage.key(n);
                if (!key?.startsWith(drafts.prefix)) continue;
                const parts = key.slice(drafts.prefix.length).split(':');
                if (parts.length !== 2 || !parts[0]) continue;
                const entry = read(parts[0], parts[1]);
                if (entry.text || entry.pending || entry.upload || Model.draftFiles(entry).length) result.push({channel: parts[0], root: parts[1], ...entry});
            }
            return result;
        }
        function locked(store, channel, root, action) {
            assertWritable();
            const issued = generation;
            return Model.withDraftLock(host, store, channel, root, () => {
                assertWritable();
                if (issued !== generation) throw new Error('This action was interrupted. Review the saved draft or opening and try again.');
                return action();
            });
        }
        function assertVisible(entry, visible) {
            // Every user action is tied to what that window actually displayed.
            if (!visible) throw new Error('Review the saved draft before continuing.');
            Model.assertVisibleDraft(entry, visible);
        }
        function save(channel, root, text, visible) {
            destination(channel, root);
            if (typeof text !== 'string') throw new Error('Draft text must be a string.');
            return locked(drafts, channel, root, () => {
                assertVisible(drafts.read(channel, root), visible);
                return drafts.edit(channel, root, text);
            });
        }
        function ensurePending(channel, root, visible) {
            const entry = drafts.read(channel, root);
            assertVisible(entry, visible);
            if (!entry.pending) throw new Error('There is no saved send to check.');
            return entry;
        }
        function validPost(post, pending) {
            return post && typeof post.id === 'string' && post.id && post.user_id === userId &&
                post.channel_id === pending.channel_id && (post.root_id || '') === pending.root_id &&
                post.message === pending.message && Model.sameFileIds(post.file_ids, pending.file_ids);
        }
        function settle(channel, root, entry, result, fromPost) {
            const pending = entry.pending;
            let postId;
            if (fromPost) {
                if (!validPost(result?.post, pending)) throw new Error('Unexpected send confirmation. Check the saved send before trying again.');
                postId = result.post.id;
            } else if (result?.status === 'complete') {
                if (result.result?.channel_id !== channel || typeof result.result?.post_id !== 'string' || !result.result.post_id) {
                    throw new Error('Unexpected recovery confirmation. The original send is still saved.');
                }
                postId = result.result.post_id;
            }
            if (postId) {
                retries.delete(pending.op_id);
                return {draft: drafts.complete(channel, root, pending.op_id), result, postId};
            }
            if (Model.isRejectedSend(result, pending.op_id)) {
                retries.delete(pending.op_id);
                return {draft: drafts.reject(channel, root, pending.op_id, result), result};
            }
            return {draft: drafts.read(channel, root), result};
        }
        async function dispatch(channel, root, entry) {
            assertWritable();
            try { return settle(channel, root, entry, await client.postMessage(entry.pending), true); }
            catch (error) {
                if (Model.isRejectedSend(error.result, entry.pending.op_id)) return settle(channel, root, entry, error.result, false);
                throw error;
            }
        }
        function send(channel, root, visible) {
            destination(channel, root);
            return locked(drafts, channel, root, async () => {
                const saved = drafts.read(channel, root);
                assertVisible(saved, visible);
                if (saved.pending) throw new Error('The original send is saved. Check its status before sending again.');
                if (Model.draftFiles(saved).length) requireAttachments(null, Model.draftFiles(saved).length);
                if ([...saved.text].length > 16000) throw new Error('Keep the message to 16,000 characters or fewer.');
                const entry = drafts.prepare(channel, root, host.crypto.randomUUID(), visible, true);
                return dispatch(channel, root, entry);
            });
        }
        function recover(channel, root, visible) {
            destination(channel, root);
            return locked(drafts, channel, root, async () => {
                const entry = ensurePending(channel, root, visible);
                const issued = generation;
                retries.delete(entry.pending.op_id);
                try { return settle(channel, root, entry, await client.operation(entry.pending.op_id), false); }
                catch (error) {
                    if (error.status !== 404) throw error;
                    if (issued !== generation) throw error;
                    assertWritable();
                    retries.add(entry.pending.op_id);
                    return {draft: entry, result: null, retryAllowed: true};
                }
            });
        }
        function retry(channel, root, visible) {
            destination(channel, root);
            return locked(drafts, channel, root, async () => {
                const entry = ensurePending(channel, root, visible);
                if (!retries.delete(entry.pending.op_id)) throw new Error('Check the original send before retrying it.');
                if (entry.rejected) throw new Error('Restore or discard the rejected send before posting again.');
                return dispatch(channel, root, entry);
            });
        }
        function releaseRejected(channel, root, visible, discard = false) {
            destination(channel, root);
            return locked(drafts, channel, root, () => {
                assertVisible(drafts.read(channel, root), visible);
                return drafts.releaseRejected(channel, root, visible, discard);
            });
        }
        async function fileBytes(file) {
            if (!file || typeof file.name !== 'string' || !file.name || !Number.isSafeInteger(file.size) || file.size < 0 ||
                typeof file.arrayBuffer !== 'function') throw new Error('Choose a file to attach.');
            if (file.size === 0) throw new Error('Empty files cannot be attached.');
            if (!host.crypto?.subtle) throw new Error('File uploads require a secure browser connection.');
            const bytes = await file.arrayBuffer();
            if (bytes.byteLength !== file.size) throw new Error('The file changed while it was being read. Choose it again.');
            const hash = await host.crypto.subtle.digest('SHA-256', bytes);
            return {bytes, sha256: [...new Uint8Array(hash)].map(value => value.toString(16).padStart(2, '0')).join('')};
        }
        function uploadedFile(value) {
            return value && {id: value.id, name: value.name, size: value.size, mime_type: value.mime_type};
        }
        async function dispatchUpload(channel, root, entry, bytes) {
            assertWritable();
            const issued = generation, request = entry.upload;
            try {
                const result = await client.uploadFile(request, bytes);
                if (issued !== generation) throw new Error('Upload interrupted. Check the saved upload before continuing.');
                assertWritable();
                if (result?.channel_id !== channel) throw new Error('Upload confirmation belongs to another conversation. Check the saved upload.');
                return {draft: drafts.finishUpload(channel, root, request.op_id, uploadedFile(result?.file)), result};
            } catch (error) {
                if (issued === generation && Model.isRejectedSend(error.result, request.op_id)) {
                    assertWritable();
                    return {draft: drafts.rejectUpload(channel, root, request.op_id, error.result), result: error.result};
                }
                throw error;
            }
        }
        function upload(channel, root, file, visible) {
            destination(channel, root);
            return locked(drafts, channel, root, async () => {
                const issued = generation, saved = drafts.read(channel, root);
                assertVisible(saved, visible);
                if (saved.pending || saved.upload) throw new Error('Check the original send or upload before attaching another file.');
                requireAttachments(file, Model.draftFiles(saved).length + 1);
                const {bytes, sha256} = await fileBytes(file);
                if (issued !== generation) throw new Error('File selection interrupted. Choose the file again.');
                assertWritable();
                const request = {op_id: host.crypto.randomUUID(), channel_id: channel, filename: file.name, size: file.size, sha256};
                const entry = drafts.prepareUpload(channel, root, request, visible);
                return dispatchUpload(channel, root, entry, bytes);
            });
        }
        function pendingUpload(channel, root, visible) {
            const entry = drafts.read(channel, root);
            assertVisible(entry, visible);
            if (!entry.upload) throw new Error('There is no saved upload to check.');
            return entry;
        }
        function recoverUpload(channel, root, visible) {
            return locked(drafts, channel, root, async () => {
                const entry = pendingUpload(channel, root, visible), issued = generation;
                retries.delete(entry.upload.op_id);
                try {
                    const result = await client.operation(entry.upload.op_id);
                    if (issued !== generation) throw new Error('Upload check interrupted. Check the saved upload again.');
                    assertWritable();
                    if (result?.status === 'complete') {
                        if (result.result?.channel_id !== channel) throw new Error('Unexpected upload recovery confirmation.');
                        return {draft: drafts.finishUpload(channel, root, entry.upload.op_id, uploadedFile(result.result.file)), result};
                    }
                    if (Model.isRejectedSend(result, entry.upload.op_id)) {
                        return {draft: drafts.rejectUpload(channel, root, entry.upload.op_id, result), result};
                    }
                    return {draft: entry, result};
                } catch (error) {
                    if (error.status !== 404 || issued !== generation) throw error;
                    assertWritable();
                    retries.add(entry.upload.op_id);
                    return {draft: entry, uploadRetryAllowed: true};
                }
            });
        }
        function retryUpload(channel, root, file, visible) {
            return locked(drafts, channel, root, async () => {
                const entry = pendingUpload(channel, root, visible), issued = generation;
                if (entry.uploadRejected || !retries.has(entry.upload.op_id)) throw new Error('Check the original upload before retrying it.');
                requireAttachments(file, Model.draftFiles(entry).length + 1);
                const {bytes, sha256} = await fileBytes(file);
                if (issued !== generation) throw new Error('File selection interrupted. Check the saved upload again.');
                assertWritable();
                if (file.name !== entry.upload.filename || file.size !== entry.upload.size || sha256 !== entry.upload.sha256) {
                    throw new Error('Choose the same original file to retry this upload.');
                }
                retries.delete(entry.upload.op_id);
                return dispatchUpload(channel, root, entry, bytes);
            });
        }
        function removeFile(channel, root, id, visible) {
            return locked(drafts, channel, root, () => drafts.removeFile(channel, root, id, visible));
        }
        function releaseUpload(channel, root, visible) {
            return locked(drafts, channel, root, () => drafts.releaseUpload(channel, root, visible));
        }
        function abandonUpload(channel, root, visible) {
            return locked(drafts, channel, root, () => {
                const entry = pendingUpload(channel, root, visible);
                const draft = drafts.abandonUpload(channel, root, visible);
                retries.delete(entry.upload.op_id);
                return draft;
            });
        }
        function readDM(agent) {
            assertActive();
            if (typeof agent !== 'string' || !agent) throw new Error('Choose an exact agent identity.');
            return openings.read(agent);
        }
        function listDM() {
            assertActive();
            const result = [], storage = host.localStorage, prefix = `murmur.operator.open.${userId}.`;
            for (let n = 0; n < storage.length; n++) {
                const key = storage.key(n);
                if (!key?.startsWith(prefix)) continue;
                const agent = key.slice(prefix.length), entry = readDM(agent);
                if (entry) result.push({agent, ...entry});
            }
            return result;
        }
        function checkOpening(agent, visible) {
            const entry = readDM(agent);
            if (!visible || !entry || entry.op_id !== visible.op_id || entry.status !== visible.status) {
                throw new Error('The saved DM opening changed in another window. Select the agent again.');
            }
            return entry;
        }
        function finishOpening(agent, entry, result, fromOpen) {
            let conversation;
            if (fromOpen) conversation = result?.conversation;
            else if (result?.status === 'complete') conversation = {id: result.result?.channel_id, kind: 'dm', name: 'Direct message'};
            if (conversation) {
                if (typeof conversation.id !== 'string' || !conversation.id || conversation.kind !== 'dm') {
                    throw new Error('Unexpected DM opening result. Check the saved opening.');
                }
                retries.delete(entry.op_id);
                return openings.finish(agent, entry.op_id, {status: 'complete', conversation});
            }
            if (fromOpen) throw new Error('Unexpected DM opening result. Check the saved opening.');
            if (result?.status === 'failed') {
                retries.delete(entry.op_id);
                return openings.finish(agent, entry.op_id, {status: 'failed', error: result.error || 'Opening failed.'});
            }
            return entry;
        }
        function openDM(agent) {
            readDM(agent);
            return locked(openings, agent, '', async () => {
                const {entry, created} = openings.prepare(agent, host.crypto.randomUUID());
                if (!created) return entry;
                return finishOpening(agent, entry, await client.openDM({op_id: entry.op_id, agent_id: agent}), true);
            });
        }
        function recoverDM(agent, visible) {
            return locked(openings, agent, '', async () => {
                const entry = checkOpening(agent, visible);
                if (entry.status !== 'pending') return {entry};
                const issued = generation;
                retries.delete(entry.op_id);
                try { return {entry: finishOpening(agent, entry, await client.operation(entry.op_id), false)}; }
                catch (error) {
                    if (error.status !== 404) throw error;
                    if (issued !== generation) throw error;
                    assertWritable();
                    retries.add(entry.op_id);
                    return {entry, retryAllowed: true};
                }
            });
        }
        function retryDM(agent, visible) {
            return locked(openings, agent, '', async () => {
                const entry = checkOpening(agent, visible);
                if (entry.status !== 'pending' || !retries.delete(entry.op_id)) throw new Error('Check the saved DM opening before retrying it.');
                return {entry: finishOpening(agent, entry, await client.openDM({op_id: entry.op_id, agent_id: agent}), true)};
            });
        }
        return {availability, attachmentAvailability, read, list, save, send, recover, retry, releaseRejected,
            upload, recoverUpload, retryUpload, removeFile, releaseUpload, abandonUpload, readDM, listDM, openDM, recoverDM, retryDM,
            suspend() { active = false; generation++; retries.clear(); },
            resume(id) { if (id !== userId) throw new Error('Your account changed. Reload before continuing.'); active = true; },
        };
    }
    return {create};
});
