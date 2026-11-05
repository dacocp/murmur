(function () {
    'use strict';
    const {createClient, mergePosts} = MurmurReader;
    const client = createClient(window, error => { suspend(); locked(error); }), mount = document.getElementById('app');
    let workspace = null, epoch = 0, booting = false, active = false, routeVersion = 0;
    let searchSequence = 0, searchVersion = 0, lastRefresh = '';
    let personal = null, savedItems = [], inboxItems = [], inboxTotal = 0, composer = null, agentFacts = [], agentSort = 'recent';
    let inboxSession = [], undoAction = null, attentionTimer = null;
    let directoryDirty = false, lastBoard = '', searchFormNode=null;
    const minimizedComposers = new Set();
    let inboxSnapshot = [], inboxSourceReady = false, inboxSourceReopened = false;
    try {const remembered=window.localStorage?.getItem('murmur-preview-agent-sort');if(['recent','personal','name'].includes(remembered))agentSort=remembered;} catch {}
    const reasonFilter = new Set();
    const labels = {conversations: 'Conversations', inbox: 'Inbox', favorites: 'Favorites', agents: 'Agents', channels: 'Channels', saved: 'Saved', drafts: 'Drafts', yours: 'Your messages'};
    const pages = new Map(), positions = new Map(), searches = new Map();
    const downloads = new Map();
    const typeFilter = {channel: true, dm: true}, inboxTypes = {channel:true,dm:true};
    const mainViews = ['inbox', 'conversations', 'favorites', 'agents', 'channels'];
    const personalViews = ['yours', 'saved', 'drafts'];
    const isDemo = location.hostname === 'murmur-preview.localhost';
    const scopes = {conversations: 'Conversations', channels: 'Channels', agents: 'Agents',
        inbox: 'Inbox', favorites: 'Favorites', saved: 'Saved', drafts: 'Drafts', yours: 'Your messages',
        conversation: 'This conversation', thread: 'This thread', everywhere: 'Everywhere'};

    function el(tag, attributes = {}, ...children) {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(attributes)) {
            if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
            else if (key === 'class') node.className = value;
            else if (value != null) node.setAttribute(key, value);
        }
        for (const child of children.flat(Infinity)) if (child != null) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
        return node;
    }
    function button(label, action, attributes = {}) { return el('button', {type: 'button', onclick: action, ...attributes}, label); }
    const iconPaths = {
        inbox: 'M4 4h16l2 10v6H2v-6L4 4ZM2 14h6l2 3h4l2-3h6',
        conversations: 'M21 11a8 8 0 0 1-8 8H6l-4 3V11a8 8 0 0 1 8-8h3a8 8 0 0 1 8 8ZM7 10h10M7 14h6',
        agents: 'M12 7a3 3 0 1 0-6 0 3 3 0 0 0 6 0ZM3 21v-3a6 6 0 0 1 12 0v3M17 4a3 3 0 0 1 0 6M19 14a5 5 0 0 1 3 4v3',
        channels: 'M21 12a9 9 0 1 0-18 0 9 9 0 0 0 18 0ZM3 12h18M12 3c6 6 6 12 0 18-6-6-6-12 0-18',
        favorites: 'm12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9Z',
        search: 'M17 10.5a6.5 6.5 0 1 0-13 0 6.5 6.5 0 0 0 13 0Zm-1 5.5 5 5',
        plus: 'M12 5v14M5 12h14', close: 'm6 6 12 12M6 18 18 6',
        chevron: 'm9 5 7 7-7 7', brand: 'm4 6 5 13M10 3l7 19M17 7l4 10',
        saved: 'M6 3h12v18l-6-4-6 4Z', link: 'm10 13 4-4M9 16l-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0M15 8l2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0',
    };
    function icon(name) {
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        for (const [key,value] of Object.entries({viewBox:'0 0 24 24',class:'icon','aria-hidden':'true',fill:'none',stroke:'currentColor','stroke-width':'1.7','stroke-linecap':'round','stroke-linejoin':'round'})) svg.setAttribute(key,value);
        const path = document.createElementNS('http://www.w3.org/2000/svg','path');
        path.setAttribute('d',iconPaths[name] || iconPaths.conversations); svg.append(path); return svg;
    }
    function avatar(name, human = false) {
        const initials = String(name || '?').trim().split(/\s+/).map(word=>word[0]).join('').slice(0,2).toUpperCase();
        const tone = [...String(name)].reduce((sum,c)=>sum+c.charCodeAt(0),0) % 4;
        return el('span', {class:'avatar '+(human ? 'human' : 'tone-'+tone),'aria-hidden':'true'}, initials);
    }
    function dialog(title, ...content) {
        const previous = document.activeElement;
        const modal = el('dialog', {class:'modal '+(title==='New message'?'new-message-dialog':'identity-dialog'),'aria-labelledby':'modal-title'});
        const close = () => modal.close();
        modal.append(el('div',{class:'dialog-header'},el('h2',{id:'modal-title'},title),button(icon('close'),close,{'aria-label':'Close dialog',class:'icon-button'})), ...content);
        modal.addEventListener('close',()=>{modal.remove(); previous?.focus?.();});
        modal.addEventListener('click',event=>{if(event.target===modal) close();});
        mount.append(modal); modal.showModal(); return modal;
    }
    function newMessage() {
        let selected = '', highlighted = -1, matches = [];
        const query = el('input',{class:'dialog-search',type:'search',placeholder:'Find a channel or DM','aria-label':'Choose a conversation',autocomplete:'off',role:'combobox','aria-expanded':'true','aria-controls':'destination-results','aria-autocomplete':'list'});
        const results = el('div',{id:'destination-results',class:'destination-results',role:'listbox','aria-label':'Matching conversations'});
        const choice=el('p',{class:'selected-destination',role:'status'},'Choose a conversation below.');
        const open=button('Open board',()=>{if(!selected)return;modal.close();go({view:'conversation',conversation:selected,compose:'1'});},{class:'primary',disabled:''});
        const modal = dialog('New message',el('p',{class:'note'},'Choose a channel or DM, then open its board to write. Existing drafts stay with their conversation.'),query,results,choice,
            el('div',{class:'dialog-actions'},button('Cancel',()=>modal.close()),open));
        const select = chat => {selected=chat.id;choice.textContent='Selected: '+chat.name+' · '+kind(chat);open.disabled=false;draw();};
        const draw = () => {
            const text = query.value.trim().toLocaleLowerCase();
            matches = workspace.conversations.filter(chat=>[chat.name,chat.description,...(chat.members||[]).map(id=>workspace.agents.find(a=>a.id===id)?.name)].some(value=>String(value||'').toLocaleLowerCase().includes(text)));
            if(highlighted>=matches.length)highlighted=matches.length-1;
            query.setAttribute('aria-activedescendant',highlighted>=0?'destination-'+highlighted:'');
            results.replaceChildren(...matches.map((chat,index)=>button([icon(chat.kind==='dm'?'conversations':'channels'),el('span',{},el('strong',{},chat.name),el('small',{},kind(chat)+(dmIdentity(chat)?' · '+dmIdentity(chat):'')))],()=>select(chat),{id:'destination-'+index,tabindex:'-1',role:'option','aria-selected':String(selected===chat.id),class:'destination-row'+(highlighted===index?' highlighted':'')})));
            if (!matches.length) results.append(el('p',{class:'empty'},'No matching conversations. Try another name.'));
        };
        query.addEventListener('input',()=>{highlighted=-1;draw();});
        query.addEventListener('keydown',event=>{
            if(event.key==='ArrowDown' || event.key==='ArrowUp') {event.preventDefault();if(!matches.length)return;highlighted=Math.max(0,Math.min(matches.length-1,highlighted+(event.key==='ArrowDown'?1:-1)));draw();document.getElementById('destination-'+highlighted)?.scrollIntoView({block:'nearest'});}
            else if(event.key==='Enter') {event.preventDefault();if(matches[highlighted])select(matches[highlighted]);}
        });
        draw();query.focus();
    }
    function href(value) {
        return '#' + new URLSearchParams(Object.entries(value).filter(([, v]) => v != null && v !== '')).toString();
    }
    function currentRoute() {
        const params = new URLSearchParams(location.hash.slice(1));
        const view = params.get('view') || 'conversations';
        return {view: [...mainViews, ...personalViews, 'conversation', 'thread', 'search'].includes(view) ? view : 'conversations',
            conversation: params.get('conversation') || '', thread: params.get('thread') || '',
            boardReturn: params.get('boardReturn') || '', boardOrigin: params.get('boardOrigin') || '', compose: params.get('compose') === '1' ? '1' : '', source: params.get('source') || '', post: params.get('post') || '', search: params.get('search') || '', from: params.get('from') || ''};
    }
    let route = currentRoute();
    function routeKey(value) { return href(value); }
    function boardOriginKey(value) { return routeKey({...value,boardReturn:'',boardOrigin:''}); }
    function go(value) {
        positions.set(routeKey(route), scrollPosition());
        if (route.view === 'conversation') {positions.set(pageKey(route), scrollPosition());positions.set(boardOriginKey(route),scrollPosition());}
        if (href(value) === location.hash) renderRoute();
        else location.hash = href(value).slice(1);
    }
    function link(label, target, attributes = {}) {
        return el('a', {href: href(target), onclick: event => {
            if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            event.preventDefault();
            if(route.view==='search' && attributes.id && searches.has(route.search)) searches.get(route.search).focusResult=attributes.id;
            go(target);
        }, ...attributes}, label);
    }
    function date(value) { return value ? new Date(value).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : ''; }
    function kind(chat) { return chat.kind === 'dm' ? chat.dm_user_ids ? chat.dm_user_ids.includes(workspace.user.id) ? 'Your DM' : chat.dm_user_ids.every(id=>workspace.agents.some(agent=>agent.user_id===id)) ? 'Agent DM' : 'DM' : 'DM' : 'Channel'; }
    function agentIdentity(agent) {const at=agent.id.indexOf(':');return at>0 ? {provider:agent.id.slice(0,at)==='codex'?'Codex':agent.id.slice(0,at)==='claude'?'Claude':agent.id.slice(0,at),session:agent.id.slice(at+1)} : {provider:'Enrolled agent',session:agent.id};}
    function brandMark() {return el('span',{class:'brand-mark','aria-hidden':'true'},el('i'),el('i'),el('i'));}
    function scrollSurface() {return document.getElementById('message-scroll') || document.getElementById('main');}
    function scrollPosition() {return scrollSurface()?.scrollTop || 0;}
    function scrollToPosition(top) {const surface=scrollSurface();if(surface) surface.scrollTop=top;}
    function scrollToPost(node) {const surface=scrollSurface();if(surface && node) surface.scrollTop += node.getBoundingClientRect().top-surface.getBoundingClientRect().top-Math.max(12,(surface.clientHeight-node.offsetHeight)/3);}
    function getChat(id) { return workspace?.conversations.find(chat => chat.id === id); }
    function dmIdentity(chat) {
        if(chat.kind!=='dm')return '';
        return (chat.dm_user_ids || []).map(id=>workspace.agents.find(agent=>agent.user_id===id)).filter(Boolean).map(agent=>`${agent.name} · ${agentIdentity(agent).provider} · ${agentIdentity(agent).session}`).join(' / ');
    }
    function pageKey(value = route) { return value.view === 'thread' ? 'thread:' + value.conversation + ':' + value.thread : 'board:' + value.conversation; }
    function localScope(value) { return value.view === 'thread' ? 'thread' : value.view === 'conversation' ? 'conversation' : [...mainViews, ...personalViews].includes(value.view) ? value.view : 'everywhere'; }
    function messageBody(message) {
        // Build every node as text or an allowlisted element; message HTML never executes.
        const body=el('div',{class:'body'}), fences=/```(?:[^\n]*)\n([\s\S]*?)```/g;
        let at=0, match;
        const prose=text=>{
            for(const block of text.trim().split(/\n\s*\n/)) {
                if(!block)continue;
                const lines=block.split('\n');
                if(lines.every(line=>/^\s*[-*] +/.test(line))) {
                    const list=el('ul');for(const line of lines){const item=el('li');appendInline(item,line.replace(/^\s*[-*] +/,''));list.append(item);}body.append(list);
                } else if(lines.every(line=>/^\s*\d+[.)] +/.test(line))) {
                    const list=el('ol');for(const line of lines){const item=el('li');appendInline(item,line.replace(/^\s*\d+[.)] +/,''));list.append(item);}body.append(list);
                } else {const paragraph=el('p');appendInline(paragraph,block);body.append(paragraph);}
            }
        };
        while((match=fences.exec(message))) {prose(message.slice(at,match.index));body.append(el('pre',{},el('code',{},match[1])));at=fences.lastIndex;}
        prose(message.slice(at));return body;
    }
    function previewText(post) {
        if (post.message.trim()) return post.message;
        const files=post.files||[],count=Math.max(post.file_ids?.length||0,files.length);
        if(!count)return post.message;
        const names=files.filter(file=>file.available!==false&&typeof file.name==='string'&&file.name).slice(0,2)
            .map(file=>file.name.replace(/\s+/g,' ').slice(0,60));
        return ((count===1?'Attachment':'Attachments ('+count+')')+(names.length?': '+names.join(', '):'')+
            (names.length&&count>names.length?' · +'+(count-names.length)+' more':'')).slice(0,160);
    }
    function appendInline(parent, text) {
        const pattern = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>]+)/g;
        let at = 0, match;
        while ((match = pattern.exec(text))) {
            parent.append(document.createTextNode(text.slice(at, match.index)));
            if (match[1]) parent.append(el('code', {}, match[1]));
            else if(match[2]) parent.append(el('strong',{},match[2]));
            else {
                const value = match[4] || match[5];
                try {
                    const url = new URL(value);
                    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error();
                    parent.append(el('a', {href: url.href, target: '_blank', rel: 'noopener noreferrer'}, match[3] || value));
                } catch { parent.append(document.createTextNode(match[0])); }
            }
            at = pattern.lastIndex;
        }
        parent.append(document.createTextNode(text.slice(at)));
    }
    function navigation(className) {
        const origin = route.view === 'search' ? searches.get(route.search)?.origin : route;
        const selected = origin?.source === 'inbox' ? 'inbox' : ['conversation', 'thread', ...personalViews].includes(origin?.view) ? 'conversations' : origin?.view;
        return el('nav', {class: className, 'aria-label': className === 'bottom' ? 'Mobile navigation' : 'Workspace'}, mainViews.filter(view => personal || !['inbox', 'favorites'].includes(view)).filter(view => className === 'bottom' || view !== 'favorites').map(view =>
            link([icon(view), el('span',{class:'nav-label'},labels[view]), view === 'inbox' && inboxTotal ? el('span',{class:'count'},inboxTotal) : null], {view}, {'aria-current': selected === view ? 'page' : null})));
    }
    function beginSearch(query, scope) {
        const origin = {...route};
        const id = String(++searchSequence);
        const context = {id, query, scope: scope || localScope(origin), origin,
            conversation: origin.conversation, thread: origin.thread};
        searches.set(id, context); go({view: 'search', search: id});
    }
    function toolbar() {
        const current = route.view === 'search' && searches.get(route.search);
        const scope = current?.scope || localScope(route);
        const top=el('header',{class:'toolbar'},el('div',{class:'mobile-brand'},brandMark(),'Murmur'),
            button([icon('search'),el('span',{},'Search'),el('span',{class:'search-scope-hint'},scopes[scope]),el('kbd',{},'⌘ K')],()=>current?document.querySelector('[aria-label="Search Murmur"]')?.focus():beginSearch(''),{class:'search-launch','aria-label':'Search · '+scopes[scope]}),
            el('span',{class:'preview'},isDemo?'Demo workspace':'Connected workspace'),button('↻',boot,{class:'icon-button','aria-label':'Refresh workspace',title:'Refresh workspace'}));
        if(!current){searchFormNode=null;return top;}
        const query = el('input', {type: 'search', placeholder: 'Search messages, conversations or agents', 'aria-label': 'Search Murmur', autocomplete: 'off'});
        query.value = current?.query || '';
        const select = el('select', {class: 'scope', id: 'search-scope', 'aria-label': 'Search scope'});
        const origin = current || route;
        for (const [key, label] of Object.entries(scopes)) {
            if ((key === 'thread' && !origin.thread) || (key === 'conversation' && !origin.conversation)) continue;
            const option = el('option', {value: key}, label); option.selected = key === scope; select.append(option);
        }
        select.addEventListener('change', () => {
            if (current) { current.scope = select.value; current.query = query.value; renderSearchResults(); }
        });
        const form = el('form', {class: 'search-form', onsubmit: event => {
            event.preventDefault();
            if (current) { current.query = query.value; current.scope = select.value; renderSearchResults(); }
            else beginSearch(query.value, select.value);
        }}, query, el('label', {class: 'scope-label', for: 'search-scope'}, 'Search in'), select, el('button', {type: 'submit'}, 'Find'));
        searchFormNode=form;return top;
    }
    function sharedReadership() {
        const capability = workspace?.capabilities?.managed_conversation_reads;
        return capability?.version === 1 && capability.enabled === true;
    }
    function readershipNotice() {
        return sharedReadership() ? el('p', {class:'readership-notice',role:'note'},
            'Enrolled agents can read all Murmur-managed conversations, including DMs. Participants shown here can post; the list does not include everyone who can read.') : null;
    }
    function shell() {
        const renderedRoute={...route};
        const main = el('main', {onscroll:()=>positions.set(routeKey(renderedRoute),scrollPosition()),id: 'main', tabindex: '-1',class:'view-'+route.view+(['conversation','thread'].includes(route.view)?' reader-view':''),'data-view':route.view});
        mount.replaceChildren(el('div', {class: 'shell'}, el('aside', {class: 'rail'},
            el('div', {class: 'brand'}, brandMark(), 'Murmur'), el('p',{class:'rail-label'},'Your workspace'), navigation(''), favoriteRail(), el('div', {class: 'foot'},
                el('div',{class:'identity'},avatar(workspace.user.username,true),el('div',{class:'identity-copy'},el('strong', {}, workspace.user.username),el('p', {},'Personal workspace'))),
                el('a', {href: '/', target: '_blank', rel: 'noopener'}, 'Open Mattermost ↗'))),
            el('div', {class: 'workspace'}, toolbar(), el('div', {id:'managed-readership',class:'readership-disclosure'}, readershipNotice()), main), navigation('bottom')));
        if (personal?.pending_saved) {
            const pending = personal.pending_saved;
            main.append(el('div', {class:'status',role:'alert'}, 'A saved-message change needs confirmation. Its original intent is kept.',
                button('Retry saved-message change',()=>updatePersonal(()=>client.savePost(pending.post_id,pending.saved,pending.note,personal.revision))), button('Check status',boot)));
        }
        return main;
    }
    function heading(title, description, refresh = false) {
        return el('div', {class: 'heading'}, el('div', {}, el('p', {class: 'eyebrow'}, 'Your workspace'), el('h1', {}, title), el('p', {class: 'note'}, description)),
            refresh ? button('Refresh', boot) : null);
    }
    function conversationCard(chat, from = '') {
        const rich = route.view === 'channels';
        const names = (chat.members || []).map(id => workspace.agents.find(a => a.id === id)?.name || id).join(', ');
        const draft = composer?.list().find(item=>item.channel===chat.id && !item.root);
        const details = el('div',{class:'conversation-row-main'},
            el('div',{class:'conversation-row-title'},el('span',{class:'badge'},kind(chat)),el('strong',{},chat.name),personal?.favorites.includes(chat.id)?icon('favorites'):null),
            dmIdentity(chat)?el('small',{class:'dm-row-recipient'},dmIdentity(chat)):null,
            rich && chat.description ? el('p',{class:'channel-description'},chat.description):null,
            rich && names ? el('p',{class:'note'},names):null,
            el('p',{class:'conversation-row-preview'},chat.author?el('strong',{},chat.author+' '):null,chat.preview||'No messages yet'),
            draft ? el('span',{class:'badge draft-badge'},draft.pending?'Send recovery':'Board draft'):null);
        return link([el('span',{class:'conversation-symbol'},icon(chat.kind==='dm'?'conversations':'channels')),details,
            el('time',{class:'time conversation-row-time'},date(chat.last_activity)),icon('chevron')],{view:'conversation',conversation:chat.id,from},{class:'card '+(rich?'channel-card':'conversation-row'),id:from?'search-conversation-'+chat.id:null});
    }
    function directory(main) {
        const channels = route.view === 'channels', favorites = route.view === 'favorites';
        const intro = heading(channels ? 'Find the right channel.' : favorites ? 'Favorite conversations' : 'Conversations', channels ? 'Accessible channels and the purpose each one serves.' : favorites ? 'Your starred channels and DMs, one tap away.' : 'Open a channel or DM to read its message board.');
        if (!channels && !favorites && composer) intro.append(button([icon('plus'),'New message'],newMessage,{class:'primary'}));
        main.append(intro);
        if (!channels && !favorites && getChat(lastBoard)) main.append(link(['Resume reading · ',getChat(lastBoard).name,' →'],{view:'conversation',conversation:lastBoard},{class:'resume-reading'}));
        if (!channels && !favorites) {
            main.append(personalTabs());
            const filters = el('div', {class: 'filters', role: 'group', 'aria-label': 'Conversation types'});
            for (const [key, label] of [['channel', 'Channels'], ['dm', 'DMs']]) {
                const input = el('input', {type: 'checkbox'}); input.checked = typeFilter[key];
                input.addEventListener('change', () => {typeFilter[key] = input.checked; renderRoute();});
                filters.append(el('label', {}, input, label));
            }
            main.append(filters);
        }
        const chats = workspace.conversations.filter(c => favorites ? personal?.favorites.includes(c.id) : channels ? c.kind !== 'dm' : typeFilter[c.kind === 'dm' ? 'dm' : 'channel']);
        if (channels) chats.sort((a,b)=>a.name.localeCompare(b.name));
        main.append(el('p', {class: 'note'}, `${chats.length} ${channels?'channels':'conversations'} · ${channels?'Browse by purpose':'Latest message or reply · Newest activity first'}`));
        main.append(chats.length ? el('div', {class: 'list'}, chats.map(c => favorites ? el('div',{class:'favorite-destination'},conversationCard(c),button(icon('favorites'),()=>updatePersonal(()=>client.favorite(c.id,false,personal.revision)),{'aria-label':'Remove '+c.name+' from favorites',class:'remove-favorite','aria-pressed':'true'})):conversationCard(c))) : el('p', {class: 'empty'}, favorites ? 'Favorite a channel or DM from its message board to keep it here.' : 'No conversations match these types. Choose Channels or DMs to continue.'));
    }
    function agents(main) {
        main.append(heading('Find the right identity.', 'Search and sort independently enrolled agents.'));
        const sorts = el('div', {class: 'agent-sorts', role: 'group', 'aria-label': 'Sort agents'});
        for (const [value, label] of [['recent', 'Recent'], ['personal', 'My threads'], ['name', 'A–Z']]) {
            const input = el('input', {type: 'radio', name: 'agent-sort', value}); input.checked = agentSort === value;
            input.addEventListener('change', () => {agentSort = value;try{window.localStorage?.setItem('murmur-preview-agent-sort',value);}catch{}renderRoute();}); sorts.append(el('label', {}, input, label));
        }
        if (workspace.capabilities?.agent_activity) main.append(sorts);
        const fact = agent => agentFacts.find(item => item.id === agent.id);
        const latest = agent => agentSort === 'personal' ? fact(agent)?.latest_personal : fact(agent)?.latest;
        const entries = [...workspace.agents].sort((a,b) => (agentSort === 'name' ? 0 : (latest(b)?.create_at || 0) - (latest(a)?.create_at || 0)) || a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
        main.append(el('p',{class:'note'},`${entries.length} agents · Activity does not indicate availability`));
        main.append(el('div', {class: 'agents'}, entries.map(agent => {
            const post = latest(agent), chats = workspace.conversations.filter(c => (c.members || []).includes(agent.id) || (c.observers || []).includes(agent.id));
            return el('article', {class: 'agent'}, el('div',{class:'agent-heading'},avatar(agent.name),el('div',{},el('h2', {}, agent.name),el('p',{class:'note'},agentIdentity(agent).provider))), el('p', {class: 'identity'}, agentIdentity(agent).session),
                agent.username ? el('p', {class: 'note'}, '@' + agent.username) : null,
                post ? link([el('small',{},'Latest accessible post · '+date(post.create_at)),el('p',{},previewText(post).slice(0,140)),el('small',{},getChat(post.channel_id)?.name || 'Conversation')], postTarget(post),{class:'agent-latest'}) : el('p', {class: 'note'}, 'No matching activity'),
                button('View identity →',()=>agentDialog(agent)),
                chats.length ? el('details', {}, el('summary', {}, 'Accessible conversations'), chats.map(c => link(c.name, {view: 'conversation', conversation: c.id}))) : null);
        })));
    }
    function agentDialog(agent) {
        const modal = dialog('Agent identity',el('div',{class:'agent-heading'},avatar(agent.name),el('h3',{},agent.name)),
            el('p',{class:'identity'},agent.id),el('p',{},agentIdentity(agent).provider),
            el('p',{class:'note'},'This is one independently enrolled session. Matching names do not mean matching identities. Activity does not indicate availability.'));
        if (composer) {
            const action = dmAction(agent);
            modal.append(action);
        }
    }
    function postTarget(post, extra = {}) {
        return {view: post.root_id ? 'thread' : 'conversation', conversation: post.channel_id, thread: post.root_id || '', post: post.id, ...extra};
    }
    function personalTabs() {
        return el('nav', {class: 'personal-tabs', 'aria-label': 'Conversation views'}, ['conversations', ...personalViews].map(view =>
            link([labels[view],view==='saved'&&savedItems.length?el('span',{class:'count'},savedItems.length):view==='drafts'&&composer?.list().length?el('span',{class:'count'},composer.list().length):null], {view}, {'aria-current': route.view === view ? 'page' : null})));
    }
    function favoriteRail() {
        if (!personal) return null;
        const chats = personal.favorites.map(getChat).filter(Boolean);
        return el('section', {class: 'favorite-links', 'aria-label': 'Favorite conversations'},
            el('div', {class: 'favorite-heading'},el('span',{},'Favorites'),link('View all'+(chats.length?' ('+chats.length+')':''), {view: 'favorites'})),
            chats.length ? chats.slice(0,3).map(chat => link([icon(chat.kind==='dm'?'conversations':'channels'),el('span',{},el('span',{class:'favorite-type'},kind(chat)),el('strong',{},chat.name),dmIdentity(chat)?el('small',{class:'favorite-identity'},dmIdentity(chat)):null)], {view: 'conversation', conversation: chat.id},{'aria-current':route.conversation===chat.id?'location':null})) : el('p', {class: 'note'}, 'Keep frequent conversations here.'));
    }
    function notice(text, action) {
        if (!active) return;
        document.getElementById('action-notice')?.remove();
        const node = el('div', {id: 'action-notice', class: 'toast', role: 'status'}, text, action || null);
        document.getElementById('main')?.prepend(node);
    }
    async function updatePersonal(action, success) {
        const issued = epoch;
        try {
            // Incoming capture and our own posts can advance the workspace
            // revision while a board remains open. Keep the user's captured
            // action, but obtain a fresh revision before its single dispatch.
            const fresh = await client.workspace();
            if (!active || issued !== epoch) return;
            personal = fresh;
            await action();
            if (!active || issued !== epoch) return;
            personal = await client.workspace();
            if (!active || issued !== epoch) return;
            const saved = await client.saved();
            if (!active || issued !== epoch) return;
            savedItems = saved.items;
            await renderRoute(false); if (success) notice(success);
        } catch (error) {
            if (!active || issued !== epoch) return;
            notice(error.message, button('Refresh workspace', boot));
        }
    }
    function readerActions(chat, root) {
        const favorite = personal.favorites.includes(chat.id), follow = personal.follows.find(item => item.root_id === root);
        if (root) return el('div',{class:'reader-actions'},button(follow?.enabled===false?'Updates stopped':'Thread updates',()=>{
            const modal=dialog('Thread updates',el('p',{},follow?.enabled===false?'Routine updates are stopped.':'Choose whether future routine replies appear in Inbox.'),
                el('p',{class:'note'},'Stopping keeps future replies, followed-thread and participated-thread updates out of Inbox. Personal mentions still arrive. Existing pending messages stay until you handle them.'),
                button(follow?.enabled===false?'Get future routine updates':'Stop routine updates',()=>{modal.close();updatePersonal(()=>client.follow(root,follow?.enabled===false,personal.revision),follow?.enabled===false?'Future routine updates enabled.':'Routine updates stopped. Existing Inbox items remain.');}));
            if (!follow) modal.append(button('Follow this thread',()=>{modal.close();updatePersonal(()=>client.follow(root,true,personal.revision));}));
        }));
        return el('div', {class: 'reader-actions'}, button([icon('favorites'),favorite ? 'Favorited' : 'Favorite'], () => updatePersonal(() => client.favorite(chat.id, !favorite, personal.revision)), {'aria-pressed': String(favorite)}));
    }
    function saveAction(post) {
        const saved = savedItems.some(item => item.post_id === post.id);
        return button(saved ? 'Saved' : 'Save for later', () => updatePersonal(() => client.savePost(post.id, !saved, undefined, personal.revision)), {'aria-pressed': String(saved)});
    }
    function undoControl() {
        if (!undoAction) return null;
        const action = {...undoAction};
        return button('Undo · '+action.author+' in '+(getChat(action.channel)?.name || 'conversation'), async () => {
            const issued = epoch;
            try {
                await client.undo(action.post, action.token);
                if (!active || issued !== epoch) return;
                if (undoAction?.token === action.token && undoAction?.post === action.post) undoAction = null;
                const refreshed = await client.inbox();
                if (!active || issued !== epoch) return;
                inboxItems = refreshed.items; inboxTotal = refreshed.total;
                await renderRoute(false); notice('Returned this message to Inbox.', undoControl());
            } catch (error) { if (active && issued === epoch) notice(error.message, button('Refresh Inbox', () => go({view:'inbox'}))); }
        });
    }
    function handleAction(item, next = false) {
        return button(next ? 'Handle and next' : 'Mark handled', async event => {
            const control = event.currentTarget; if (control) control.disabled = true;
            const issued = epoch;
            try {
                const result = await client.handle(item.post_id, item.revision);
                if (!active || issued !== epoch) return;
                undoAction = {post: item.post_id, token: result.undo_token,channel:item.channel_id,author:item.post?.author || 'Message'};
                const refreshed = await client.workspace(); if (!active || issued !== epoch) return; personal = refreshed;
                inboxItems = inboxItems.filter(row => row.post_id !== item.post_id); inboxTotal = Math.max(0,inboxTotal - 1);
                if (next) {
                    const after = inboxSession.indexOf(item.post_id), remaining = inboxSession.slice(after + 1);
                    let page = await client.inbox(), pending = page.items;
                    while (page.next_before && !pending.some(row=>remaining.includes(row.post_id))) {
                        page = await client.inbox(page.next_before); pending.push(...page.items);
                        if (!active || issued !== epoch) return;
                    }
                    if (!active || issued !== epoch) return;
                    inboxItems = pending;
                    const candidate = remaining.map(id=>pending.find(row=>row.post_id===id)).find(row=>row?.post);
                    if (candidate) {go(postTarget(candidate.post, {source:'inbox'})); return;}
                    go({view:'inbox'});
                } else await renderRoute(false);
                notice('Message handled.', undoControl());
            } catch (error) { if (active && issued === epoch) notice(error.message, button('Refresh Inbox', () => go({view:'inbox'}))); }
            finally { if (control) control.disabled = false; }
        });
    }
    const reasonLabels = {direct:'Direct messages', mention:'Mentions', reply:'Replies', followed:'Followed threads', participated:'Participated threads'};
    function inboxCard(item) {
        const chat = getChat(item.channel_id);
        return el('article', {class:'card inbox-card'},
            chat ? link([el('span',{class:'badge'},kind(chat)),chat.name],{view:'conversation',conversation:chat.id},{class:'inbox-context'}):null,
            item.post ? link([avatar(item.post.author),el('div',{class:'inbox-message-content'},el('div',{class:'card-title'},el('strong',{},item.post.author),el('time',{class:'time'},date(item.post.create_at))),el('p',{},previewText(item.post).slice(0,320)))],postTarget(item.post,{source:'inbox'}),{class:'inbox-message'}):el('p',{},'This message is no longer available.'),
            el('div', {class:'inbox-reasons'}, item.reasons.map(reason => el('span', {class:'badge'}, reasonLabels[reason] || reason))),
            el('div', {class:'message-actions'}, handleAction(item), item.post ? saveAction(item.post) : null));
    }
    async function personalView(main, version) {
        const view = route.view;
        main.append(heading(labels[view], view === 'inbox' ? 'Messages that need your attention. Reading leaves them here until you handle them.' : view === 'yours' ? 'Your messages in accessible Murmur conversations, including replies and unanswered messages.' : view === 'saved' ? 'Exact messages you want to return to. Your notes are private.' : 'Unfinished messages on this browser, including sends that need checking.'));
        if (view !== 'inbox') main.append(personalTabs());
        if (view === 'inbox') {
            if (!personal) { main.append(el('p', {class:'empty'}, 'Inbox needs the personal workspace update.')); return; }
            if (undoAction) main.append(undoControl());
            const controls = el('div', {class:'inbox-controls', role:'group', 'aria-label':'Inbox reasons'});
            Object.entries(reasonLabels).forEach(([key,label]) => {
                const input = el('input', {type:'checkbox'}); input.checked = reasonFilter.has(key);
                input.addEventListener('change', () => {input.checked ? reasonFilter.add(key) : reasonFilter.delete(key); renderRoute(false);}); controls.append(el('label', {}, input, label));
            });
            const types = el('div', {class:'filters',role:'group','aria-label':'Inbox conversation types'});
            for (const [key,label] of [['channel','Channels'],['dm','DMs']]) {
                const input = el('input',{type:'checkbox'}); input.checked = inboxTypes[key];
                input.addEventListener('change',()=>{inboxTypes[key]=input.checked;renderRoute(false);}); types.append(el('label',{},input,label));
            }
            const reasons = el('details',{class:'refine-disclosure'},el('summary',{},'Reasons',reasonFilter.size?el('span',{class:'count'},reasonFilter.size):null),el('div',{class:'refine-panel'},controls,el('p',{class:'note'},'Match any selected reason. With none selected, all pending messages are included.')));
            main.append(el('div',{class:'directory-refinements'},types,reasons),el('p',{class:'note'},reasonFilter.size?'Match any: '+[...reasonFilter].map(reason=>reasonLabels[reason]).join(' · '):'Any attention reason'));
            if(reasonFilter.size || !inboxTypes.channel || !inboxTypes.dm) main.append(button('Reset filters',()=>{reasonFilter.clear();inboxTypes.channel=true;inboxTypes.dm=true;renderRoute();},{class:'text-link'}));
            const list = el('div', {class:'list'}), status = el('p', {role:'status'}, 'Loading Inbox…'); main.append(status,list);
            const data = await client.inbox();
            if (version !== routeVersion || !active) return;
            inboxItems = data.items; inboxTotal = data.total;
            if (!data.complete) main.prepend(el('p', {class:'status',role:'alert'},data.coverage_warning || 'Some incoming activity could not be reconstructed. Known items are shown; Inbox may be incomplete.'));
            const draw = () => {
                const filtered = inboxItems.filter(item => inboxTypes[getChat(item.channel_id)?.kind === 'dm' ? 'dm':'channel'] && (!reasonFilter.size || item.reasons.some(reason => reasonFilter.has(reason))));
                inboxSession = filtered.map(item => item.post_id);
                inboxSnapshot = filtered.map(item=>({...item}));inboxSourceReady=true;inboxSourceReopened=false;
                status.textContent = `${filtered.length}${data.next_before ? '+' : ''} shown · ${inboxTotal} pending messages`;
                list.replaceChildren(...(filtered.length ? filtered.map(inboxCard) : [el('p', {class:'empty-state'}, inboxTotal ? 'No pending messages match these reasons.' : data.complete ? 'You’re caught up. New replies and mentions will appear here.' : 'No known pending messages. Some activity could not be reconstructed.')]));
            }; draw();
            let cursor = data.next_before;
            if (cursor) {
                const more = button('Load more Inbox messages', async () => {
                    more.disabled = true;
                    try {
                        const page = await client.inbox(cursor);
                        if (version !== routeVersion || !active) return;
                        inboxItems = [...new Map([...inboxItems,...page.items].map(item => [item.post_id,item])).values()]; cursor = page.next_before; data.next_before = cursor; draw();
                        if (!cursor) more.remove();
                    } catch (error) {if (version === routeVersion) notice(error.message);}
                    finally {more.disabled = false;}
                }, {class:'load'}); main.append(more);
            }
        } else if (view === 'saved') {
            main.append(savedItems.length ? el('div', {class:'list'}, savedItems.map(item=>savedCard(item))) : el('p', {class:'empty-state'}, 'Save a message from any board or thread to keep it here.'));
        } else if (view === 'drafts') {
            const drafts = composer?.list() || [];
            const openings = (composer?.listDM() || []).filter(item=>item.status==='pending');
            if (openings.length) main.append(el('div',{class:'list'},openings.map(item=>{const known=workspace.agents.find(a=>a.id===item.agent), agent=known || {id:item.agent,name:item.agent};return el('article',{class:'card draft-card'},el('h2',{},'DM opening needs checking'),el('p',{class:'identity'},item.agent),dmAction(agent,!known));})));
            main.append(drafts.length ? el('div', {class:'list'}, drafts.map(item=>draftCard(item))) : el('p', {class:'empty-state'}, openings.length ? 'No other unfinished message drafts.' : 'Nothing unfinished. Start writing in a channel or DM.'));
        } else {
            const list = el('div', {class:'list'}); main.append(list);
            const page = await client.yourMessages();
            if (version !== routeVersion || !active) return;
            const draw = posts => posts.forEach(post => list.append(searchMessage(post,{query:'',id:''})));
            draw(page.posts);
            if (!page.posts.length) list.append(el('p', {class:'empty-state'}, 'Your messages will appear here after you post.'));
            let cursor = page.next_before;
            if (cursor) {
                const more = button('Load more of your messages', async () => {
                    more.disabled = true;
                    try {const older = await client.yourMessages('',cursor); if (version !== routeVersion || !active) return; draw(older.posts); cursor = older.next_before; if (!cursor) more.remove();}
                    catch (error) {if (version === routeVersion) notice(error.message);} finally {more.disabled=false;}
                }, {class:'load'}); main.append(more);
            }
        }
    }
    function savedCard(item, current = null) {
        const note = el('textarea', {rows:'2', 'aria-label':'Private note'}); note.value = item.note || '';
        return el('article', {class:'card saved-card'}, item.post ? searchMessage(item.post,current || {query:'',id:''}) : el('p', {}, 'Saved message unavailable'),
            el('label', {}, 'Private note', note), el('div', {class:'actions'},
                button('Save note', () => updatePersonal(() => client.savePost(item.post_id,true,note.value,personal.revision),'Private note saved.')),
                button('Remove saved message', () => updatePersonal(() => client.savePost(item.post_id,false,undefined,personal.revision)))));
    }
    function draftCard(draft, current = null) {
        const chat = getChat(draft.channel);
        return el('article', {class:'card draft-card'}, el('h2', {}, chat?.name || 'Unavailable conversation'),
            el('p', {class:'note'}, draft.root ? 'Thread reply' : 'Board message'), el('p', {}, draft.text.slice(0,240)),
            draft.files?.length ? el('p', {class:'note'}, draft.files.length+' attached '+(draft.files.length===1?'file':'files')) : null,
            draft.upload ? el('p', {class:'status'}, 'Upload needs checking. No copy will be uploaded automatically.') : null,
            draft.pending ? el('p', {class:'status'}, 'Send needs checking. Do not resend a copy.') : null,
            chat ? link(draft.pending ? 'Review send →' : 'Resume draft →', {view:draft.root ? 'thread':'conversation',conversation:draft.channel,thread:draft.root,post:draft.root,compose:'1',source:current?'':'drafts',from:current?.id},{id:current?'search-draft-'+draft.channel+'-'+(draft.root||'board'):null}) : null);
    }
    function dmAction(agent, missing = false) {
        const holder = el('div', {class:'agent-actions'});
        let entry = composer.readDM(agent.id), retryAllowed = false, opening = false, failure = '';
        const perform = async mode => {
            if (opening) return; opening = true; failure = ''; draw();
            const issued = epoch, origin = routeKey(route);
            try {
                const value = mode === 'check' ? await composer.recoverDM(agent.id,entry) : mode === 'retry' ? await composer.retryDM(agent.id,entry) : await composer.openDM(agent.id);
                if (!active || issued !== epoch) return;
                entry = value.entry || value; retryAllowed = !!value.retryAllowed;
                if (entry.status === 'complete') {
                    const fresh = await client.bootstrap(); if (!active || issued !== epoch) return;
                    workspace = fresh;
                    const chat = getChat(entry.conversation.id), participating = chat?.members?.includes(agent.id);
                    const target = {view:'conversation',conversation:entry.conversation.id,compose:participating ? '1' : ''};
                    if (!participating) {notice('This agent no longer participates in that DM. Opening it will not rejoin them.', chat ? link('View conversation history',target) : null); return;}
                    if (origin === routeKey(route) && holder.isConnected) {document.querySelector('dialog[open]')?.close();go(target);}
                    else notice('Your direct message is ready.',link('Open DM',target));
                } else draw();
            } catch (error) {if (active && issued === epoch) {entry = composer.readDM(agent.id); failure = error.message;}}
            finally {opening = false; if (holder.isConnected) draw();}
        };
        const draw = () => {
            const availability = composer.availability();
            holder.replaceChildren(button(opening ? 'Opening…' : entry?.status === 'pending' ? 'Check DM opening' : `Message ${agent.name}`, () => perform(entry?.status === 'pending' ? 'check' : 'open'), {class:'primary', ...((availability.enabled && !opening && (!missing || entry?.status === 'pending')) ? {} : {disabled:''})}));
            if (!availability.enabled) holder.append(el('p', {class:'note'},availability.reason));
            if (failure) holder.append(el('p', {class:'status',role:'alert'},failure));
            if (missing) holder.append(el('p', {class:'note'},'This identity is no longer listed. You can still check the original opening.'));
            if (retryAllowed) holder.append(button('Retry the same DM opening',()=>perform('retry')));
        }; draw(); return holder;
    }
    function composerView(channel, root) {
        const origin = {...route}, chat = getChat(channel), holder = el('section', {id:'message-composer', class:'composer', 'aria-label':root ? 'Reply composer':'Message composer'});
        let visible = composer.read(channel,root), queue = Promise.resolve(), busy = false, saveFailed = false, retryAllowed = false, uploadRetryAllowed = false;
        const composerKey=channel+':'+root;
        let expanded = !!visible.pending || !!visible.upload || (!minimizedComposers.has(composerKey) && (!!visible.text || !!visible.files?.length || window.innerWidth > 760));
        const title = root ? `Reply in ${chat.name}` : `Message ${chat.name}`;
        const status = el('div', {class:'composer-status',role:'status'});
        const input = el('textarea', {class:'composer-input', rows:'4', 'aria-label':title, placeholder:root ? 'Write a reply…':'Write a message…'});
        input.value = visible.text;
        const write = text => {
            status.textContent = 'Saving draft…';
            queue = queue.then(async () => {visible = await composer.save(channel,root,text,visible); saveFailed = false;}).catch(error => {saveFailed = true; status.textContent = error.message;
                status.append(button('Review saved draft', () => {
                    try {
                        const stored = composer.read(channel,root);
                        const comparison = el('div', {class:'status'},el('p',{},'Your unsaved text remains above. The saved draft is:'),el('pre',{},stored.text),
                            button('Copy my unsaved text',async()=>{try {await navigator.clipboard.writeText(input.value);notice('Unsaved text copied.');} catch {notice('Select and copy your text before replacing it.');}}),
                            button('Use saved draft',()=>{visible=stored;input.value=stored.text;saveFailed=false;draw();}));
                        status.append(comparison);
                    } catch (readError) {status.textContent=readError.message;}
                }));}).finally(() => {if (!saveFailed) status.textContent = 'Draft saved on this browser';});
        };
        input.addEventListener('input', () => {write(input.value);const submit=holder.querySelector?.('.composer-actions .primary');if(submit)submit.disabled=(!input.value.trim() && !visible.files?.length) || !!visible.pending || !!visible.upload || !composer.availability().enabled;});
        const picker = el('input', {type:'file','aria-label':'Choose an attachment'});
        picker.addEventListener('change', async () => {
            const file = picker.files?.[0];
            if (!file || busy) return;
            const issued = epoch;
            busy = true; draw(); status.textContent = 'Preparing and uploading file…'; await queue;
            if (saveFailed || !active || issued !== epoch) {busy = false;picker.value='';return;}
            status.textContent = 'Uploading '+file.name+'…';
            try {
                const result = visible.upload ? await composer.retryUpload(channel,root,file,visible) : await composer.upload(channel,root,file,visible);
                if (!active || issued !== epoch) return;
                visible = result.draft; uploadRetryAllowed = false;
                status.textContent = visible.uploadRejected ? visible.uploadRejected.error : 'File attached to this draft.';
            } catch (error) {
                if (active && issued === epoch) {visible = composer.read(channel,root);status.textContent = error.message;}
            } finally {busy = false;picker.value='';if(active && issued===epoch)draw();}
        });
        const changeFiles = async action => {
            if (busy) return;
            const issued = epoch;
            busy = true; draw(); await queue;
            if (saveFailed || !active || issued !== epoch) {busy=false;return;}
            try {
                const result = await action();
                if (!active || issued !== epoch) return;
                visible = result.draft || result; uploadRetryAllowed = !!result.uploadRetryAllowed;
                status.textContent = visible.upload ? visible.uploadRejected ? visible.uploadRejected.error : uploadRetryAllowed ? 'Choose the original file to retry the same upload.' : 'The upload is still awaiting confirmation.' : 'Attachment selection updated.';
            } catch (error) {if(active && issued===epoch){visible=composer.read(channel,root);status.textContent=error.message;}}
            finally {busy=false;if(active && issued===epoch)draw();}
        };
        const run = async mode => {
            if (busy) return;
            const issued = epoch;
            busy = true; draw(); input.disabled = true; status.textContent = 'Finishing draft save…'; await queue;
            if (saveFailed || !active || issued !== epoch) {busy = false; input.disabled = !!visible.pending; return;}
            status.textContent = mode === 'check' ? 'Checking this send…' : 'Posting message…';
            try {
                const result = mode === 'check' ? await composer.recover(channel,root,visible) : mode === 'retry' ? await composer.retry(channel,root,visible) : await composer.send(channel,root,visible);
                if (!active || issued !== epoch) return;
                visible = result.draft; retryAllowed = !!result.retryAllowed;
                input.value = visible.text;
                if (result.postId) {
                    directoryDirty = true;
                    pages.delete('board:' + channel); if (root) pages.delete('thread:' + channel + ':' + root);
                    const target = {view:root ? 'thread':'conversation',conversation:channel,thread:root,post:result.postId,from:origin.from,source:origin.source,boardOrigin:origin.boardOrigin};
                    if (routeKey(origin) === routeKey(route)) go(target);
                    else notice('Message posted.',link('View message',target));
                } else draw();
            } catch (error) {
                if (active && issued === epoch) {visible = composer.read(channel,root); draw(); status.textContent = error.message;}
            } finally {busy = false; if(active && issued===epoch)draw();}
        };
        const draw = () => {
            holder.className='composer'+(expanded?' expanded':' collapsed')+(visible.pending?' held':'');
            holder.replaceChildren(el('div', {class:'heading'}, el('h2', {},title), visible.pending ? null : button(expanded ? 'Minimize' : visible.text ? 'Resume saved draft' : root ? 'Reply to this message' : 'Write a new message', () => {
                expanded = !expanded; expanded?minimizedComposers.delete(composerKey):minimizedComposers.add(composerKey); draw();
                if (expanded) input.focus(); else holder.querySelector('button')?.focus();
            })));
            const availability = composer.availability(), attachmentPolicy = composer.attachmentAvailability(); input.disabled = busy || !!visible.pending || !availability.enabled;
            if (visible.pending) {
                const held = el('div', {class:'composer-held',role:'status'}, visible.rejected ? 'Send rejected: '+visible.rejected.error : 'This send is held until its outcome is known.');
                if (visible.rejected) held.append(button('Edit rejected message', async () => {try {visible = await composer.releaseRejected(channel,root,visible); input.value = visible.text; expanded = true; draw();} catch (error) {status.textContent=error.message;}}));
                else held.append(button('Check send status',()=>run('check')));
                if (retryAllowed) held.append(button('Retry the same send',()=>run('retry')));
                holder.append(held);
            }
            if (visible.upload) {
                const held = el('div', {class:'composer-held',role:'status'}, visible.uploadRejected ? 'Upload rejected: '+visible.uploadRejected.error : 'Upload of '+visible.upload.filename+' needs confirmation.');
                held.append(button(visible.uploadRejected ? 'Remove rejected upload' : 'Check upload status', () => changeFiles(() => visible.uploadRejected ? composer.releaseUpload(channel,root,visible) : composer.recoverUpload(channel,root,visible)), {...(busy ? {disabled:''} : {})}));
                if(!visible.uploadRejected)held.append(button('Remove upload from draft',()=>changeFiles(()=>composer.abandonUpload(channel,root,visible)),{...(busy?{disabled:''}:{})}),
                    el('p',{class:'note'},'Removing it here does not cancel the upload or delete a file. An unattached file may already exist on the server.'));
                if (uploadRetryAllowed) held.append(el('p',{class:'note'},'Choose the same original file below to retry this upload.'));
                holder.append(held);
            }
            if (expanded) {
                const staged = el('div',{class:'attachment-list','aria-label':'Draft attachments'},(visible.files||[]).map(file=>el('div',{class:'attachment-card'},el('div',{},el('strong',{},file.name),el('small',{class:'note'},fileSize(file.size))),
                    button('Remove',()=>changeFiles(()=>composer.removeFile(channel,root,file.id,visible)),{'aria-label':'Remove '+file.name,...((busy||visible.pending||visible.upload)?{disabled:''}:{})}))));
                picker.disabled=busy || !!visible.pending || (visible.files||[]).length>=(attachmentPolicy.max_files_per_post||10) || (!!visible.upload && !uploadRetryAllowed) || !availability.enabled || !attachmentPolicy.enabled;
                holder.append(input,staged,el('label',{class:'attachment-picker'},visible.upload?'Choose original file to retry':'Attach a file',picker),
                    el('div', {class:'composer-actions'}, el('span', {class:'note'},'Sending as '+workspace.user.username), button(root ? 'Post reply':'Post message',()=>run('send'), {class:'primary', ...((busy || (!visible.text.trim() && !visible.files?.length) || visible.pending || visible.upload || !availability.enabled || (visible.files?.length && !attachmentPolicy.enabled)) ? {disabled:''} : {})})));
                if(!attachmentPolicy.enabled)holder.append(el('p',{class:'note'},attachmentPolicy.reason));
            }
            else if (visible.text) holder.append(el('p', {class:'note'},'Draft kept · '+visible.text.slice(0,100)));
            if (!availability.enabled) holder.append(el('p', {class:'status'},availability.reason));
            holder.append(status);
        };
        holder.openComposer = () => {expanded = true;minimizedComposers.delete(composerKey);draw();input.focus();};
        draw(); return holder;
    }
    async function pageFor(value, before) {
        const key = pageKey(value);
        if (!before && pages.has(key)) return pages.get(key);
        const data = value.view === 'thread' ? await client.thread(value.conversation, value.thread, before) : await client.board(value.conversation, before);
        const old = before && pages.get(key);
        const merged = {...data, posts: old ? mergePosts(data.posts, old.posts) : data.posts};
        pages.set(key, merged); return merged;
    }
    function fileSize(bytes) {
        return Number.isFinite(bytes) ? bytes < 1024 ? bytes+' bytes' : bytes < 1048576 ? (bytes/1024).toFixed(1)+' KB' : (bytes/1048576).toFixed(1)+' MB' : '';
    }
    function releaseDownload(url) {
        const timer = downloads.get(url);
        if (timer !== undefined) {window.clearTimeout(timer);window.URL.revokeObjectURL(url);downloads.delete(url);}
    }
    function attachmentCards(post) {
        if (!post.file_ids?.length) return null;
        return el('div',{class:'attachment-list','aria-label':'Message attachments'},post.file_ids.map(id=>{
            const metadata=(post.files||[]).find(item=>item.id===id),unavailable=metadata?.available===false;
            const file=unavailable?null:metadata;
            const status=el('span',{class:'note',role:'status'});
            const download=button('Download',async()=>{
                const issued=epoch,version=routeVersion;
                download.disabled=true;status.textContent='Downloading…';
                try {
                    const blob=await client.downloadFile(post.id,id);
                    if(!active || issued!==epoch || version!==routeVersion)return;
                    if(!window.URL?.createObjectURL)throw new Error('File downloads are unavailable in this browser.');
                    const url=window.URL.createObjectURL(blob);
                    downloads.set(url,window.setTimeout(()=>releaseDownload(url),1000));
                    const anchor=el('a',{href:url,download:file?.name||'attachment'});anchor.click();
                    status.textContent='Download started.';
                }catch(error){if(active && issued===epoch && version===routeVersion)status.textContent=error.message;}
                finally{download.disabled=false;}
            },{'aria-label':'Download '+(file?.name||'attachment'),...(unavailable?{disabled:''}:{})});
            return el('div',{class:'attachment-card'},el('div',{},el('strong',{},file?.name||(unavailable?'Attachment unavailable':'Attachment details unavailable')),file?el('small',{class:'note'},fileSize(file.size)+(file.mime_type?' · '+file.mime_type:'')):null),download,status);
        }));
    }
    function message(post, isRootBoard = false) {
        const target = {view: 'thread', conversation: post.channel_id, thread: post.root_id || post.id, post: post.id, from: route.from, source: route.source,boardOrigin:route.view==='conversation'?boardOriginKey(route):route.boardOrigin};
        const copy = button('Copy link', async () => {
            try { await navigator.clipboard.writeText(location.origin + location.pathname + href({...target, from:'',source:'',boardOrigin:''})); copy.textContent = 'Link copied'; }
            catch { copy.textContent = 'Use the address bar link'; }
        });
        return el('article', {class: 'message' + (route.post === post.id ? ' highlight' : ''), id: 'post-' + post.id, tabindex: '-1'},
            avatar(post.author,post.user_id===workspace.user.id),el('div',{class:'message-content'},el('header', {}, el('strong', {}, post.user_id === workspace.user.id ? 'You' : post.author), el('time', {class: 'time', datetime: new Date(post.create_at).toISOString()}, date(post.create_at))),
            messageBody(post.message), attachmentCards(post), el('footer', {class: 'message-actions'}, personal ? saveAction(post) : null, route.source === 'inbox' && inboxItems.some(item => item.post_id === post.id) ? handleAction(inboxItems.find(item => item.post_id === post.id)) : null, isRootBoard ? link(post.reply_count ? `${post.reply_count} ${post.reply_count === 1 ? 'reply' : 'replies'} →` : 'Open thread →', target) : null, copy)));
    }
    async function restoreInboxSource(valid) {
        const fresh=await client.inbox();if(!valid())return;
        inboxItems=fresh.items;inboxTotal=fresh.total;
        if(!inboxSession.length) {
            inboxSession=fresh.items.map(item=>item.post_id);
            inboxSnapshot=fresh.items;inboxSourceReopened=true;
        } else {
            const restored=[];
            for(const id of inboxSession) {
                const pending=fresh.items.find(item=>item.post_id===id);
                if(pending){restored.push(pending);continue;}
                try {const context=await client.context(id);if(!valid())return;restored.push({post_id:id,channel_id:context.post.channel_id,post:context.post,reasons:[]});}
                catch(error){if(!valid())return;if(error.status!==404)throw error;}
            }
            inboxSnapshot=restored;
        }
        inboxSourceReady=true;
    }
    async function reader(main, version, restore) {
        const value = {...route}, chat = getChat(value.conversation);
        if (!chat) throw new Error('This conversation is no longer available in your workspace.');
        const thread = value.view === 'thread';
        if(personal && value.source==='inbox' && !inboxSourceReady) {await restoreInboxSource(()=>active&&version===routeVersion);if(!active||version!==routeVersion)return;}
        lastBoard=chat.id;
        const mainContainer=main;
        const pane=el('section',{class:'reader-main','aria-label':thread?'Message thread':chat.name+' message board'});
        const header=el('header',{class:'reader-header'});
        const layout=el('div',{class:'reader-layout'});
        if (value.source==='inbox' && inboxSnapshot.length) {
            const source=el('aside',{class:'inbox-source','aria-label':'Inbox source list'},el('header',{},el('h2',{},'Inbox'),el('p',{class:'note'},(inboxSourceReopened?'Current Inbox after reopening':'Original order and filters')+' · New arrivals stay in Inbox'),link('Return to Inbox →',{view:'inbox'})));
            source.append(el('div',{class:'inbox-source-list'},inboxSnapshot.filter(item=>item.post).map(item=>link([
                el('span',{},getChat(item.channel_id)?.name||'Conversation'),el('strong',{},item.post.author+' · '+date(item.post.create_at)),el('p',{},previewText(item.post).slice(0,150)),el('small',{},inboxItems.some(row=>row.post_id===item.post_id)?'Pending':'Open message')
            ],postTarget(item.post,{source:'inbox'}),{class:'inbox-source-item','aria-current':value.post===item.post_id?'location':null}))));
            layout.append(source);
        }
        layout.append(pane);mainContainer.append(layout);pane.append(header);
        main=header;
        if (thread) main.append(link(['← ',el('span',{class:'badge'},kind(chat)),chat.name,' · Message board'], {view: 'conversation', conversation: chat.id, post: value.thread, boardReturn:value.boardOrigin?'1':'',boardOrigin:value.boardOrigin, from: value.from, source: value.source}, {class: 'back board-back'}));
        if (['inbox',...personalViews].includes(value.source)) main.append(link('← Return to '+labels[value.source], {view:value.source}, {class:'back'}));
        if (value.from && searches.has(value.from)) main.append(link('← Return to search', {view: 'search', search: value.from}, {class: 'back'}));
        const title=heading(thread?'Thread':chat.name,thread?'Original message and replies':chat.description || 'Messages in arrival order');
        title.querySelector?.('.eyebrow')?.remove();
        if(personal) title.append(readerActions(chat,thread?value.thread:''));
        if(!thread) title.firstElementChild?.prepend(el('p',{class:'board-kind'},el('span',{class:'badge'},kind(chat)),' Message board'));
        main.append(title);
        if (!workspace.capabilities?.root_board && !thread) {
            main.append(el('p', {class: 'status'}, 'This server needs the complete-board API update. Continue using Mattermost until the candidate backend is installed.')); return;
        }
        const memberAgents=(chat.members||[]).map(id=>workspace.agents.find(agent=>agent.id===id)).filter(Boolean);
        const observerAgents=(chat.observers||[]).map(id=>workspace.agents.find(agent=>agent.id===id)).filter(Boolean);
        const audience=el('details',{class:'audience'},el('summary',{},icon('agents'),memberAgents.map(agent=>agent.name).join(', ') || kind(chat),sharedReadership() ? ' · Participants' : ' · Audience'),
            el('div',{class:'audience-panel'},el('h3',{},sharedReadership() ? 'Who can post' : 'Direct participants'),
                chat.dm_user_ids?.includes(workspace.user.id)?el('p',{},'You · '+workspace.user.username):null,
                memberAgents.map(agent=>el('p',{class:'identity'},agent.name+' · '+agent.id)),
                el('h3',{},'Inherited readers'),observerAgents.length?observerAgents.map(agent=>el('p',{class:'identity'},agent.name+' · '+agent.id)):el('p',{},'None listed'),
                el('p',{class:'note'},'Access does not mean an agent was notified or is working on this conversation.')));
        if(!thread) main.append(el('div',{class:'board-context'},audience,button('Jump to latest ↓',()=>{const scroll=document.getElementById('message-scroll');if(scroll)scroll.scrollTop=scroll.scrollHeight;},{class:'text-link jump-latest'})));
        else main.append(audience);
        const attention = value.source === 'inbox' && inboxItems.find(item => item.post_id === value.post);
        if (attention) main.append(el('div', {class:'reader-actions'}, handleAction(attention), handleAction(attention,true)));
        if (undoAction && (value.source==='inbox' || value.conversation===undoAction.channel)) main.append(undoControl());
        const scroll=el('div',{id:'message-scroll',class:'reader-scroll',onscroll:()=>{positions.set(routeKey(value),scroll.scrollTop);if(!thread)positions.set(pageKey(value),scroll.scrollTop);}});pane.append(scroll);main=scroll;
        const loading = el('p', {role: 'status', class: 'note'}, 'Loading messages…'); main.append(loading);
        // An Inbox arrival may be newer than the board cached before it arrived.
        // Read the current page so a new message appears in arrival order.
        const cached=pages.get(pageKey(value));
        if (value.source === 'inbox' || (cached && value.post && cached.root?.id !== value.post && !cached.posts.some(post=>post.id===value.post))) pages.delete(pageKey(value));
        const data = await pageFor(value);
        if (version !== routeVersion || !active) return;
        let exact = null;
        if (value.post && value.post !== data.root?.id && !data.posts.some(p => p.id === value.post)) {
            if (workspace.capabilities?.exact_context) {
                exact = await client.context(value.post);
                if (exact.post.channel_id !== chat.id || (thread && exact.root.id !== value.thread) || (!thread && exact.root.id !== value.post)) throw new Error('The linked message belongs to another board or thread.');
            }
        }
        if (version !== routeVersion || !active) return;
        loading.remove();
        const content = el('div', {class: 'board'});
        if (thread) {
            const replyCount=data.posts.filter(p=>p.id!==data.root.id).length;
            const description=title.querySelector?.('.note');if(description)description.textContent=(data.root.user_id===workspace.user.id?'Replies to your message':`Replies to ${data.root.author}’s message`)+` · ${replyCount}${data.next_before?'+':''} ${replyCount===1&&!data.next_before?'reply':'replies'}`;
            const rootMessage=message(data.root);rootMessage.className+=' thread-root';rootMessage.querySelector?.('header')?.append(el('span',{class:'root-label'},'Original message'));content.append(rootMessage,el('div',{class:'reply-divider'},'Replies'));
        } else content.append(el('p',{class:'arrival-label'},'Root messages · Arrival order'));
        if (data.next_before) {
            const older = button('Load older messages', async () => {
                older.disabled = true;
                const first = content.querySelector('.message'), offset = first?.getBoundingClientRect().top;
                try {
                    await pageFor(value, data.next_before);
                    if (version !== routeVersion || !active) return;
                    const previousId = first?.id;
                    await renderRoute(false);
                    if (previousId && Number.isFinite(offset)) scrollToPosition(scrollPosition()+document.getElementById(previousId).getBoundingClientRect().top-offset);
                } catch (error) { if (version === routeVersion) showError(error, main); }
                finally { older.disabled = false; }
            }, {class: 'load'}); main.append(older);
        }
        if (exact) {
            content.append(el('p', {class: 'status'}, 'Linked message shown separately. Earlier messages between it and the loaded page have not been loaded.'), message(thread ? exact.post : exact.root, !thread));
        } else if (value.post && !data.posts.some(p => p.id === value.post) && data.root?.id !== value.post) {
            main.append(el('p', {class: 'status'}, 'The linked message is not in this page. Load older messages to find it.'));
        }
        const posts = thread ? data.posts.filter(p => p.id !== data.root.id) : data.posts;
        posts.forEach(post => content.append(message(post, !thread)));
        if (!posts.length) content.append(el('p', {class: 'empty'}, thread ? 'No replies yet.' : 'No messages yet.'));
        main.append(content);
        if (composer) pane.append(composerView(chat.id, thread ? value.thread : ''));
        pane.append(el('p', {class: 'reader-footer'}, 'Reading does not handle Inbox items or join channels. Rich embeds are available in Mattermost.'));
        if (value.compose && composer) document.getElementById('message-composer')?.openComposer();
        else if (restore) restorePosition(value);
    }
    function restorePosition(value) {
        requestAnimationFrame(() => {
            if (routeKey(value) !== routeKey(route) || !active) return;
            if(value.view==='search') {
                scrollToPosition(positions.get(routeKey(value))||0);
                const focusId=searches.get(value.search)?.focusResult;
                if(focusId)document.getElementById(focusId)?.focus({preventScroll:true});
                return;
            }
            if (value.view === 'conversation' && (value.boardReturn || (!value.post && !value.from)) && positions.has(pageKey(value))) {
                scrollToPosition(positions.get(value.boardOrigin) ?? positions.get(pageKey(value))); return;
            }
            if (positions.has(routeKey(value))) {scrollToPosition(positions.get(routeKey(value)));return;}
            if (value.post) {
                const target = document.getElementById('post-' + value.post);
                if (target) {scrollToPost(target); return;}
            }
            scrollToPosition(positions.get(routeKey(value)) || 0);
        });
    }
    function searchSnippet(text, query) {
        const matchAt = text.toLocaleLowerCase().indexOf(query.toLocaleLowerCase());
        const start = Math.max(0, matchAt - 90), end = Math.min(text.length, start + 320);
        const snippet = el('p', {class: 'result-snippet'});
        if (start) snippet.append('…');
        if (matchAt >= start && matchAt < end) {
            snippet.append(text.slice(start, matchAt), el('mark', {}, text.slice(matchAt, matchAt + query.length)), text.slice(matchAt + query.length, end));
        } else snippet.append(text.slice(start, end));
        if (end < text.length) snippet.append('…');
        return snippet;
    }
    function searchMessage(post, current) {
        const chat = getChat(post.channel_id);
        return link(el('div', {}, el('div', {class: 'card-title'}, el('strong', {}, post.author), el('time', {class: 'time'}, date(post.create_at))),
            el('p', {class: 'result-context'}, `${chat ? kind(chat) + ' · ' + chat.name : 'Conversation'} · ${post.root_id ? 'Thread reply' : 'Board message'}`),
            searchSnippet(previewText(post), current.query.trim())),
            {view: post.root_id ? 'thread' : 'conversation', conversation: post.channel_id,
                thread: post.root_id || '', post: post.id, from: current.id, source: current.id ? '' : route.view}, {class: 'card search-hit',id:'search-message-'+post.id});
    }
    async function renderSearchResults(more = false) {
        const current = searches.get(route.search), results = document.getElementById('search-results');
        if (!current || !results || !active) return;
        const issued = ++searchVersion, version = routeVersion;
        const valid = () => active && issued === searchVersion && version === routeVersion;
        const query = current.query.trim(), signature = JSON.stringify([query, current.scope, current.conversation, current.thread]);
        if (current.signature !== signature) {current.signature = signature; delete current.messages;delete current.focusResult;}
        const match = (...texts) => texts.some(text => String(text || '').toLocaleLowerCase().includes(query.toLocaleLowerCase()));
        results.replaceChildren();
        const subtitle = document.getElementById('search-description');
        if (subtitle) subtitle.textContent = scopes[current.scope];
        if (!query) {results.append(el('p', {class: 'empty'}, 'Enter a phrase or name above.')); return;}
        if ([...query].length > 256) {results.append(el('p', {class: 'status', role: 'alert'}, 'Use a phrase of up to 256 characters.')); return;}
        if (['inbox', 'saved', 'drafts', 'yours'].includes(current.scope)) {
            const status = el('p', {role:'status'}, 'Searching…'), list = el('div', {class:'list'}); results.append(status,list);
            try {
                if (current.scope === 'drafts') {
                    const drafts = (composer?.list() || []).filter(item => match(item.text,getChat(item.channel)?.name));
                    list.append(...drafts.map(item=>draftCard(item,current))); status.textContent = `${drafts.length} matching drafts on this browser`;
                } else if (current.scope === 'saved') {
                    const found = savedItems.filter(item => match(item.note,item.post?.message,item.post?.author,getChat(item.post?.channel_id)?.name));
                    list.append(...found.map(item=>savedCard(item,current))); status.textContent = `${found.length} saved messages`;
                } else {
                    if (!current.messages || more) {
                        const data = current.scope === 'inbox' ? await client.inbox(more ? current.messages?.next_before : '',query) : await client.yourMessages(query,more ? current.messages?.next_before : '');
                        if (!valid()) return;
                        const posts = current.scope === 'inbox' ? data.items.filter(item => item.post).map(item => item.post) : data.posts;
                        current.messages = {posts:[...new Map([...(more ? current.messages?.posts || [] : []),...posts].map(post => [post.id,post])).values()],next_before:data.next_before,complete:data.complete,coverage_warning:data.coverage_warning};
                    }
                    if (current.scope === 'inbox' && !current.messages.complete) results.append(el('p', {class:'status',role:'alert'},current.messages.coverage_warning || 'Inbox coverage is incomplete; these are known matching items.'));
                    list.append(...current.messages.posts.map(post => searchMessage(post,current)));
                    status.textContent = `${current.messages.posts.length}${current.messages.next_before ? '+' : ''} matching ${current.messages.posts.length === 1 && !current.messages.next_before ? 'message' : 'messages'}`;
                    if (current.messages.next_before) results.append(button('Load more matches',()=>renderSearchResults(true),{class:'load'}));
                }
                if (!list.children.length) list.append(el('p', {class:'empty-state'},'No matches in this scope.'));
                results.append(button('Search everywhere →',()=>{current.scope='everywhere';renderRoute();},{class:'widen-search'}));
            } catch (error) {if (valid()) {status.textContent='Search could not finish.';list.append(el('p', {class:'status',role:'alert'},error.message));}}
            return;
        }
        let directoryCount = 0;
        if (['everywhere', 'conversations', 'channels', 'favorites'].includes(current.scope)) {
            const chats = workspace.conversations.filter(c => (current.scope !== 'channels' || c.kind !== 'dm') && (current.scope !== 'favorites' || personal?.favorites.includes(c.id)) && match(c.name, c.description, ...(c.members || []).map(id => workspace.agents.find(a => a.id === id)?.name)));
            if (chats.length) results.append(el('h2', {class: 'result-type'}, 'Conversations'), el('div', {class: 'list'}, chats.map(c => conversationCard(c, current.id))));
            directoryCount += chats.length;
        }
        if (['everywhere', 'agents'].includes(current.scope)) {
            const found = workspace.agents.filter(a => match(a.name, a.id, a.username));
            if (found.length) results.append(el('h2', {class: 'result-type'}, 'Agents'), el('div', {class: 'list'}, found.map(a => el('article', {class: 'card'}, el('strong', {}, a.name), el('p', {}, a.id), button('View identity →',()=>agentDialog(a))))));
            directoryCount += found.length;
        }
        const messageScope = ['everywhere', 'conversation', 'thread'].includes(current.scope);
        if (!messageScope) {
            results.prepend(el('p', {role: 'status'}, `${directoryCount} results in ${scopes[current.scope]}`));
            if (!directoryCount) results.append(el('p', {class: 'empty'}, 'No matching names or descriptions.'));
            results.append(button('Search messages everywhere →', () => {
                current.scope = 'everywhere'; renderRoute();
            }, {class: 'widen-search'}));
            return;
        }
        results.append(el('h2', {class: 'result-type'}, 'Messages'), el('p', {class: 'note'}, 'Searches full message history, including replies. Exact phrase, newest first.'));
        if (!workspace.capabilities?.full_history_search) {
            results.append(el('p', {class: 'status'}, 'Full-history search needs the search API update on this server. No partial results are shown.')); return;
        }
        const summary = el('p', {role: 'status'}, 'Searching message history…'), list = el('div', {class: 'list search-messages'});
        results.append(summary, list);
        const drawMessages = () => list.replaceChildren(...(current.messages?.posts || []).map(post => searchMessage(post, current)));
        drawMessages();
        try {
            if (!current.messages || more) {
                const data = await client.search(query, {
                    conversation: current.scope === 'everywhere' ? '' : current.conversation,
                    root: current.scope === 'thread' ? current.thread : '',
                    before: more ? current.messages?.next_before || '' : '',
                });
                if (!valid()) return;
                current.messages = {posts: [...new Map([...(more ? current.messages?.posts || [] : []), ...data.posts].map(post => [post.id, post])).values()], next_before: data.next_before};
            }
            if (!valid()) return;
            drawMessages();
            const count = current.messages.posts.length;
            summary.textContent = `${count}${current.messages.next_before ? '+' : ''} matching ${count === 1 ? 'message' : 'messages'}${directoryCount ? ` · ${directoryCount} directory results above` : ''}`;
            if (!count) {list.remove(); results.append(el('p', {class: 'empty'}, 'No messages contain that phrase in this scope. Try fewer words or search everywhere.'));}
            if (current.messages.next_before) results.append(button('Load more matching messages', () => renderSearchResults(true), {class: 'load'}));
            if (current.scope !== 'everywhere') results.append(button('Search everywhere →', () => {current.scope = 'everywhere'; renderRoute();}, {class: 'widen-search'}));
            restorePosition(route);
        } catch (error) {
            if (!valid()) return;
            summary.textContent = 'Search could not finish.';
            results.append(el('div', {role: 'alert', class: 'status'}, error.name === 'AbortError' ? 'The search timed out. Try again.' : error.message,
                button('Retry search', () => renderSearchResults(more))));
        }
    }
    async function searchView(main) {
        const current = searches.get(route.search);
        if (!current) {main.append(heading('Search', 'Search text is kept only in this page’s memory.', false), button('Start a new search', () => beginSearch('', 'everywhere'))); return;}
        main.append(button('← Close search', () => go(current.origin), {class: 'search-close'}),
            el('div', {class: 'heading'}, el('div', {}, el('h1', {}, 'Search'), el('p', {id: 'search-description', class: 'note'}, scopes[current.scope]))), searchFormNode, el('div', {id: 'search-results'}));
        await renderSearchResults();
        if(!current.query) document.querySelector?.('[aria-label="Search Murmur"]')?.focus();
    }
    function showError(error, main) {
        if ([401,403,409,426].includes(error.status)) { suspend(); locked(error); return; }
        main.append(el('div', {class: 'status', role: 'alert'}, error.name === 'AbortError' ? 'The request timed out. Try again.' : error.message,
            button('Try again', () => renderRoute())));
    }
    async function renderRoute(restore = true) {
        if (!active || !workspace) return;
        const previousPosition=scrollPosition(), previousView=route.view;
        route = currentRoute(); const version = ++routeVersion, main = shell();
        try {
            if (route.view === 'agents') agents(main);
            else if (route.view === 'inbox' || personalViews.includes(route.view)) await personalView(main, version);
            else if (route.view === 'search') await searchView(main);
            else if (['conversation', 'thread'].includes(route.view)) await reader(main, version, restore);
            else {
                if (directoryDirty || (!booting && previousView !== route.view)) {
                    const fresh = await client.bootstrap();
                    if (!active || version !== routeVersion) return;
                    workspace = fresh; directoryDirty = false;
                    const notice = readershipNotice();
                    document.getElementById('managed-readership')?.replaceChildren(...(notice ? [notice] : []));
                    lastRefresh = new Date().toLocaleTimeString(undefined, {hour:'2-digit',minute:'2-digit'});
                }
                directory(main);
            }
            if (restore && !['conversation', 'thread'].includes(route.view)) restorePosition(route);
            else if(!restore) scrollToPosition(previousPosition);
        } catch (error) { if (version === routeVersion && active) showError(error, main); }
    }
    function locked(error) {
        mount.replaceChildren(el('main', {id: 'main', class: 'lock'}, el('p', {class: 'eyebrow'}, 'Murmur reader preview'),
            el('h1', {}, error ? 'Workspace locked' : 'Checking your session'),
            el('p', {role: 'status'}, error?.message || 'Private content appears after your Mattermost session is validated.'),
            isDemo ? el('p', {class: 'note'}, 'This is a separate demo with test accounts and messages. Your normal Mattermost account does not sign in here.') : null,
            error?.status === 401 ? el('p', {class: 'note'}, 'Sign in in the new Mattermost tab, then return here and continue. Your reading link stays here.') : null,
            el('div', {class: 'actions'}, button(error?.status === 401 ? 'Continue after sign-in' : 'Try again', boot),
                error?.status === 401 ? el('a', {href: '/login', target: '_blank', rel: 'noopener'}, 'Sign in to Mattermost ↗') : el('a', {href: '/'}, 'Open Mattermost →'))));
    }
    function scheduleAttention() {
        window.clearTimeout(attentionTimer);
        attentionTimer = window.setTimeout(async () => {
            if (!active || !personal || document.visibilityState === 'hidden') return;
            const issued = epoch;
            try {
                const data = await client.inbox();
                if (!active || issued !== epoch) return;
                const changed = data.total > inboxTotal;
                inboxTotal = data.total;
                document.querySelectorAll('nav a[href="#view=inbox"]').forEach(node => {node.replaceChildren(icon('inbox'),el('span',{class:'nav-label'},'Inbox'),...(inboxTotal?[el('span',{class:'count'},inboxTotal)]:[]));});
                if (changed) notice('New messages in your Inbox.', link('Open Inbox', {view:'inbox'}));
            } catch (error) {
                if (active && issued === epoch) notice('Inbox could not refresh. Your current work is kept.', button('Refresh',boot));
            } finally {if (active && issued === epoch) scheduleAttention();}
        }, 20000);
    }
    function suspend() {
        for (const url of downloads.keys()) releaseDownload(url);
        window.clearTimeout(attentionTimer); attentionTimer = null;
        epoch++; routeVersion++; searchVersion++; active = false; booting = false; client.cancel(); workspace = null; personal = null; savedItems = []; inboxItems = []; inboxSnapshot = []; inboxSourceReady=false; agentFacts = []; composer?.suspend(); pages.clear();
        for (const search of searches.values()) delete search.messages;
        // Synchronous concealment before BFCache or background suspension.
        locked();
    }
    async function boot() {
        if (booting || document.visibilityState === 'hidden') return;
        suspend(); booting = true; const issued = epoch;
        try {
            const data = await client.bootstrap();
            if (issued !== epoch || document.visibilityState === 'hidden') return;
            workspace = data; directoryDirty = false; active = true;
            if (workspace.capabilities?.personal_workspace) {
                const details = await client.workspace();
                if (issued !== epoch) return;
                personal = details;
                const saved = await client.saved();
                if (issued !== epoch) return;
                savedItems = saved.items;
            }
            if (workspace.capabilities?.agent_activity) {
                const activity = await client.agentActivity();
                if (issued !== epoch) return;
                agentFacts = activity.agents;
            }
            if (typeof MurmurCompose !== 'undefined') {
                if (!composer) composer = MurmurCompose.create({host: window, client, userId: workspace.user.id, isActive: () => active,
                    attachmentPolicy: () => workspace?.capabilities?.attachments});
                else composer.resume(workspace.user.id);
            }
            if (personal && route.source==='inbox') {await restoreInboxSource(()=>issued===epoch);if(issued!==epoch)return;}
            lastRefresh = new Date().toLocaleTimeString(undefined, {hour:'2-digit', minute:'2-digit'});
            await renderRoute();
            if (personal) scheduleAttention();
        } catch (error) { if (issued === epoch) {active = false; locked(error);} }
        finally { if (issued === epoch) booting = false; }
    }
    window.addEventListener('hashchange', () => renderRoute());
    window.addEventListener('pagehide', suspend);
    window.addEventListener('pageshow', () => { if (!active) boot(); });
    document.addEventListener('visibilitychange', () => document.visibilityState === 'hidden' ? suspend() : boot());
    window.addEventListener('keydown', event => {
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {event.preventDefault(); if(route.view==='search') document.querySelector('[aria-label="Search Murmur"]')?.focus(); else beginSearch('');}
    });
    boot();
})();
