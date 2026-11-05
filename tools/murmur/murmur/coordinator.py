"""One local coordinator: server reconciliation, exact reading and doorbells."""

from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType

from .backend import BackendError
from .attachments import describe_source, preview, source_bytes
from .ledger import LedgerError, StaleFeedError


class ConversationChanged(RuntimeError):
    pass


def conversation_descriptor(value, channel):
    if (not isinstance(value, dict) or value.get("id") != channel
            or not isinstance(value.get("name"), str) or value.get("kind") not in ("dm", "channel")
            or value.get("role") not in (None, "member", "observer")
            or "role" not in value or type(value.get("open")) is not bool):
        raise RuntimeError("Invalid canonical conversation descriptor")
    visibility = "private" if value["kind"] == "dm" else "public" if value["open"] else "legacy-private"
    if value.get("visibility") != visibility or value["kind"] == "dm" and value["open"]:
        raise RuntimeError("Invalid canonical conversation visibility")
    return value


def identity_dict(identity):
    if is_dataclass(identity):
        return asdict(identity)
    return dict(identity)


def actor_key(identity):
    value = identity_dict(identity)
    return value["provider"] + ":" + value["session_id"]


def enrollment_capabilities(mode):
    """Enrollment policy, independent of ancestry facts and runtime enable flags."""
    if mode not in ("native", "manual"):
        raise ValueError("Unsupported enrollment mode")
    return {"mode": mode, "automatic_wakeups": mode == "native", "inherited_access": mode == "native"}


def _server_parent(identity, mode):
    if mode == "manual":
        return None
    parent = identity.get("parent_session_id")
    return identity["provider"] + ":" + parent if parent else None


def routing_binding(identity):
    """Bind an attempt to verified routing facts; not a synthetic process epoch.

    Claude exposes a process generation. Codex currently proves session routing
    but no process epoch. Store the distinction in identity evidence.
    """
    value = identity_dict(identity)
    target = value.get("target", {})
    epoch = target.get("native_epoch")
    return hashlib.sha256(json.dumps({"actor": actor_key(identity),
                                     "native_epoch": epoch},
                                    sort_keys=True).encode()).hexdigest()


def operation_metadata(result):
    """Retain reconciliation evidence without a second copy of message bodies."""
    if result and "post" in result:
        value = {"post_id": result["post"]["id"], "channel_id": result["post"]["channel_id"]}
        if result["post"].get("file_ids"):
            value["file_ids"] = result["post"]["file_ids"]
        return value
    if result and "file_id" in result:
        return {key: result[key] for key in ("file_id", "channel_id", "file") if key in result}
    if result and "conversation" in result:
        return {"channel_id": result["conversation"]["id"]}
    if result and "post_id" in result:
        return {"post_id": result["post_id"], "channel_id": result["channel_id"]}
    if result and "channel_id" in result:
        return {"channel_id": result["channel_id"]}
    return {"ok": True}


