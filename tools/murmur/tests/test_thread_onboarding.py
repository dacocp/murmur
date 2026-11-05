"""Fork subscriptions are explicit copies, installed before the first feed page."""

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from murmur.coordinator import Coordinator
from murmur.ledger import Ledger, LedgerError
from murmur.native import NativeIdentity
from test_open_channels import OpenBackend


PARENT = NativeIdentity("codex", "11111111-1111-4111-8111-111111111111", parent_known=True)
CHILD = NativeIdentity("codex", "22222222-2222-4222-8222-222222222222", PARENT.session_id, True)


class SubscriptionBackend(OpenBackend):
    """Canonical posts and membership live separately from the real host ledger."""

    def __init__(self):
        super().__init__()
        self.chats.extend([
            dict(id="api", name="API", kind="channel", role="member"),
            dict(id="ops", name="Operations", kind="channel", role="member"),
            dict(id="reference", name="Reference", kind="channel", role="observer"),
        ])
        self.boundary_calls = []
        self.after_join = None
        self.after_boundary = None

    def attention_boundary(self, actor, channel):
        boundary = super().attention_boundary(actor, channel)
        self.boundary_calls.append((actor, channel, boundary["after_seq"]))
        if self.after_boundary:
            self.after_boundary(actor, channel)
        return boundary

    def mutate(self, action, actor, op_id, **payload):
        fresh = op_id not in self.operations
        result = super().mutate(action, actor, op_id, **payload)
        if action == "join" and fresh and self.after_join:
            self.after_join(actor, payload["channel_id"])
        return result


class ThreadOnboardingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "operations.sqlite3"
        self.ledger = Ledger(self.path, clock=lambda: 100)
        self.addCleanup(lambda: self.ledger.close())
        self.backend = SubscriptionBackend()
        self.runtime = Coordinator(self.ledger, self.backend)
        self.runtime.register(PARENT, "Parent")
        self.backend.roles[PARENT.key]["reference"] = "observer"
        for channel in ("build", "api", "ops", "dm", "reference"):
            self.backend.add(channel + "-root", channel=channel)
        self.runtime.sync(PARENT.key)
        self.ledger.mark_read(PARENT.key, [channel + "-root" for channel in ("build", "api", "ops", "dm")])
        self.runtime.notify(PARENT.key, "build", "threads")
        self.runtime.notify(PARENT.key, "api", "off")
        for channel in ("build", "api", "ops", "dm"):
            self.runtime.follow(PARENT.key, channel + "-root")
        self.backend.add("pending-request", channel="dm", root="dm-root")
        self.backend.add("saved-note", channel="build", root="build-root")
        self.runtime.sync(PARENT.key)
        self.ledger.mark_read(PARENT.key, ["saved-note"])
        self.runtime.set_saved(PARENT.key, "saved-note", True)
        self.backend.boundary_calls.clear()

    def restart(self):
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: 200)
        self.runtime = Coordinator(self.ledger, self.backend)

    def follows(self, actor):
        return {(row["channel_id"], row["root_id"]) for row in self.ledger.following(actor)}

    def receipts(self, actor):
        return [tuple(row) for row in self.ledger.db.execute(
            "SELECT * FROM receipts WHERE actor=? ORDER BY arrival", (actor,))]

    def only_build_channel(self):
        for channel in ("api", "ops"):
            self.backend.roles[PARENT.key][channel] = "observer"
        self.runtime.sync(PARENT.key)

    def frozen_failure(self):
        self.only_build_channel()
        with patch.object(self.ledger, "apply_channel_attention", side_effect=RuntimeError("crash before attention commit")):
            with self.assertRaisesRegex(RuntimeError, "crash before attention commit"):
                self.runtime.register(CHILD, "Child", inherit="pending-and-channels")
        plan = self.ledger.onboarding(CHILD.key)
        self.assertEqual(plan["status"], "pending")
        self.assertIn("build", plan["snapshot"]["attention_boundaries"])
        return deepcopy(plan["snapshot"])

    def test_choice_four_installs_only_copied_channel_attention_before_first_ingestion(self):
        parent_receipts = self.receipts(PARENT.key)
        parent_follows = self.follows(PARENT.key)
        parent_stars = set(self.backend.stars[PARENT.key])

        def before_boundary(actor, channel):
            if actor == CHILD.key:
                self.backend.add("before-" + channel, channel=channel, root=channel + "-root")

        def after_boundary(actor, channel):
            if actor == CHILD.key:
                self.backend.add("after-" + channel, channel=channel, root=channel + "-root")

        self.backend.after_join = before_boundary
        self.backend.after_boundary = after_boundary
        applied = []
        apply_feed = self.ledger.apply_feed

        def inspect_before_first_page(actor, page):
            if actor == CHILD.key:
                applied.append({
                    "ready": self.ledger.onboarding_ready(actor),
                    "modes": {c: self.ledger.notification_mode(actor, c)["mode"] for c in ("api", "build", "ops")},
                    "follows": self.follows(actor),
                })
            return apply_feed(actor, page)

        with patch.object(self.ledger, "apply_feed", side_effect=inspect_before_first_page):
            self.runtime.register(CHILD, "Child", inherit="pending-and-channels")
        expected_follows = {("api", "api-root"), ("build", "build-root"), ("ops", "ops-root")}
        self.assertTrue(applied)
        self.assertEqual(applied[0], {"ready": True,
            "modes": {"api": "off", "build": "threads", "ops": "all"}, "follows": expected_follows})
        self.assertEqual(self.follows(CHILD.key), expected_follows)
        plan = self.ledger.onboarding(CHILD.key)
        self.assertEqual(plan["snapshot"]["attention_version"], 1)
        self.assertEqual({c["id"] for c in plan["snapshot"]["channels"]}, {"api", "build", "ops"})
        boundaries = plan["snapshot"]["attention_boundaries"]
        self.assertEqual(set(boundaries), {"api", "build", "ops"})
        for row in self.ledger.following(CHILD.key):
            self.assertEqual(row["after_seq"], boundaries[row["channel_id"]])
        self.assertEqual({p["id"] for p in self.ledger.notification_candidates(CHILD.key)}, {"after-build", "after-ops"})
        unread = {p["id"]: p for p in self.ledger.unread_posts(CHILD.key)}
        self.assertFalse(unread["pending-request"]["notification_eligible"])
        self.assertTrue({"before-build", "before-api", "before-ops"}.issubset(unread))
        self.assertEqual(self.ledger.notification_mode(CHILD.key, "dm")["mode"], "off")
        self.assertEqual(self.backend.stars[CHILD.key], {"saved-note"})
        self.assertEqual(self.receipts(PARENT.key), parent_receipts)
        self.assertEqual(self.follows(PARENT.key), parent_follows)
        self.assertEqual(self.backend.stars[PARENT.key], parent_stars)

    def test_fresh_and_pending_never_copy_follows_or_follow_from_reading_and_starring(self):
        for identity, choice in ((CHILD, "fresh"),
                (NativeIdentity("codex", "33333333-3333-4333-8333-333333333333", PARENT.session_id, True), "pending")):
            with self.subTest(choice=choice):
                self.runtime.register(identity, "Independent child", inherit=choice)
                self.runtime.show(identity.key, "build-root")
                self.runtime.history(identity.key, "dm")
                self.runtime.context(identity.key, "saved-note")
                self.runtime.set_saved(identity.key, "build-root", True)
                if choice == "pending":
                    selected = self.runtime.prepare_read(identity.key, post_id="pending-request")
                    self.runtime.ack_read(identity.key, selected)
                else:
                    self.assertEqual(self.ledger.unread_posts(identity.key), [])
                self.assertEqual(self.follows(identity.key), set())
                self.assertEqual(self.ledger.notification_candidates(identity.key), [])
                self.assertFalse(any(a == identity.key for a, _, _ in self.backend.boundary_calls))
        self.assertEqual(self.backend.mutation_calls, [])
        self.assertEqual(self.follows(PARENT.key), {("api", "api-root"), ("build", "build-root"), ("ops", "ops-root"), ("dm", "dm-root")})

    def test_child_choices_are_independent_and_renaming_a_complete_import_does_not_recopy(self):
        self.runtime.register(CHILD, "Child", inherit="pending-and-channels")
        parent_follows = self.follows(PARENT.key)
        self.runtime.notify(CHILD.key, "build", "off")
        self.runtime.unfollow(CHILD.key, "build-root")
        self.runtime.set_saved(CHILD.key, "saved-note", False)
        self.assertEqual(self.follows(PARENT.key), parent_follows)
        self.assertEqual(self.ledger.notification_mode(PARENT.key, "build")["mode"], "threads")
        self.assertIn("saved-note", self.backend.stars[PARENT.key])
        follows = [tuple(r) for r in self.ledger.db.execute("SELECT * FROM thread_follows WHERE actor=? ORDER BY channel_id,root_id", (CHILD.key,))]
        plan = self.ledger.onboarding(CHILD.key)
        effects = (list(self.backend.boundary_calls), list(self.backend.mutation_calls), list(self.backend.star_calls))
        self.runtime.notify(PARENT.key, "build", "off")
        self.runtime.register(CHILD, "Renamed child")
        self.assertEqual(self.ledger.actor(CHILD.key)["name"], "Renamed child")
        self.assertEqual(self.ledger.notification_mode(CHILD.key, "build")["mode"], "off")
        self.assertEqual([tuple(r) for r in self.ledger.db.execute("SELECT * FROM thread_follows WHERE actor=? ORDER BY channel_id,root_id", (CHILD.key,))], follows)
        self.assertEqual(self.ledger.onboarding(CHILD.key), plan)
        self.assertEqual((self.backend.boundary_calls, self.backend.mutation_calls, self.backend.star_calls), effects)
        self.assertNotIn("saved-note", self.backend.stars[CHILD.key])

    def test_restart_after_frozen_boundary_preserves_fast_reply_and_never_rebases(self):
        self.backend.after_boundary = lambda actor, channel: self.backend.add(
            "fast-reply", channel=channel, root="build-root") if actor == CHILD.key else None
        snapshot = self.frozen_failure()
        original = snapshot["attention_boundaries"]["build"]
        self.assertEqual(self.ledger.cursor(CHILD.key)["cursor"], 0)
        self.assertNotIn("fast-reply", {p["id"] for p in self.ledger.unread_posts(CHILD.key)})
        self.runtime.notify(PARENT.key, "build", "off")
        self.runtime.unfollow(PARENT.key, "build-root")
        self.restart()
        self.backend.add("after-restart", channel="build", root="build-root")
        self.runtime.register(CHILD, "Resumed child")
        self.assertEqual(self.ledger.onboarding(CHILD.key)["snapshot"], snapshot)
        self.assertEqual(self.ledger.notification_mode(CHILD.key, "build")["mode"], "threads")
        self.assertEqual(self.ledger.thread_attention(CHILD.key, "build", "build-root")["after_seq"], original)
        self.assertEqual({p["id"] for p in self.ledger.notification_candidates(CHILD.key)}, {"fast-reply", "after-restart"})
        self.assertEqual([(c, n) for a, c, n in self.backend.boundary_calls if a == CHILD.key], [("build", original)])
        self.assertEqual(len(self.backend.mutation_calls), 1)

    def test_leave_and_rejoin_cannot_reuse_a_frozen_attention_membership(self):
        snapshot = self.frozen_failure()
        del self.backend.roles[CHILD.key]["build"]
        self.runtime.sync(CHILD.key)
        self.backend.roles[CHILD.key]["build"] = "member"
        self.runtime.sync(CHILD.key)
        with self.assertRaisesRegex(LedgerError, "membership changed"):
            self.runtime.register(CHILD, "Resumed child")
        self.assertEqual(self.ledger.onboarding(CHILD.key)["snapshot"], snapshot)
        self.assertEqual(self.ledger.onboarding(CHILD.key)["status"], "pending")
        self.assertEqual(self.follows(CHILD.key), set())
        self.assertEqual(len([c for a, c, _ in self.backend.boundary_calls if a == CHILD.key]), 1)

    def test_leave_rejoin_during_boundary_rpc_cannot_publish_old_membership_cutoff(self):
        self.only_build_channel()
        revisions = []

        def leave_rejoin_before_response(actor, channel):
            if actor != CHILD.key:
                return
            before = self.ledger.notification_mode(actor, channel)["membership_revision"]
            del self.backend.roles[actor][channel]
            self.runtime.sync(actor)
            self.backend.roles[actor][channel] = "member"
            self.runtime.sync(actor)
            revisions.append((before, self.ledger.notification_mode(actor, channel)["membership_revision"]))

        self.backend.after_boundary = leave_rejoin_before_response
        with self.assertRaisesRegex(LedgerError, "membership changed"):
            self.runtime.register(CHILD, "Child", inherit="pending-and-channels")
        self.assertEqual(len(revisions), 1)
        self.assertGreater(revisions[0][1], revisions[0][0])
        plan = self.ledger.onboarding(CHILD.key)
        self.assertEqual(plan["status"], "pending")
        self.assertIn("membership changed", plan["error"])
        self.assertNotIn("build", plan["snapshot"].get("attention_boundaries", {}))
        self.assertNotIn("build", plan["snapshot"].get("attention_membership_revisions", {}))
        self.assertNotIn("attention:build", plan["progress"])
        self.assertEqual(self.follows(CHILD.key), set())
        self.assertEqual(self.ledger.cursor(CHILD.key)["cursor"], 0)

    def test_explicit_child_off_and_unfollow_win_while_the_import_is_pending(self):
        snapshot = self.frozen_failure()
        self.runtime.notify(CHILD.key, "build", "off")
        self.runtime.unfollow(CHILD.key, "build-root")
        own_follow = self.ledger.thread_attention(CHILD.key, "build", "build-root")
        self.backend.add("later-after-child-off", channel="build", root="build-root")
        self.runtime.register(CHILD, "Resumed with own preferences")
        self.assertEqual(self.ledger.onboarding(CHILD.key)["status"], "complete")
        self.assertEqual(self.ledger.onboarding(CHILD.key)["snapshot"], snapshot)
        self.assertEqual(self.ledger.notification_mode(CHILD.key, "build")["mode"], "off")
        self.assertEqual(self.ledger.thread_attention(CHILD.key, "build", "build-root"), own_follow)
        self.assertEqual(self.ledger.notification_candidates(CHILD.key), [])
        self.assertEqual(self.ledger.notification_mode(PARENT.key, "build")["mode"], "threads")
        self.assertIn(("build", "build-root"), self.follows(PARENT.key))

    def test_legacy_frozen_and_completed_plans_keep_their_recorded_mute_only_behavior(self):
        self.only_build_channel()
        self.ledger.enroll(CHILD.key, asdict(CHILD), "Legacy child",
            onboarding=("pending-and-channels", PARENT.key))
        plan = self.ledger.onboarding(CHILD.key)
        snapshot = self.ledger.capture_pending(CHILD.key, plan["plan_id"])
        del snapshot["attention_version"]
        for channel in snapshot["channels"]:
            del channel["notification_mode"]
            del channel["followed_threads"]
        snapshot.update(saved_ids=["saved-note"], saved_started_at=90, saved_finished_at=91)
        self.ledger.db.execute("UPDATE onboarding SET snapshot_json=? WHERE actor=?", (json.dumps(snapshot, sort_keys=True), CHILD.key))
        legacy = deepcopy(snapshot)
        self.runtime.notify(PARENT.key, "build", "off")
        self.backend.after_join = lambda actor, channel: self.backend.add(
            "legacy-join-arrival", channel=channel, root="build-root") if actor == CHILD.key else None
        self.runtime.register(CHILD, "Legacy child")
        self.assertEqual(self.ledger.onboarding(CHILD.key)["snapshot"], legacy)
        self.assertEqual(self.ledger.notification_mode(CHILD.key, "build")["mode"], "all")
        self.assertEqual(self.follows(CHILD.key), set())
        self.assertEqual({p["id"] for p in self.ledger.notification_candidates(CHILD.key)}, {"legacy-join-arrival"})
        self.assertFalse(any(a == CHILD.key for a, _, _ in self.backend.boundary_calls))
        self.runtime.notify(CHILD.key, "build", "threads")
        self.runtime.follow(CHILD.key, "build-root")
        before = deepcopy(self.ledger.onboarding(CHILD.key))
        subscriptions = self.follows(CHILD.key)
        self.runtime.register(CHILD, "Legacy child renamed")
        self.assertEqual(self.ledger.onboarding(CHILD.key), before)
        self.assertEqual(self.ledger.notification_mode(CHILD.key, "build")["mode"], "threads")
        self.assertEqual(self.follows(CHILD.key), subscriptions)

    def test_inspecting_an_unenrolled_identity_never_provisions_it_or_inherits_attention(self):
        unknown = "codex:44444444-4444-4444-8444-444444444444"
        actors, provisions = self.ledger.actors(), list(self.backend.register_calls)
        for action in (lambda: self.runtime.show(unknown, "saved-note"),
                lambda: self.runtime.history(unknown, "build"), lambda: self.runtime.follow(unknown, "build-root"),
                lambda: self.runtime.inbox(unknown)):
            with self.assertRaises(LedgerError):
                action()
        self.assertEqual(self.ledger.actors(), actors)
        self.assertEqual(self.backend.register_calls, provisions)


if __name__ == "__main__":
    unittest.main()
