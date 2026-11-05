"""One frozen fork handoff, fenced by its durable plan ID.

The ledger commits references/preferences with their progress. This runner owns
external effects and distinguishes a rejected save from an unknown save outcome.
"""

import hashlib

from .backend import BackendError
from .ledger import LedgerError


class Onboarding:
    def __init__(self, coordinator, actor, plan_id):
        self.coordinator = coordinator
        self.ledger, self.backend = coordinator.ledger, coordinator.backend
        self.actor, self.plan_id = actor, plan_id

    def plan(self):
        return self.ledger.require_onboarding(self.actor, self.plan_id)

    def step(self, key, state, **values):
        return self.ledger.onboarding_step(self.actor, self.plan_id, key, state, **values)

    def post(self, post_id, key):
        self.plan()
        try:
            post = self.coordinator.show(self.actor, post_id)
        except BackendError as error:
            if error.status not in (403, 404):
                raise
            self.step(key, "unavailable")
            return None
        if (any(not isinstance(post.get(k), str) or not post[k] for k in ("id", "channel_id", "user_id"))
                or not isinstance(post.get("root_id", ""), str)
                or type(post.get("create_at")) is not int or type(post.get("update_at")) is not int
                or post.get("type", "").startswith("system_")):
            raise RuntimeError("Invalid canonical pending post")
        return post

    def run(self):
        plan = self.plan()
        if plan["status"] == "complete":
            return
        try:
            snapshot = self.ledger.capture_pending(self.actor, self.plan_id)
            if "saved_ids" not in snapshot:
                started = self.ledger.clock()
                saved = self.backend.saved(plan["source_actor"])
                ids = []
                for item in saved:
                    if not isinstance(item.get("post_id"), str) or not item["post_id"]:
                        raise RuntimeError("Invalid parent saved-post reference")
                    if not item.get("unavailable") and item.get("post", {}).get("id") != item["post_id"]:
                        raise RuntimeError("Parent saved post does not match its reference")
                    ids.append(item["post_id"])
                snapshot = self.ledger.capture_saved(self.actor, self.plan_id, ids, started, self.ledger.clock())
            for reference in snapshot["unread"]:
                key = "post:" + reference["id"]
                if key in self.plan()["progress"]:
                    continue
                post = self.post(reference["id"], key)
                if post is None:
                    continue
                if any(post.get(field, "") != reference[field] for field in ("channel_id", "root_id", "user_id")):
                    raise RuntimeError("Pending post's canonical destination or sender changed")
                self.step(key, "done", post=post)
            for post_id in snapshot["saved_ids"]:
                self.save(post_id)
            for channel in snapshot["channels"]:
                self.join(channel)
            self.ledger.finish_onboarding(self.actor, self.plan_id)
        except (RuntimeError, ValueError, OSError) as error:
            # A stale producer must not even replace the new plan's error.
            try:
                self.ledger.finish_onboarding(self.actor, self.plan_id, str(error))
            except LedgerError:
                if self.ledger.onboarding(self.actor)["plan_id"] == self.plan_id:
                    raise
            raise

    def save(self, post_id):
        key = "star:" + post_id
        state = self.plan()["progress"].get(key)
        if state in ("done", "unavailable", "skipped"):
            return
        if state == "started":
            # Absence could reflect a deliberate child unstar after a successful
            # write whose response was lost. Never automatically re-add it.
            current = {item["post_id"] for item in self.backend.saved(self.actor)}
            if post_id not in current:
                raise RuntimeError("Saved-copy outcome is uncertain for " + post_id +
                                   "; inspect the child star or explicitly skip this plan step")
        else:
            if self.post(post_id, key) is None:
                return
            if not self.step(key, "started"):
                raise LedgerError("Another registration started this saved copy; retry registration")
            try:
                self.backend.set_saved(self.actor, post_id, True)
            except BackendError as error:
                if not error.uncertain:
                    self.step(key, "unavailable" if error.status in (403, 404) else "failed")
                    if error.status in (403, 404):
                        return
                raise
        self.step(key, "done")

    def join(self, channel):
        from .coordinator import operation_metadata
        key = "channel:" + channel["id"]
        if self.plan()["progress"].get(key) == "done":
            return
        # Persist the step before preparing/sending external intent. This also
        # prevents a concurrent fresh choice from abandoning a started effect.
        self.step(key, "started")
        digest = hashlib.sha256((self.actor + "\0" + channel["id"]).encode()).hexdigest()[:40]
        op_id = "inherit-" + digest  # Preserve operation IDs from older plans.
        operation = self.ledger.find_outbound(op_id)
        existing = operation is not None
        if not existing:
            operation = self.ledger.prepare_outbound(self.actor, dict(action="join", channel_id=channel["id"]), op_id=op_id)
        if existing and operation["state"] != "complete":
            try:
                result = self.backend.operation(self.actor, op_id)
            except BackendError as error:
                if error.status != 404:
                    raise
            else:
                status = result.get("status")
                if status == "complete":
                    metadata = operation_metadata(result.get("result"))
                    if metadata.get("channel_id") != channel["id"]:
                        raise RuntimeError("Recovered join belongs to another conversation")
                    self.ledger.set_outbound(op_id, "complete", result=metadata)
                elif status == "failed":
                    self.ledger.set_outbound(op_id, "failed", error=result.get("error"))
                    raise RuntimeError("Inherited channel join failed: " + str(result.get("error", channel["id"])))
                elif status not in ("pending", "uncertain"):
                    raise RuntimeError("Invalid inherited join operation status")
                operation = self.ledger.outbound(op_id)
        self.plan()
        if operation["state"] != "complete":
            self.coordinator._execute(operation)
        if self.ledger.outbound(op_id)["result"].get("channel_id") != channel["id"]:
            raise RuntimeError("Inherited join result belongs to another conversation")
        # Versioned plans defer feed ingestion until copied preferences are in
        # place. Old frozen plans retain their original join/mute semantics.
        self.coordinator.sync(self.actor)
        snapshot = self.plan()["snapshot"]
        if snapshot.get("attention_version") == 1:
            after_seq = snapshot.get("attention_boundaries", {}).get(channel["id"])
            if after_seq is None:
                membership = self.ledger.notification_mode(self.actor, channel["id"])["membership_revision"]
                after_seq = self.coordinator.attention_boundary(self.actor, channel["id"])
                after_seq = self.ledger.freeze_channel_attention_boundary(
                    self.actor, self.plan_id, channel["id"], after_seq,
                    expected_membership_revision=membership)
            self.ledger.apply_channel_attention(
                self.actor, self.plan_id, channel["id"], channel["notification_mode"],
                channel["followed_threads"], after_seq)
            self.step(key, "done")
        else:
            self.step(key, "done", mute=channel["muted"])