class Coordinator:
    def __init__(self, ledger, backend, transport=None):
        self.ledger = ledger
        self.backend = backend
        self.transport = transport

    def activity(self, identity):
        """Observed lifecycle state; missing hooks never establish native idleness."""
        actor, binding = actor_key(identity), routing_binding(identity)
        active = self.ledger.active_turn(actor)
        if active:
            return {"state": "observed_busy" if active["epoch"] == binding else "unknown",
                    "turn_key": active["turn_key"]}
        latest = self.ledger.db.execute("""SELECT turn_key,status FROM native_turn_records
            WHERE actor=? AND epoch=? ORDER BY rowid DESC LIMIT 1""", (actor, binding)).fetchone()
        if latest and latest["status"] in ("completed", "interrupted"):
            return {"state": "observed_idle", "turn_key": latest["turn_key"]}
        return {"state": "unknown"}

    def register(self, identity, name=None, mode="native", *, username_prefix=None, inherit=None):
        capabilities = enrollment_capabilities(mode)
        value = identity_dict(identity)
        if mode == "manual":
            from .native import NativeIdentity
            if not isinstance(identity, NativeIdentity) or identity.provider != "codex":
                raise ValueError("Manual sidechat enrollment requires the current Codex native identity")
        elif not value.get("parent_known"):
            raise RuntimeError("Native ancestry is unresolved; registration is held")
        if mode == "native" and value.get("provider") == "codex":
            from .native import NativeIdentity
            from .native.codex import automatic_enrollment_reason
            reason = automatic_enrollment_reason(NativeIdentity(**value))
            if reason:
                raise ValueError(reason)
        actor = actor_key(value)
        if value["provider"] == "claude" and value.get("target", {}).get("native_epoch") is not None:
            try:
                previous = self.ledger.actor(actor)["identity"]
            except LedgerError:
                previous = None
            # Re-registering or renaming uses an ordinary lookup, which lacks
            # hook-only history evidence. Preserve it for this exact owner.
            if previous and value["target"] == previous["target"]:
                value["evidence"] = dict(value.get("evidence", {}))
                for key in ("claude_peer_transcript_path", "claude_peer_engine_version", "claude_peer_capture_epoch"):
                    if key not in value["evidence"] and key in previous.get("evidence", {}):
                        value["evidence"][key] = previous["evidence"][key]
        if mode == "manual":
            try:
                previous = self.ledger.actor(actor)
            except LedgerError:
                previous = None
            # A lightweight capture need not repeat an already verified proof.
            # Retain those facts without turning on inheritance or wakeups.
            if previous and previous["identity"].get("parent_known") and not value.get("parent_known"):
                for field in ("parent_known", "parent_session_id"):
                    value[field] = previous["identity"][field]
                value["evidence"] = {**previous["identity"].get("evidence", {}), **value.get("evidence", {})}
            if name is None:
                name = previous["name"] if previous else "Sidechat · " + identity.session_id[-8:]
        if not isinstance(name, str) or not name.strip() or len(name) > 64:
            raise ValueError("Registration requires a nonempty name of at most 64 characters")
        if username_prefix is not None and (not isinstance(username_prefix, str)
                or not username_prefix.strip() or len(username_prefix) > 64):
            raise ValueError("Username prefix must contain 1 to 64 characters of nonblank text")
        parent = _server_parent(value, mode)
        plan = self.ledger.onboarding(actor)
        choice = inherit or (plan["choice"] if plan else "fresh")
        if choice not in ("fresh", "pending", "pending-and-channels"):
            raise ValueError("Unsupported fork onboarding choice")
        source = parent if choice != "fresh" else None
        if choice != "fresh":
            if mode != "native" or not parent:
                raise ValueError("Pending import requires a verified direct parent; manual sidechats cannot import")
            try:
                previous = self.ledger.actor(actor)
            except LedgerError:
                previous = None
            if not plan and previous and previous["user_id"]:
                raise ValueError("Pending import is only available on first registration")
            if not plan:
                try:
                    parent_record = self.ledger.actor(parent)
                except LedgerError as error:
                    raise ValueError("Pending import requires the direct parent enrolled in this coordinator; choose fresh instead") from error
                if not parent_record["user_id"] or parent_record["mode"] != "native":
                    raise ValueError("Pending import requires the direct parent enrolled in this coordinator")
        record = self.ledger.enroll(actor, value, name, mode=mode, username_prefix=username_prefix,
                                    onboarding=(choice, source))
        response = self._register_backend(record, parent)
        agent = self._accept_registration(record, response)
        self.sync(actor)
        from .onboarding import Onboarding
        Onboarding(self, actor, self.ledger.onboarding(actor)["plan_id"]).run()
        plan = self.ledger.onboarding(actor)
        if plan["status"] == "complete" and (plan["snapshot"] or {}).get("attention_version") == 1:
            self.sync(actor)
        summary = {k: plan[k] for k in ("choice", "source_actor", "status")}
        if plan["snapshot"]:
            summary.update({key: plan["snapshot"][key] for key in
                            ("local_captured_at", "saved_started_at", "saved_finished_at")})
        summary["unavailable"] = sorted(key for key, state in plan["progress"].items() if state == "unavailable")
        return {**agent, "enrollment": capabilities, "onboarding": summary}

    def _register_backend(self, record, parent):
        options = {"username_prefix": record["username_prefix"]} if record.get("username_prefix") is not None else {}
        options["mode"] = record["mode"]
        if record["enrollment_id"] is not None:
            options["enrollment_id"] = record["enrollment_id"]
        if record["mode"] == "native":
            identity = record["identity"]
            edges = identity.get("evidence", {}).get("lineage_edges", [])
            if not edges and identity.get("parent_known") is True:
                edges = [{"session_id": identity["session_id"], "parent_session_id": identity.get("parent_session_id")}]
            if edges:
                provider = identity["provider"] + ":"
                options["lineage"] = [{"actor": provider + edge["session_id"],
                                       "parent": provider + edge["parent_session_id"] if edge["parent_session_id"] else None}
                                      for edge in edges]
        return self.backend.register(record["actor"], record["name"], parent, **options)

    def _accept_registration(self, record, response):
        agent = response.get("agent", response)
        cutoff = response.get("enrollment_seq")
        if record["enrollment_id"] is None:
            cutoff = record["enrollment_seq"]  # Legacy mapping: no historical cutoff can be invented.
        self.ledger.set_registration(record["actor"], agent["user_id"], cutoff)
        return agent

    def recover_onboarding(self, actor, plan_id, key, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Onboarding recovery requires an explicit reason")
        self.ledger.skip_onboarding_save(actor, plan_id, key)
        return {"actor": actor, "plan_id": plan_id, "step": key, "state": "skipped",
                "resume": "Repeat register to finish the frozen onboarding plan"}

    def sync(self, actor, *, max_pages=20):
        record = self.ledger.actor(actor)
        if not record:
            raise RuntimeError("This session is not enrolled; invoke Murmur and register explicitly")
        if not record.get("user_id"):
            value = record["identity"]
            parent = _server_parent(value, record["mode"])
            agent = self._register_backend(record, parent)
            self._accept_registration(record, agent)
        plan = self.ledger.onboarding(actor)
        if plan and plan["status"] != "complete" and plan["choice"] == "pending-and-channels":
            snapshot = self.ledger.capture_pending(actor, plan["plan_id"])
            if snapshot.get("attention_version") == 1:
                # Import subscription policy before the first feed ingestion.
                # A resumed/uncertain join must not ingest under default All.
                self.ledger.sync_conversations(actor, self.backend.conversations(actor))
                return
        # Each bounded request commits access, attention and cursor together.
        # Yield to the daemon between ticks if a busy server has a large backlog.
        stale, last_stale = 0, None
        for _ in range(max_pages):
            cursor = self.ledger.cursor(actor)
            response = self.backend.changes(actor, cursor["cursor"], cursor.get("generation"))
            try:
                self.ledger.apply_feed(actor, response)
            except StaleFeedError as error:
                stale, last_stale = stale + 1, error
                if stale >= 3:
                    raise StaleFeedError("Feed state repeatedly moved backwards; check server restore/reconciliation") from error
                continue
            last_stale = None
            if not response["more"]:
                return
        if last_stale is not None:
            raise StaleFeedError("Concurrent feed synchronization could not settle within its bound") from last_stale

    def inbox(self, actor, starred_only=False):
        for retry in range(2):
            self.sync(actor)
            try:
                return self._inbox_snapshot(actor, starred_only)
            except ConversationChanged:
                if retry:
                    raise

    def _inbox_snapshot(self, actor, starred_only):
        summaries = {item["id"]: dict(item, starred_count=0, starred=[])
                     for item in self.ledger.inbox(actor)}
        chats = {chat["id"]: chat for chat in self.ledger.conversations(actor)}
        for saved in self.backend.saved(actor):
            if saved.get("unavailable"):
                summary = summaries.setdefault(None, {
                    "id": None, "name": "Unavailable saved messages", "kind": "unavailable",
                    "role": None, "muted": True, "unread_count": 0, "create_at": 0,
                    "starred_count": 0, "starred": []})
                summary["starred"].append({"post_id": saved["post_id"], "unavailable": True})
            else:
                post = saved["post"]
                if post["id"] != saved["post_id"]:
                    raise RuntimeError("Server returned the wrong saved post")
                chat = chats.get(post["channel_id"])
                descriptor = conversation_descriptor(saved.get("conversation"), post["channel_id"])
                if descriptor["role"] is None:
                    if chat is not None:
                        raise ConversationChanged("Saved conversation changed; retry inbox")
                    chat = {**descriptor, "muted": True}
                elif chat is None or chat["role"] != descriptor["role"]:
                    # Access can change between the conversation and saved
                    # snapshots. Do not invent membership or hide saved work.
                    raise ConversationChanged("Saved conversation changed; retry inbox")
                summary = summaries.setdefault(chat["id"], {
                    "id": chat["id"], "name": chat["name"], "kind": chat["kind"],
                    "role": chat["role"], "muted": bool(chat["muted"]),
                    "notification_mode": chat.get("notification_mode"),
                    "followed_thread_count": len(self.ledger.following(actor, chat["id"])),
                    "unread_count": 0, "starred_count": 0, "starred": []})
                summary["starred"].append({
                    "post_id": post["id"], "root_id": post["root_id"],
                    "author": post["author"], "create_at": post["create_at"],
                    "preview": preview(post)})
                if not summary["unread_count"] and post["create_at"] >= summary.get("create_at", 0):
                    summary.update(latest_post_id=post["id"], author=post["author"],
                                   create_at=post["create_at"], preview=summary["starred"][-1]["preview"])
            summary["starred_count"] += 1
        summaries = [item for item in summaries.values() if not starred_only or item["starred_count"]]
        for summary in summaries:
            summary["starred"].sort(key=lambda p: (-p.get("create_at", 0), p["post_id"]))
            if summary.get("latest_post_id") and summary["unread_count"]:
                candidates = self.ledger.preview_candidates(actor, summary["id"])
                if candidates:
                    first = candidates[0]
                    summary.update(latest_post_id=first["id"], author=first["author"], create_at=first["create_at"])
                summary["preview"] = "[message no longer available]"
                for candidate in candidates:
                    try:
                        post = self.show(actor, candidate["id"])
                    except BackendError as error:
                        if error.status != 404:
                            raise
                        continue
                    if post["id"] != candidate["id"] or post["channel_id"] != summary["id"]:
                        raise RuntimeError("Server returned a preview from the wrong conversation")
                    summary.update(latest_post_id=post["id"], author=post["author"], create_at=post["create_at"],
                                   preview=preview(post))
                    break
        return sorted(summaries, key=lambda c: (-c["create_at"], c["id"] or ""))

    def show(self, actor, post_id):
        """Reopen an exact canonical anchor without changing consumption."""
        self.ledger.actor(actor)
        post = self.backend.post(actor, post_id)
        if post["id"] != post_id:
            raise RuntimeError("Server returned the wrong post")
        return post

    def set_saved(self, actor, post_id, starred):
        """Mattermost owns saves; no read, arrival, attempt or causal mutations."""
        self.ledger.actor(actor)
        if type(starred) is not bool:
            raise ValueError("Starred must be true or false")
        return self.backend.set_saved(actor, post_id, starred)

    def read(self, actor, channel, root=None, limit=20):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Read limit must be between 1 and 1000")
        self.sync(actor)
        if not any(chat["id"] == channel for chat in self.ledger.conversations(actor)):
            raise ValueError("Conversation has no tracked inbox; use history to preview or join to track future arrivals")
        if root:
            anchor = self.backend.root(actor, root)
            if anchor["id"] != root:
                raise RuntimeError("Server returned the wrong thread root")
            if anchor["channel_id"] != channel:
                raise ValueError("Thread belongs to another conversation")
            if anchor.get("root_id"):
                raise ValueError("Requested post is a reply, not a thread root; use its root_id or murmur reply POST_ID")
        selected = self.ledger.unread_posts(actor, channel, root, limit)
        posts = []
        while selected:
            for record in selected:
                try:
                    post = self.backend.post(actor, record["id"])
                except BackendError as error:
                    if error.status != 404:
                        raise
                    self.ledger.mark_unavailable(actor, record["id"])
                    continue
                if post["id"] != record["id"] or post["channel_id"] != channel:
                    raise RuntimeError("Server returned a post from the wrong conversation")
                if root and post["id"] != root and post.get("root_id") != root:
                    raise RuntimeError("Server returned a post from the wrong thread")
                posts.append(post)
                if len(posts) == limit:
                    return posts
            # Deleted IDs no longer occupy the limited selection. Live messages
            # remain unread until CLI output succeeds, so exclude those fetched.
            fetched = {post["id"] for post in posts}
            selected = [record for record in self.ledger.unread_posts(actor, channel, root, limit)
                        if record["id"] not in fetched]
        return posts

    def prepare_read(self, actor, channel=None, root=None, limit=20, post_id=None):
        """Prepare canonical output and actor-bound exact acknowledgement IDs."""
        if post_id is None:
            if channel is None:
                raise ValueError("Select a conversation or exact unread post")
            posts = self.read(actor, channel, root, limit)
        else:
            if channel is not None or root is not None:
                raise ValueError("An exact post cannot be combined with a conversation or thread")
            self.sync(actor)
            records = self.ledger._unread(actor, post_id=post_id, limit=1)
            if not records:
                raise ValueError("Post is not a tracked accessible unread message")
            try:
                post = self.show(actor, post_id)
            except BackendError as error:
                if error.status == 404:
                    self.ledger.mark_unavailable(actor, post_id)
                raise
            if post["channel_id"] != records[0]["channel_id"] or post.get("root_id", "") != records[0]["root_id"]:
                raise RuntimeError("Server returned a post with a different canonical destination")
            posts = [post]
        return MappingProxyType({"actor": actor, "posts": posts, "ack_ids": tuple(post["id"] for post in posts)})

    def ack_read(self, actor, selection):
        if (selection.get("actor") != actor or not isinstance(selection.get("ack_ids"), tuple)
                or selection["ack_ids"] != tuple(post["id"] for post in selection["posts"])):
            raise LedgerError("Prepared read belongs to another actor or selection")
        self.ledger.mark_read(actor, selection["ack_ids"])

    def history(self, actor, channel, root=None, limit=20, before=None):
        self.ledger.actor(actor)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if root:
            page = self.backend.thread_page(actor, root, limit, before)
            anchor = page["root"]
            if anchor["id"] != root or anchor.get("root_id"):
                raise RuntimeError("Server returned the wrong thread root")
            if "conversation" in page:
                conversation_descriptor(page["conversation"], channel)
            if anchor["channel_id"] != channel or any(post["channel_id"] != channel for post in page["posts"]):
                raise RuntimeError("Thread belongs to another conversation")
            if any(post["id"] != root and post.get("root_id") != root for post in page["posts"]):
                raise RuntimeError("Server returned a post from the wrong thread")
            return page
        page = self.backend.posts(actor, channel, limit, before)
        if "conversation" in page:
            conversation_descriptor(page["conversation"], channel)
        if any(post["channel_id"] != channel for post in page["posts"]):
            raise RuntimeError("Server returned history from another conversation")
        return page

    def context(self, actor, post_id, limit=20, before=None):
        """Exact anchor plus bounded latest thread context; never consume mail."""
        post = self.show(actor, post_id)
        root = post.get("root_id") or post["id"]
        page = self.history(actor, post["channel_id"], root, limit, before)
        return {"post": post, "context": page}

    def reply(self, actor, post_id, message, files=()):
        """Derive the canonical destination once, then use the normal send journal."""
        post = self.show(actor, post_id)
        return self.send(actor, post["channel_id"], message,
                         root=post.get("root_id") or post["id"], files=files)

    def send(self, actor, channel, message, *, root="", files=()):
        if not files:
            return self.mutate(actor, "post", channel_id=channel, message=message, root_id=root)
        limits = self.backend.attachment_limits()
        if len(files) > limits["max_files_per_post"]:
            raise ValueError(f"At most {limits['max_files_per_post']} attachments can be sent in one post")
        if root:
            anchor = self.backend.root(actor, root)
            if anchor.get("id") != root or anchor.get("channel_id") != channel or anchor.get("root_id"):
                raise ValueError("Attachment reply must use a genuine root in its destination conversation")
        sources = [describe_source(path, limits["max_file_size"]) for path in files]
        operation = self.ledger.prepare_attachment_outbound(actor,
            {"channel_id": channel, "message": message, "root_id": root}, sources,
            auto_follow_after_seq=self._send_attention_boundary(actor, channel))
        return self._execute_attachment(operation)

    def download(self, actor, post_id, file_id, output):
        self.ledger.actor(actor)
        return self.backend.download(actor, post_id, file_id, output)

    def threads(self, actor, channel, limit=20, before=None):
        self.ledger.actor(actor)
        page = self.backend.threads(actor, channel, limit, before)
        descriptor = conversation_descriptor(page.get("conversation"), channel)
        if any(summary["channel_id"] != channel for summary in page["threads"]):
            raise RuntimeError("Server returned threads from another conversation")
        if descriptor["role"] is None:
            for summary in page["threads"]:
                summary["unread_count"] = None
                summary["following"] = False
            page["notification_mode"] = None
            return page
        self.sync(actor)
        chat = next((c for c in self.ledger.conversations(actor) if c["id"] == channel), None)
        if chat is None or chat["role"] != descriptor["role"]:
            raise ConversationChanged("Thread conversation changed; retry threads")
        counts = {}
        for post in self.ledger._unread(actor, channel_id=channel):
            root = post["root_id"] or post["id"]
            counts[root] = counts.get(root, 0) + 1
        followed = {f["root_id"] for f in self.ledger.following(actor, channel)}
        for summary in page["threads"]:
            summary["unread_count"] = counts.get(summary["id"], 0)
            summary["following"] = summary["id"] in followed
        page["notification_mode"] = self.ledger.notification_mode(actor, channel)["mode"]
        return page

    def attention_boundary(self, actor, channel):
        """Require the versioned canonical boundary before enabling future attention."""
        self.ledger.actor(actor)
        health = self.backend.request("GET", "/health")
        capabilities = health.get("capabilities") if isinstance(health, dict) else None
        capability = capabilities.get("attention_boundaries") if isinstance(capabilities, dict) else None
        if (not isinstance(capability, dict) or type(capability.get("version")) is not int
                or capability["version"] != 1):
            raise BackendError("This server requires attention-boundary version 1; upgrade the server before enabling notifications")
        value = self.backend.attention_boundary(actor, channel)
        if (not isinstance(value, dict) or value.get("conversation_id") != channel
                or type(value.get("after_seq")) is not int or value["after_seq"] < 0):
            raise BackendError("Server returned an invalid canonical attention boundary")
        return value["after_seq"]

    def _full_member(self, actor, channel):
        self.ledger.actor(actor)
        chat = next((c for c in self.ledger.conversations(actor) if c["id"] == channel), None)
        if chat is None or chat["role"] != "member" or chat.get("leave_op_id"):
            raise LedgerError("Thread notifications require full participation; join the conversation, check chats and retry")
        return chat

    def _send_attention_boundary(self, actor, channel):
        chat = next((c for c in self.ledger.conversations(actor) if c["id"] == channel), None)
        if chat is None or chat["role"] != "member":
            # A newly created/joined conversation may not have reached this
            # host's next background snapshot yet. Learn membership before
            # journaling the post, never manufacture it from a read descriptor.
            self.sync(actor)
        self._full_member(actor, channel)
        return self.attention_boundary(actor, channel)

    def notify(self, actor, channel, mode):
        self.ledger.actor(actor)
        if mode not in ("all", "threads", "off"):
            raise ValueError("Notification mode must be all, threads or off")
        if mode == "off":
            return {"id": channel, **self.ledger.set_notification_mode(actor, channel, mode)}
        chat = next((c for c in self.ledger.conversations(actor) if c["id"] == channel), None)
        if chat is not None and chat["role"] == "observer":
            # A dormant preference is preserved for a later explicit join;
            # observer access never activates notifications or a follow.
            return {"id": channel, **self.ledger.set_notification_mode(actor, channel, mode)}
        self._full_member(actor, channel)
        previous = self.ledger.notification_mode(actor, channel)
        if previous["mode"] == mode:
            return {"id": channel, **self.ledger.set_notification_mode(actor, channel, mode,
                expected_revision=previous["revision"], expected_membership_revision=previous["membership_revision"])}
        boundary = self.attention_boundary(actor, channel)
        value = self.ledger.set_notification_mode(actor, channel, mode, after_seq=boundary,
            expected_revision=previous["revision"], expected_membership_revision=previous["membership_revision"])
        return {"id": channel, **value}

    def _canonical_thread(self, actor, post_id):
        post = self.show(actor, post_id)
        root_id = post.get("root_id") or post["id"]
        root = self.backend.root(actor, root_id)
        if (root.get("id") != root_id or root.get("channel_id") != post["channel_id"]
                or root.get("root_id") or root.get("system") or root.get("type", "").startswith("system_")):
            raise RuntimeError("Server returned a different canonical thread root")
        return {"channel_id": post["channel_id"], "root_id": root_id}

    def follow(self, actor, post_id):
        self.ledger.actor(actor)
        # Resolve known aliases before any network work. Repeating a follow
        # preserves its original cutoff and works while offline.
        alias = self.ledger.resolve_thread_alias(actor, post_id)
        if alias:
            self._full_member(actor, alias["channel_id"])
            previous = self.ledger.thread_attention(actor, alias["channel_id"], alias["root_id"])
            if previous["followed"]:
                # Reaffirming an existing follow keeps its visible cutoff but
                # fences older automatic intents that have not settled yet.
                return self.ledger.follow_thread(actor, alias["channel_id"], alias["root_id"], previous["after_seq"],
                    expected_revision=previous["revision"], expected_membership_revision=previous["membership_revision"],
                    expected_attention_revision=previous["attention_revision"])
        revisions = {chat["id"]: self.ledger.notification_mode(actor, chat["id"])
                     for chat in self.ledger.conversations(actor) if chat["role"] == "member"}
        target = self._canonical_thread(actor, post_id)
        if alias is not None and alias != target:
            raise RuntimeError("Canonical post changed its conversation or thread")
        channel, root = target["channel_id"], target["root_id"]
        if channel not in revisions:
            raise LedgerError("Thread notifications require full participation; check chats and retry")
        revision = revisions[channel]
        previous = self.ledger.thread_attention(actor, channel, root)
        boundary = self.attention_boundary(actor, channel)
        value = self.ledger.follow_thread(actor, channel, root, boundary,
            expected_revision=previous["revision"], expected_membership_revision=revision["membership_revision"],
            expected_attention_revision=revision["revision"])
        self.ledger.remember_thread_alias(actor, post_id, channel, root)
        return value

    def unfollow(self, actor, post_id):
        self.ledger.actor(actor)
        target = self.ledger.resolve_thread_alias(actor, post_id)
        if target is None:
            try:
                target = self._canonical_thread(actor, post_id)
            except BackendError as error:
                if error.status in (400, 403, 404):
                    raise
                raise LedgerError("Cannot resolve this post offline; use a canonical root from following, then retry unfollow") from error
            except OSError as error:
                raise LedgerError("Cannot resolve this post offline; use a canonical root from following, then retry unfollow") from error
        value = self.ledger.unfollow_thread(actor, target["channel_id"], target["root_id"])
        self.ledger.remember_thread_alias(actor, post_id, target["channel_id"], target["root_id"])
        return value

    def following(self, actor, channel=None):
        self.ledger.actor(actor)
        if channel is not None:
            self.ledger.notification_mode(actor, channel)
        return self.ledger.following(actor, channel)

    def channels(self, actor, query="", limit=20, after=None):
        self.ledger.actor(actor)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Channel limit must be between 1 and 100")
        page = self.backend.channels(actor, query, limit, after)
        for channel in page["channels"]:
            descriptor = conversation_descriptor(channel, channel["id"])
            if descriptor["kind"] != "channel" or not descriptor["open"]:
                raise RuntimeError("Channel directory returned a non-public channel")
        return page

    def conversation_directory(self, actor, query="", kind=None, agent=None, limit=20, after=None):
        """Discover managed references without membership, feed or inbox changes."""
        self.ledger.actor(actor)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Conversation limit must be between 1 and 100")
        if not isinstance(query, str) or kind not in (None, "dm", "channel"):
            raise ValueError("Choose a text query and an optional dm or channel kind")
        if agent is not None and (not isinstance(agent, str) or not agent.startswith(("codex:", "claude:"))):
            raise ValueError("Use an agent actor ID from murmur agents")
        page = self.backend.conversation_directory(actor, query, kind, agent, limit, after)
        if (not isinstance(page, dict) or not isinstance(page.get("conversations"), list)
                or len(page["conversations"]) > limit or "next_after" not in page
                or page["next_after"] is not None and (not isinstance(page["next_after"], str) or not page["next_after"])):
            raise RuntimeError("Invalid managed conversation directory")
        seen = set()
        for chat in page["conversations"]:
            if not isinstance(chat, dict) or not isinstance(chat.get("id"), str) or not chat["id"]:
                raise RuntimeError("Invalid canonical conversation descriptor")
            descriptor = conversation_descriptor(chat, chat["id"])
            if descriptor["id"] in seen or not isinstance(descriptor.get("purpose"), str):
                raise RuntimeError("Invalid managed conversation directory")
            seen.add(descriptor["id"])
            participants = descriptor.get("participants")
            if not isinstance(participants, list):
                raise RuntimeError("Invalid managed conversation participants")
            for participant in participants:
                if (not isinstance(participant, dict) or participant.get("kind") not in ("agent", "human")
                        or not isinstance(participant.get("id"), str) or not participant["id"]
                        or not isinstance(participant.get("name"), str)
                        or not isinstance(participant.get("username"), str)
                        or participant["kind"] == "agent" and not participant["id"].startswith(("codex:", "claude:"))):
                    raise RuntimeError("Invalid managed conversation participants")
        return page

    def agents(self, query="", limit=20, after=None, scope="server"):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Agent limit must be between 1 and 100")
        if scope == "server":
            return {**self.backend.agent_page(query, limit, after), "scope": "server"}
        if scope != "local":
            raise ValueError("Agent scope must be server or local")
        needle = query.strip().casefold()
        accounts = [{"id": a["actor"], "name": a["name"], "mode": a["mode"]}
                    for a in self.ledger.actors()
                    if needle in a["actor"].casefold() or needle in a["name"].casefold()]
        accounts.sort(key=lambda a: a["id"])
        if after:
            index = next((i for i, a in enumerate(accounts) if a["id"] == after), None)
            if index is None:
                raise ValueError("Agent cursor no longer matches this scope/query; omit --after to restart")
            accounts = accounts[index + 1:]
        page = accounts[:limit]
        return {"agents": page, "next_after": page[-1]["id"] if len(accounts) > limit else None,
                "scope": "local"}

    def mutate(self, actor, action, **payload):
        """Commit intent before HTTP. A timeout holds the operation for inspection."""
        options = {"auto_follow_after_seq": self._send_attention_boundary(actor, payload["channel_id"])} if action == "post" else {}
        operation = self.ledger.prepare_outbound(actor, dict(action=action, **payload), **options)
        return self._execute(operation)

    def _auto_follow_pending(self, operation):
        return any(i["op_id"] == operation["op_id"] for i in self.ledger.pending_auto_follows(operation["actor"]))

    def _canonical_outbound_post(self, operation, result):
        """Authenticated operation proof binds IDs even after a post is deleted."""
        payload = operation["payload"]
        post = result.get("post") if isinstance(result, dict) else None
        user_id = self.ledger.actor(operation["actor"])["user_id"]
        if isinstance(post, dict):
            if (not isinstance(post.get("id"), str) or not post["id"]
                    or post.get("channel_id") != payload["channel_id"]
                    or post.get("root_id", "") != payload.get("root_id", "")
                    or post.get("user_id") != user_id or post.get("message") != payload.get("message")
                    or post.get("system") or post.get("type", "").startswith("system_")):
                raise BackendError("Canonical post differs from its frozen outbound intent", uncertain=True)
            return post
        metadata = operation_metadata(result)
        if (not isinstance(metadata.get("post_id"), str) or not metadata["post_id"]
                or metadata.get("channel_id") != payload["channel_id"]):
            raise BackendError("Canonical post proof differs from its frozen destination", uncertain=True)
        return {"id": metadata["post_id"], "channel_id": payload["channel_id"],
                "root_id": payload.get("root_id", ""), "user_id": user_id}

    def _complete_auto_follow(self, operation, result):
        if self._auto_follow_pending(operation):
            self.ledger.complete_auto_follow(operation["op_id"], self._canonical_outbound_post(operation, result))

    def _refresh_join_access(self, operation):
        try:
            self.ledger.sync_conversations(operation["actor"], self.backend.conversations(operation["actor"]))
        except (BackendError, OSError, ValueError) as error:
            # The canonical join is already sealed. A failed access refresh
            # must not turn that success into an uncertain operation to retry.
            from .hooks import initialize, record_error
            warning = "Join completed; access refresh failed. Run chats before send/notify: " + str(error)
            initialize(self.ledger.db)
            record_error(self.ledger, operation["actor"], "join_access_refresh", warning)
            return warning
        return None

    def _execute(self, operation):
        payload = dict(operation["payload"])
        action = payload.pop("action")
        try:
            result = self.backend.mutate(action, operation["actor"], operation["op_id"], **payload)
            if action == "post" and self._auto_follow_pending(operation):
                self._canonical_outbound_post(operation, result)
        except BackendError as error:
            self.ledger.set_outbound(operation["op_id"],
                                     "uncertain" if error.uncertain else "failed", error=str(error))
            raise
        self.ledger.set_outbound(operation["op_id"], "complete", result=operation_metadata(result))
        if action == "post":
            self._complete_auto_follow(operation, result)
        elif action == "join":
            # A confirmed observer-to-member transition must be available to
            # immediate send/notify commands, before the next daemon feed.
            warning = self._refresh_join_access(operation)
            if warning:
                result = dict(result, sync_warning=warning)
        return result

    def _upload_result(self, operation, result):
        payload = operation["payload"]
        if not isinstance(result, dict):
            raise BackendError("Invalid upload receipt", uncertain=True)
        info = result.get("file")
        file_id = result.get("file_id") or (info.get("id") if isinstance(info, dict) else None)
        if (not isinstance(file_id, str) or not file_id
                or result.get("channel_id") != payload["channel_id"]):
            raise BackendError("Upload receipt does not match its frozen destination", uncertain=True)
        source = payload["source"]
        if (not isinstance(info, dict) or info.get("id") != file_id
                or not isinstance(info.get("name"), str) or info["name"] != source["filename"]
                or type(info.get("size")) is not int or info["size"] != source["size"]
                or not isinstance(info.get("mime_type"), str) or not info["mime_type"]):
            raise BackendError("Upload receipt differs from its frozen file", uncertain=True)
        return {"file_id": file_id, "channel_id": payload["channel_id"], "file": info}

    def _dispatch_upload(self, operation, can_dispatch=None):
        if can_dispatch is not None and not can_dispatch():
            return
        parent = self.ledger.outbound(operation["payload"]["parent_id"])
        if parent["state"] == "failed":
            self.ledger.settle_outbound(operation["op_id"], "failed", error="Parent attachment post failed before upload")
            return
        if (parent["actor"] != operation["actor"] or parent["state"] != "pending"
                or parent["payload"].get("action") != "attachment_post"
                or operation["op_id"] not in parent["payload"]["uploads"]
                or parent["payload"]["channel_id"] != operation["payload"]["channel_id"]):
            raise LedgerError("Upload no longer belongs to a pending attachment post")
        for sibling in self._attachment_children(parent):
            if sibling["op_id"] == operation["op_id"]:
                break
            if sibling["state"] != "complete":
                return
        try:
            data = source_bytes(operation["payload"]["source"])
        except (ValueError, OSError) as error:
            # A competing dispatcher may already own a valid buffer. Fail only
            # when this caller can claim the still-unstarted stage itself.
            if self.ledger.claim_outbound(operation["op_id"], admit=can_dispatch):
                self.ledger.settle_outbound(operation["op_id"], "failed",
                    error="Local source invalid before upload: " + str(error))
                self.ledger.fail_attachment_outbound(parent["op_id"], "Local source invalid before upload: " + str(error))
                raise
            return
        if not self.ledger.claim_outbound(operation["op_id"], admit=can_dispatch):
            return
        try:
            result = self.backend.upload(operation["actor"], operation["op_id"],
                operation["payload"]["channel_id"], operation["payload"]["source"], data)
            metadata = self._upload_result(operation, result)
        except BackendError as error:
            settled = self.ledger.settle_outbound(operation["op_id"],
                "uncertain" if error.uncertain else "failed", error=str(error))
            if settled and not error.uncertain:
                self.ledger.fail_attachment_outbound(parent["op_id"], "Attachment upload failed: " + str(error))
            raise
        self.ledger.settle_outbound(operation["op_id"], "complete", result=metadata)

    def _attachment_children(self, operation):
        children = [self.ledger.outbound(op_id) for op_id in operation["payload"]["uploads"]]
        for child in children:
            payload = child["payload"]
            if (child["actor"] != operation["actor"] or payload.get("action") != "upload"
                    or payload.get("parent_id") != operation["op_id"]
                    or payload.get("channel_id") != operation["payload"]["channel_id"]):
                raise LedgerError("Attachment post has a mismatched upload operation")
        return children

    def _validate_attachment_post(self, operation, result, file_ids):
        post = result.get("post") if isinstance(result, dict) else None
        payload = operation["payload"]
        if (not isinstance(post, dict) or not post.get("id")
                or post.get("channel_id") != payload["channel_id"]
                or post.get("root_id", "") != payload["root_id"]
                or post.get("message") != payload["message"]
                or post.get("user_id") != self.ledger.actor(operation["actor"])["user_id"]
                or post.get("file_ids") != file_ids):
            raise BackendError("Canonical attachment post differs from its frozen intent", uncertain=True)

    def _advance_attachment(self, operation, can_dispatch=None):
        operation = self.ledger.outbound(operation["op_id"])
        if operation["state"] == "uncertain":
            self._reconcile_one(operation)
            return
        if operation["state"] != "pending":
            return
        children = self._attachment_children(operation)
        for child in children:
            if child["state"] == "failed":
                self.ledger.fail_attachment_outbound(operation["op_id"],
                                                     "Attachment upload failed: " + str(child["error"]))
                return
            if child["state"] != "complete":
                if child["state"] == "pending":
                    self._dispatch_upload(child, can_dispatch)
                else:
                    self._reconcile_one(child)
                return  # One bulk request/reconciliation per background parent tick.
        # Mattermost canonicalizes attachment IDs before saving the native post.
        file_ids = sorted(child["result"]["file_id"] for child in children)
        if not file_ids or len(set(file_ids)) != len(file_ids):
            raise LedgerError("Attachment post requires distinct confirmed uploaded files")
        payload = {key: operation["payload"][key] for key in ("channel_id", "message", "root_id", "causal")
                   if key in operation["payload"]}
        if not self.ledger.claim_outbound(operation["op_id"], result={"file_ids": file_ids}, admit=can_dispatch):
            return
        try:
            result = self.backend.mutate("post", operation["actor"], operation["op_id"],
                                         file_ids=file_ids, **payload)
            self._validate_attachment_post(operation, result, file_ids)
        except BackendError as error:
            self.ledger.settle_outbound(operation["op_id"], "uncertain" if error.uncertain else "failed",
                                     result={"file_ids": file_ids}, error=str(error))
            raise
        self.ledger.settle_outbound(operation["op_id"], "complete", result=operation_metadata(result))
        self._complete_auto_follow(operation, result)
        return result

    def _execute_attachment(self, operation):
        # An explicit CLI send may drive its own small sequence; background ticks
        # use only one _advance_attachment call, retaining fair host scheduling.
        for _ in range(len(operation["payload"]["uploads"]) + 1):
            try:
                result = self._advance_attachment(operation)
            except (BackendError, OSError, ValueError) as error:
                current = self.ledger.outbound(operation["op_id"])
                if current["state"] == "complete":
                    return {"op_id": current["op_id"], "status": "complete", **current["result"]}
                if current["state"] == "failed":
                    raise BackendError(current["error"]) from error
                raise BackendError(f"Attachment send {operation['op_id']} is held: {error}; "
                                   "inspect doctor instead of resending", uncertain=True) from error
            current = self.ledger.outbound(operation["op_id"])
            if current["state"] == "complete":
                return result or {"op_id": current["op_id"], "status": "complete", **current["result"]}
            if current["state"] == "failed":
                raise BackendError(current["error"])
            if current["state"] == "uncertain" or any(child["state"] == "uncertain"
                    for child in self._attachment_children(current)):
                break
        raise BackendError(f"Attachment send {operation['op_id']} awaits confirmation; "
                           "inspect doctor instead of resending", uncertain=True)

    def _reconcile_one(self, operation):
        try:
            result = self.backend.operation(operation["actor"], operation["op_id"])
        except BackendError as error:
            if error.status == 404:
                self.ledger.settle_outbound(operation["op_id"], "uncertain",
                    result=operation["result"], error="Server has not confirmed this operation")
                return
            raise
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise BackendError("Server returned an invalid operation receipt", uncertain=True)
        status = result["status"]
        if status not in ("complete", "failed"):
            return
        metadata = None
        if status == "complete":
            if operation["payload"].get("action") == "upload":
                metadata = self._upload_result(operation, result.get("result") or {})
            else:
                metadata = operation_metadata(result.get("result"))
                if operation["payload"].get("action") == "attachment_post":
                    if "post" in (result.get("result") or {}):
                        self._validate_attachment_post(operation, result["result"], operation["result"]["file_ids"])
                    if (metadata.get("channel_id") != operation["payload"]["channel_id"]
                            or not metadata.get("post_id")):
                        raise BackendError("Post recovery differs from its frozen destination", uncertain=True)
                if self._auto_follow_pending(operation):
                    self._canonical_outbound_post(operation, result.get("result"))
        settled = self.ledger.settle_outbound(operation["op_id"], status, result=metadata, error=result.get("error"))
        if status == "complete" and operation["payload"].get("action") in ("post", "attachment_post"):
            self._complete_auto_follow(operation, result.get("result"))
        elif settled and status == "complete" and operation["payload"].get("action") == "join":
            self._refresh_join_access(operation)

    def reconcile_outbound(self, actor, *, limit=None, operations=None, can_dispatch=None):
        operations = self.ledger.unresolved_outbounds(actor, limit) if operations is None else operations
        for operation in operations:
            operation = self.ledger.outbound(operation["op_id"])
            if operation["state"] not in ("pending", "uncertain"):
                continue
            action = operation["payload"].get("action")
            if action == "attachment_post":
                self._advance_attachment(operation, can_dispatch)
            elif action == "upload" and operation["state"] == "pending":
                self._dispatch_upload(operation, can_dispatch)
            else:
                self._reconcile_one(operation)
        # A crash can occur after durable canonical completion and before the
        # local follow is activated. The sealed IDs suffice; do not fetch a
        # potentially deleted post or move the original capture boundary.
        for intent in self.ledger.pending_auto_follows(actor):
            operation = self.ledger.outbound(intent["op_id"])
            if operation["state"] == "complete":
                self._complete_auto_follow(operation, operation["result"])

    def deliver(self, identity, *, expected_identity=None):
        if self.transport is None:
            return None
        from .native import format_doorbell
        actor = actor_key(identity)
        expected = identity_dict(identity if expected_identity is None else expected_identity)
        if actor_key(expected) != actor:
            raise LedgerError("Verified idle recipient differs from the enrolled actor")
        if self.ledger.actor(actor)["mode"] == "manual":
            return None
        account = self.ledger.actor(actor)
        enrollment = {key: account[key] for key in ("enrollment_id", "enrollment_seq", "user_id")}
        attempt = self.ledger.create_attempt(actor, routing_binding(identity),
                                             expected_identity=expected,
                                             refreshed_identity=identity_dict(identity),
                                             expected_enrollment=enrollment)
        if not attempt:
            return None
        names = {chat["id"]: chat["name"] for chat in self.ledger.conversations(actor)}
        message = format_doorbell([names.get(channel, "a conversation") for channel in attempt["channels"]], attempt["token"])
        prepare = getattr(self.transport, "prepare_idle_handoff", None)
        if prepare is not None:
            try:
                emission = prepare(identity, message, attempt["token"], state_root=Path(self.ledger.path).parent)
                if emission is not None and not self.ledger.seal_idle_handoff(
                        attempt["token"], actor, attempt["epoch"], emission, expected_enrollment=enrollment):
                    self.ledger.set_transport(attempt["token"], "rejected",
                                              {"detail": "Idle recipient changed before enqueue"})
                    return None
            except (ValueError, RuntimeError, OSError):
                self.ledger.set_transport(attempt["token"], "rejected",
                                          {"detail": "Idle handoff preparation failed before enqueue"})
                raise
        frozen = attempt["native_identity"]
        recipient = type(identity)(**frozen) if is_dataclass(identity) else frozen
        result = self.transport.deliver(recipient, message)
        self.ledger.set_transport(attempt["token"], result.status,
                                  {"detail": result.detail, "target": result.target})
        return result

    def recover_idle(self, attempt, expected_identity, *, enabled=None):
        """Wake an existing native queue entry; never append another input."""
        if (self.transport is None or attempt["mode"] != "idle" or attempt["status"] != "pending"
                or attempt.get("agent_seen_at") is not None
                or (attempt["native_identity"] or {}).get("provider") != "codex"):
            return None
        from .native import NativeIdentity
        recipient = NativeIdentity(**attempt["native_identity"])
        state = self.transport.owner_process_state(recipient, timeout=.5)
        if state["status"] == "alive":
            return None
        if state["status"] != "ended":
            return {"status": "held", "reason": state.get("reason", "Original owner is unverified")}
        if (attempt["emission"] or {}).get("kind") != "codex-idle-handoff-v1":
            return {"status": "held", "reason": "Legacy queue handoff has no frozen restart qualification"}
        probe = self.transport.probe_idle_recovery(recipient, attempt, state_root=Path(self.ledger.path).parent)
        if probe["status"] != "ready":
            return {"status": "held", "reason": probe.get("reason", "Native queue recovery is unverified")}
        if enabled is not None and not enabled():
            return None
        generation = hashlib.sha256(json.dumps(probe["successor"], sort_keys=True).encode()).hexdigest()
        claim = self.ledger.claim_idle_wake(attempt["token"], attempt, expected_identity,
                                           {"queue_id": probe["queue_id"], "generation": generation,
                                            "queue_payload_sha256": probe["queue_payload_sha256"]})
        if claim is None:
            return None
        if enabled is not None and not enabled():
            self.ledger.finish_idle_wake(attempt["token"], claim["action_id"],
                                        {"status": "rejected", "detail": "Delivery disabled before wake"})
            return None
        try:
            # The adapter rechecks the exact queue/generation/gate immediately
            # before the URL. Loss of its response permits another wake only.
            result = self.transport.wake_idle_recovery(recipient.session_id, probe,
                before_wake=lambda: (self.ledger.idle_wake_current(
                    attempt["token"], claim["action_id"], expected_identity)
                    and (enabled is None or enabled())))
        except (ValueError, RuntimeError, OSError):
            self.ledger.finish_idle_wake(attempt["token"], claim["action_id"],
                                        {"status": "uncertain", "detail": "Wake-only outcome unavailable"})
            raise
        self.ledger.finish_idle_wake(attempt["token"], claim["action_id"],
                                    {"status": result.status, "detail": result.detail})
        return {"status": result.status, "queue_id": probe["queue_id"], "detail": result.detail}

    def deliver_peer(self, identity, turn_key, *, expected_identity=None, enabled=None):
        """Journal a busy peer doorbell before its single possible socket write.

        Native history, not this transport result, settles admission. The
        original task remains the active turn throughout the handoff.
        """
        if self.transport is None:
            return None
        from .native import format_doorbell
        actor = actor_key(identity)
        expected = identity_dict(identity if expected_identity is None else expected_identity)
        if actor_key(expected) != actor:
            raise LedgerError("Verified peer recipient differs from the enrolled actor")
        if not self.ledger.notification_candidates(actor, limit=1):
            return None
        request = self.transport.prepare_peer_receipt(identity, turn_key)
        if enabled is not None and not enabled():
            return None
        binding = routing_binding(identity)
        attempt = self.ledger.claim_peer(actor, binding, turn_key, native_identity=identity_dict(identity),
                                         receipt_request=request, expected_identity=expected)
        if not attempt:
            return None
        try:
            names = {chat["id"]: chat["name"] for chat in self.ledger.conversations(actor)}
            message = format_doorbell([names.get(channel, "a conversation") for channel in attempt["channels"]],
                                      attempt["token"])
            emission = self.transport.peer_emission(identity, message, attempt["token"], request)
            if not self.ledger.mark_peer_emitted(attempt["token"], actor, binding, turn_key, emission):
                return None
        except (ValueError, RuntimeError, OSError):
            # No transport call has happened. This is positive no-write evidence.
            self.ledger.set_peer_transport(attempt["token"], "rejected", {"detail": "Preparation failed before socket delivery"})
            raise
        if enabled is not None and not enabled():
            self.ledger.set_peer_transport(attempt["token"], "rejected", {"detail": "Delivery disabled before socket delivery"})
            return None
        try:
            result = self.transport.deliver_peer(identity, message, attempt["token"], request, emission)
        except (ValueError, RuntimeError, OSError):
            # An exception after entering the native transport is ambiguous.
            self.ledger.set_peer_transport(attempt["token"], "uncertain", {"detail": "Native peer outcome unavailable"})
            raise
        self.ledger.set_peer_transport(attempt["token"], result.status,
                                       {"detail": result.detail, "target": result.target})
        return result

    def reconcile_context(self, actor, *, attempts=None):
        """Corroborate handoffs outside hooks, including after Stop or disabling delivery."""
        if self.transport is None or self.ledger.actor(actor)["mode"] == "manual":
            return []
        from .native import NativeIdentity
        unknown = []
        attempts = self.ledger.unconfirmed_contexts(actor) if attempts is None else attempts
        for attempt in attempts:
            identity = NativeIdentity(**attempt["native_identity"])
            peer = (attempt["receipt_request"] or {}).get("kind") == "claude-peer-v1"
            verify = self.transport.verify_peer_receipt if peer else self.transport.verify_context_receipt
            result = verify(identity, attempt, timeout=0.5)
            if result["status"] == "accepted":
                if peer:
                    self.ledger.confirm_peer(attempt["token"], actor, attempt["epoch"],
                                              attempt["expected_turn_key"], verification=result["evidence"])
                else:
                    self.ledger.confirm_context(attempt["token"], actor, attempt["epoch"],
                                                attempt["expected_turn_key"], attempt["tool_id"],
                                                verification=result["evidence"])
            elif result["status"] != "unknown":
                raise RuntimeError("Invalid native context receipt outcome")
            else:
                unknown.append({"token": attempt["token"], "reason": str(result.get("reason", "Unknown native receipt"))[:1000]})
        return unknown
