"""Public previews and one-time fork handoffs with real SQLite, no native/server calls."""

from dataclasses import replace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from murmur.backend import BackendError
from murmur import __main__ as cli
from murmur.coordinator import Coordinator, ConversationChanged
from murmur.ledger import Ledger, LedgerError
from murmur.native import NativeIdentity
from murmur.onboarding import Onboarding
from test_coordinator import FakeBackend


class OpenBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.chats.append(dict(id="dm", name="Parent DM", kind="dm", role="member"))
        self.roles = {}
        self.starts = {}
        self.stars = {}
        self.star_calls = []
        self.change_calls = []
        self.fail_star = None
        self.before_star = None
        self.lost_join = False
        self.bad_descriptor = False

    def register(self, actor, name, parent, **options):
        result = super().register(actor, name, parent, **options)
        if actor not in self.roles:
            self.roles[actor] = ({c: "observer" for c in self.roles.get(parent, {})} if parent else
                                 {c["id"]: "member" for c in self.chats})
        return dict(result, user_id="user-" + actor)

    def descriptor(self, actor, channel):
        chat = next(c for c in self.chats if c["id"] == channel)
        return dict(chat, role=self.roles.get(actor, {}).get(channel), open=chat["kind"] == "channel",
                    visibility="public" if chat["kind"] == "channel" else "private", purpose="API work")

    def conversations(self, actor):
        return [self.descriptor(actor, c) for c in self.roles[actor]]

    def changes(self, actor, cursor, generation):
        self.change_calls.append(actor)
        items = [{"seq": i + 1, "first_seen_seq": next(j + 1 for j, old in enumerate(self.events) if old["id"] == post["id"]), "post": dict(post), "notification_eligible": True}
                 for i, post in enumerate(self.events) if i + 1 > cursor
                 and self.roles[actor].get(post["channel_id"]) == "member"
                 and i + 1 > self.starts.get((actor, post["channel_id"]), 0)]
        return dict(attention_version=3, conversations=self.conversations(actor), changes=items,
                    cursor=len(self.events), generation=1, more=False, reset=generation != 1)

    def post(self, actor, post_id):
        post = super().post(actor, post_id)
        descriptor = self.descriptor(actor, post["channel_id"])
        if not descriptor["open"] and descriptor["role"] is None:
            raise BackendError("Access denied", status=403)
        return post

    def saved(self, actor):
        return [{"post_id": p, "post": self.post(actor, p),
                 "conversation": self.descriptor(actor, self.canonical[p]["channel_id"])}
                for p in sorted(self.stars.get(actor, set()))]

    def set_saved(self, actor, post_id, starred):
        if starred:
            self.post(actor, post_id)
        self.star_calls.append((actor, post_id, starred))
        if self.before_star:
            self.before_star()
        stars = self.stars.setdefault(actor, set())
        stars.add(post_id) if starred else stars.discard(post_id)
        if self.fail_star:
            error, self.fail_star = self.fail_star, None
            raise error
        return dict(post_id=post_id, starred=starred)

    def threads(self, actor, channel, limit, before):
        descriptor = self.descriptor(actor, channel)
        if self.bad_descriptor:
            descriptor["id"] = "wrong"
        return dict(conversation=descriptor, threads=[dict(id=p["id"], channel_id=channel)
                    for p in self.canonical.values() if p["channel_id"] == channel and not p["root_id"]], next_before=None)

    def channels(self, actor, query, limit, after):
        channels = [self.descriptor(actor, c["id"]) for c in self.chats if c["kind"] == "channel"]
        return dict(channels=channels[:limit], next_after=None)

    def mutate(self, action, actor, op_id, **payload):
        if action != "join":
            return super().mutate(action, actor, op_id, **payload)
        self.mutation_calls.append((action, actor, op_id, payload))
        if op_id not in self.operations:
            channel = payload["channel_id"]
            self.roles[actor][channel] = "member"
            self.starts[(actor, channel)] = len(self.events)
            self.operations[op_id] = dict(conversation=self.descriptor(actor, channel))
        if self.lost_join:
            self.lost_join = False
            raise BackendError("Lost join response", uncertain=True)
        return self.operations[op_id]

    def operation(self, actor, op_id):
        result = super().operation(actor, op_id)
        return dict(status="complete", result={"channel_id": result["conversation"]["id"]})


class OpenChannelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "operations.sqlite3"
        self.ledger = Ledger(self.path, clock=lambda: 100)
        self.addCleanup(lambda: self.ledger.close())
        self.backend = OpenBackend()
        self.runtime = Coordinator(self.ledger, self.backend)
        self.parent = NativeIdentity("codex", "11111111-1111-4111-8111-111111111111", parent_known=True)
        self.child = NativeIdentity("codex", "22222222-2222-4222-8222-222222222222", self.parent.session_id, True)
        self.runtime.register(self.parent, "Parent")

    def source_pending(self):
        self.backend.add("request", channel="dm")
        self.backend.add("saved", when=102)
        self.runtime.sync(self.parent.key)
        self.ledger.mark_read(self.parent.key, ["saved"])
        self.backend.set_saved(self.parent.key, "saved", True)

    def restart(self):
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: 200)
        self.runtime = Coordinator(self.ledger, self.backend)

    def run_cli(self, *args):
        output = io.StringIO()
        with patch.object(cli.config, "ledger_path", return_value=self.path), \
                patch.object(cli.config, "load", return_value={"url": "http://fixture.invalid", "token": "fixture"}), \
                patch.object(cli, "Backend", return_value=self.backend), \
                patch("murmur.native.current_identity", return_value=self.parent), redirect_stdout(output):
            cli.main(list(args))
        return json.loads(output.getvalue())

    def test_cli_creator_only_channel_purpose_directory_and_handoff_argument(self):
        calls = []

        def create(action, actor, op_id, **payload):
            calls.append((action, actor, payload))
            return {"conversation": self.backend.descriptor(actor, "build")}

        with patch.object(self.backend, "mutate", side_effect=create):
            self.run_cli("channel", "API review", "--purpose", "Coordinate schema changes")
        self.assertEqual(calls, [("conversation", self.parent.key, dict(kind="channel", name="API review",
            members=[], purpose="Coordinate schema changes"))])
        with patch.object(self.backend, "channels", wraps=self.backend.channels) as directory:
            self.run_cli("channels", "--query", "API", "--limit", "1", "--after", "cursor")
            directory.assert_called_once_with(self.parent.key, "API", 1, "cursor")
        self.assertEqual(cli.parser().parse_args(["register", "--name", "Fork", "--inherit", "pending"])
                         .inherit, "pending")
        count = len(self.ledger.outbounds(self.parent.key))
        with self.assertRaisesRegex(ValueError, "250"):
            self.run_cli("channel", "Too long", "--purpose", "x" * 251)
        self.assertEqual(len(self.ledger.outbounds(self.parent.key)), count)

    def test_fresh_references_no_receipts_and_unmuting_observer_cannot_wake(self):
        self.source_pending()
        self.runtime.register(self.child, "Child")
        self.ledger.set_mute(self.child.key, "dm", False)
        self.runtime.sync(self.child.key)
        self.assertEqual(self.runtime.inbox(self.child.key), [])
        self.assertIsNone(self.ledger.create_attempt(self.child.key, "epoch"))
        self.assertEqual(self.backend.post(self.child.key, "request")["id"], "request")
        self.assertEqual(self.backend.stars.get(self.child.key, set()), set())

    def test_pending_copy_is_quiet_independent_and_rename_does_not_reimport(self):
        self.source_pending()
        before_parent = [tuple(r) for r in self.ledger.db.execute("SELECT * FROM receipts WHERE actor=?", (self.parent.key,))]
        before_calls = self.backend.change_calls.count(self.parent.key)
        result = self.runtime.register(self.child, "Child", inherit="pending")
        self.assertEqual(result["onboarding"]["choice"], "pending")
        self.assertEqual(self.backend.change_calls.count(self.parent.key), before_calls)
        unread = self.ledger.unread_posts(self.child.key)
        self.assertEqual([p["id"] for p in unread], ["request"])
        self.assertFalse(unread[0]["notification_eligible"])
        self.assertIsNone(unread[0]["announced_at"])
        self.assertEqual(self.backend.stars[self.child.key], {"saved"})
        self.ledger.mark_read(self.child.key, ["request"])
        self.backend.set_saved(self.child.key, "saved", False)
        self.restart()
        self.runtime.register(self.child, "Renamed")
        self.assertEqual(self.ledger.unread_posts(self.child.key), [])
        self.assertEqual(self.backend.stars[self.child.key], set())
        self.assertEqual(self.backend.stars[self.parent.key], {"saved"})
        self.assertEqual([tuple(r) for r in self.ledger.db.execute("SELECT * FROM receipts WHERE actor=?", (self.parent.key,))], before_parent)
        self.assertEqual(self.ledger.actor(self.child.key)["causal"], {"chain_ids": []})
        self.assertEqual(self.ledger.attempts(self.child.key), [])
        self.assertEqual(self.ledger.outbounds(self.child.key), [])
        with self.assertRaisesRegex(LedgerError, "choice"):
            self.runtime.register(self.child, "Renamed", inherit="pending-and-channels")

    def test_import_quiets_existing_receipt_without_resetting_read_or_admission(self):
        self.source_pending()
        original = Onboarding.post
        inserted = []

        def overlapping_receipt(runner, post_id, key):
            actor = runner.actor
            post = original(runner, post_id, key)
            if key == "post:request":
                account = self.ledger.actor(actor)
                self.ledger.ingest(actor, [post], account["cursor"], account["generation"])
                self.ledger.db.execute("""UPDATE receipts SET notification_eligible=1,read_at=777,
                    announced_at=888 WHERE actor=? AND post_id=?""", (actor, post_id))
                inserted.append(post_id)
            return post

        with patch.object(Onboarding, "post", new=overlapping_receipt):
            self.runtime.register(self.child, "Child", inherit="pending")
        receipt = self.ledger.db.execute("""SELECT notification_eligible,read_at,announced_at FROM receipts
            WHERE actor=? AND post_id='request'""", (self.child.key,)).fetchone()
        self.assertEqual(inserted, ["request"])
        self.assertEqual(tuple(receipt), (0, 777, 888))

    def test_channel_handoff_copies_only_full_channels_and_explicit_mute_after_lost_join(self):
        self.source_pending()
        self.ledger.set_mute(self.parent.key, "build", True)
        self.backend.lost_join = True
        with self.assertRaisesRegex(BackendError, "Lost join"):
            self.runtime.register(self.child, "Child", inherit="pending-and-channels")
        self.assertFalse(self.ledger.onboarding_ready(self.child.key))
        self.assertIsNone(self.ledger.create_attempt(self.child.key, "epoch"))
        self.backend.add("between", when=104)
        self.restart()
        self.runtime.register(self.child, "Child")
        self.assertEqual(len(self.backend.mutation_calls), 1)
        chats = {c["id"]: c for c in self.ledger.conversations(self.child.key)}
        self.assertEqual(chats["dm"]["role"], "observer")
        self.assertEqual((chats["build"]["role"], chats["build"]["muted"], chats["build"]["mute_explicit"]), ("member", 1, 1))
        self.assertEqual(self.backend.operation_calls, [self.ledger.outbounds(self.child.key)[0]["op_id"]])
        self.backend.add("new-dm", channel="dm", when=105)
        self.runtime.sync(self.child.key)
        self.assertNotIn("new-dm", {p["id"] for p in self.ledger.unread_posts(self.child.key)})
        self.assertIsNone(self.ledger.create_attempt(self.child.key, "epoch"))

    def test_uncertain_star_does_not_resurrect_child_unstar_or_recapture_parent(self):
        self.source_pending()
        self.backend.fail_star = BackendError("Lost star response", uncertain=True)
        with self.assertRaisesRegex(BackendError, "Lost star"):
            self.runtime.register(self.child, "Child", inherit="pending")
        self.backend.set_saved(self.child.key, "saved", False)
        self.backend.add("later-parent", channel="dm", when=106)
        self.runtime.sync(self.parent.key)
        self.restart()
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            self.runtime.register(self.child, "Child")
        self.assertEqual([c for c in self.backend.star_calls if c == (self.child.key, "saved", True)], [(self.child.key, "saved", True)])
        self.assertNotIn("later-parent", {p["id"] for p in self.ledger.unread_posts(self.child.key)})
        self.assertFalse(self.ledger.onboarding_ready(self.child.key))

    def test_uncertain_star_present_reconciles_without_repeat_write(self):
        self.source_pending()
        self.backend.fail_star = BackendError("Lost star response", uncertain=True)
        with self.assertRaises(BackendError):
            self.runtime.register(self.child, "Child", inherit="pending")
        self.restart()
        self.runtime.register(self.child, "Child")
        self.assertTrue(self.ledger.onboarding_ready(self.child.key))
        self.assertEqual(self.backend.star_calls.count((self.child.key, "saved", True)), 1)

    def test_overlapping_registration_cannot_restar_or_regress_completed_handoff(self):
        self.source_pending()
        other_ledger = Ledger(self.path, clock=lambda: 200)
        self.addCleanup(other_ledger.close)
        other = Coordinator(other_ledger, self.backend)
        original = Onboarding.post
        interleaved = []

        def before_claim(runner, post_id, key):
            actor = runner.actor
            post = original(runner, post_id, key)
            if key == "star:saved" and not interleaved:
                interleaved.append(True)
                # The second connection completes after the first has read
                # empty progress, but before its atomic star-start claim.
                other.register(self.child, "Other registration")
                self.backend.set_saved(self.child.key, "saved", False)
            return post

        with patch.object(Onboarding, "post", new=before_claim):
            with self.assertRaisesRegex(LedgerError, "Another registration"):
                self.runtime.register(self.child, "Child", inherit="pending")
        self.assertEqual(interleaved, [True])
        self.assertEqual(self.backend.star_calls.count((self.child.key, "saved", True)), 1)
        self.assertEqual(self.backend.stars[self.child.key], set())
        self.assertEqual(self.ledger.onboarding(self.child.key)["status"], "complete")
        self.assertIsNone(self.ledger.onboarding(self.child.key)["error"])
        self.runtime.register(self.child, "Child")
        self.assertEqual(self.backend.stars[self.child.key], set())

    def test_join_recovery_does_not_treat_pending_or_failed_as_complete(self):
        self.source_pending()
        self.backend.lost_join = True
        with self.assertRaises(BackendError):
            self.runtime.register(self.child, "Child", inherit="pending-and-channels")
        op_id = self.ledger.outbounds(self.child.key)[0]["op_id"]
        with patch.object(self.backend, "operation", return_value={"status": "failed", "error": "membership rejected"}):
            with self.assertRaisesRegex(RuntimeError, "membership rejected"):
                self.runtime.register(self.child, "Child")
        self.assertEqual(self.ledger.outbound(op_id)["state"], "failed")
        self.assertEqual(self.ledger.onboarding(self.child.key)["progress"]["channel:build"], "started")
        with patch.object(self.backend, "operation", return_value={"status": "pending"}):
            self.runtime.register(self.child, "Child")
        self.assertEqual([call[2] for call in self.backend.mutation_calls], [op_id, op_id])
        self.assertTrue(self.ledger.onboarding_ready(self.child.key))

    def test_missing_local_parent_and_manual_import_fail_before_provisioning(self):
        missing = replace(self.child, parent_session_id="33333333-3333-4333-8333-333333333333")
        before = list(self.backend.register_calls)
        with self.assertRaisesRegex(ValueError, "parent enrolled"):
            self.runtime.register(missing, "Child", inherit="pending")
        self.assertEqual(self.backend.register_calls, before)
        with self.assertRaisesRegex(ValueError, "manual sidechats"):
            self.runtime.register(self.child, "Child", mode="manual", inherit="pending")
        self.runtime.register(missing, "Child", inherit="fresh")

    def test_inaccessible_pending_anchor_is_reported_without_importing_metadata(self):
        self.source_pending()
        del self.backend.canonical["request"]
        result = self.runtime.register(self.child, "Child", inherit="pending")
        self.assertEqual(result["onboarding"]["unavailable"], ["post:request"])
        self.assertEqual(self.ledger.unread_posts(self.child.key), [])

    def test_public_visitor_preview_and_saved_only_inbox_do_not_join_or_track(self):
        self.source_pending()
        self.runtime.register(self.child, "Child")
        del self.backend.roles[self.child.key]["build"]
        self.runtime.sync(self.child.key)
        before = tuple(self.ledger.db.iterdump())
        calls = list(self.backend.change_calls)
        self.runtime.channels(self.child.key)
        self.runtime.history(self.child.key, "build")
        page = self.runtime.threads(self.child.key, "build")
        self.assertIsNone(page["threads"][0]["unread_count"])
        self.assertEqual(tuple(self.ledger.db.iterdump()), before)
        self.assertEqual(self.backend.change_calls, calls)
        self.runtime.set_saved(self.child.key, "saved", True)
        inbox = self.runtime.inbox(self.child.key)
        self.assertEqual((inbox[0]["role"], inbox[0]["muted"], inbox[0]["unread_count"]), (None, True, 0))
        self.assertEqual(inbox[0]["starred"][0]["post_id"], "saved")
        self.assertNotIn("build", {c["id"] for c in self.ledger.conversations(self.child.key)})
        self.assertEqual(self.ledger.unread_posts(self.child.key), [])
        self.assertEqual(self.backend.mutation_calls, [])
        self.runtime.set_saved(self.child.key, "saved", False)
        self.assertEqual(self.runtime.inbox(self.child.key), [])

    def test_wrong_preview_descriptor_and_history_channel_fail(self):
        self.source_pending()
        self.runtime.register(self.child, "Child")
        self.backend.bad_descriptor = True
        with self.assertRaisesRegex(RuntimeError, "descriptor"):
            self.runtime.threads(self.child.key, "build")
        self.backend.posts = lambda *_args: {"posts": [self.backend.canonical["request"]]}
        with self.assertRaisesRegex(RuntimeError, "another conversation"):
            self.runtime.history(self.child.key, "build")

    def test_saved_metadata_does_not_mask_tracked_membership_races(self):
        self.source_pending()
        self.runtime.register(self.child, "Child")
        self.runtime.set_saved(self.child.key, "saved", True)
        saved = self.backend.saved(self.child.key)
        saved[0]["conversation"]["role"] = None
        with patch.object(self.backend, "saved", return_value=saved):
            with self.assertRaisesRegex(ConversationChanged, "retry inbox"):
                self.runtime.inbox(self.child.key)
        saved[0]["conversation"]["id"] = "dm"
        with patch.object(self.backend, "saved", return_value=saved):
            with self.assertRaisesRegex(RuntimeError, "descriptor"):
                self.runtime.inbox(self.child.key)

    def test_old_unmuted_observer_eligibility_cannot_admit_a_wake(self):
        self.source_pending()
        self.runtime.register(self.child, "Child", inherit="pending")
        self.ledger.set_mute(self.child.key, "dm", False)
        self.ledger.db.execute("UPDATE receipts SET notification_eligible=1 WHERE actor=?", (self.child.key,))
        self.assertIsNone(self.ledger.create_attempt(self.child.key, "epoch"))
        self.runtime.sync(self.child.key)
        receipt = self.ledger.unread_posts(self.child.key)[0]
        self.assertIsNone(receipt["read_at"])
        self.assertIsNone(receipt["announced_at"])
        self.assertFalse(receipt["notification_eligible"])


if __name__ == "__main__":
    unittest.main()
