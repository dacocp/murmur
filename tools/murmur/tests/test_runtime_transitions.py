"""Persistence and interleavings at the runtime's external-effect boundaries."""

from dataclasses import asdict
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from murmur import hooks
from murmur.backend import BackendError
from murmur.coordinator import Coordinator, routing_binding
from murmur.ledger import Ledger, LedgerError
from murmur.native import HookEvent, NativeIdentity
from murmur.onboarding import Onboarding
from test_open_channels import OpenBackend


PARENT = NativeIdentity("codex", "11111111-1111-4111-8111-111111111111", parent_known=True)
CHILD = NativeIdentity("codex", "22222222-2222-4222-8222-222222222222", PARENT.session_id, True)


class RuntimeTransitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "operations.sqlite3"
        self.ledger = Ledger(self.path, clock=lambda: 100)
        self.addCleanup(lambda: self.ledger.close())
        self.backend = OpenBackend()
        self.coordinator = Coordinator(self.ledger, self.backend)
        self.coordinator.register(PARENT, "Parent")

    def restart(self):
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: 200)
        self.coordinator = Coordinator(self.ledger, self.backend)

    def pending(self, *, saved=False):
        self.backend.add("request", channel="dm")
        self.coordinator.sync(PARENT.key)
        if saved:
            self.backend.set_saved(PARENT.key, "request", True)

    def submit(self):
        self.pending()
        epoch = routing_binding(PARENT)
        self.ledger.create_attempt(PARENT.key, epoch, "wake")
        prompt = "You have new messages. [wake:wake]"
        event = HookEvent(PARENT, "submit", "UserPromptSubmit", "codex:turn", prompt=prompt, wake_token="wake")
        return event, prompt

    def test_admission_has_no_fallible_identity_write_after_commit(self):
        event, prompt = self.submit()
        blocker = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(blocker.close)
        original = self.ledger.admit

        def after_commit(*args, **kwargs):
            accepted = original(*args, **kwargs)
            blocker.execute("BEGIN IMMEDIATE")
            return accepted

        with patch("murmur.native.parse_hook", return_value=event), \
                patch.object(self.ledger, "admit", side_effect=after_commit):
            result = hooks.handle(self.ledger, "codex", "UserPromptSubmit", {"prompt": prompt})
        blocker.rollback()
        self.assertEqual(result, {})
        self.assertEqual(self.ledger.attempt("wake")["status"], "admitted")
        self.assertIsNotNone(self.ledger.unread_posts(PARENT.key)[0]["announced_at"])
        self.assertEqual(self.ledger.actor(PARENT.key)["identity"], asdict(PARENT))

    def test_identity_failure_rolls_back_admission_and_announcement(self):
        event, prompt = self.submit()
        self.ledger.db.execute("""CREATE TRIGGER reject_identity BEFORE UPDATE OF identity_json ON actors
            BEGIN SELECT RAISE(ABORT,'fixture identity write failed'); END""")
        with patch("murmur.native.parse_hook", return_value=event):
            result = hooks.handle(self.ledger, "codex", "UserPromptSubmit", {"prompt": prompt})
        self.assertEqual(result["decision"], "block")
        self.assertEqual(self.ledger.attempt("wake")["status"], "pending")
        self.assertIsNone(self.ledger.unread_posts(PARENT.key)[0]["announced_at"])
        self.assertIsNone(self.ledger.active_turn(PARENT.key))

    def test_later_explicit_child_mute_survives_lost_join_and_restart(self):
        self.backend.lost_join = True
        with self.assertRaises(BackendError):
            self.coordinator.register(CHILD, "Child", inherit="pending-and-channels")
        self.ledger.set_mute(CHILD.key, "build", True)
        self.restart()
        self.coordinator.register(CHILD, "Child")
        channel = next(c for c in self.ledger.conversations(CHILD.key) if c["id"] == "build")
        self.assertEqual((channel["muted"], channel["mute_explicit"]), (1, 1))
        self.assertTrue(self.ledger.onboarding_ready(CHILD.key))
        self.assertEqual(len(self.backend.mutation_calls), 1)

    def test_replaced_plan_fences_stale_import_and_error_completion(self):
        self.pending()
        other_ledger = Ledger(self.path, clock=lambda: 101)
        self.addCleanup(other_ledger.close)
        other = Coordinator(other_ledger, self.backend)
        original = Onboarding.post
        switched = []

        def replace_before_import(runner, post_id, key):
            if not switched:
                other.register(CHILD, "Fresh child", inherit="fresh")
                switched.append(True)
            return original(runner, post_id, key)

        with patch.object(Onboarding, "post", new=replace_before_import), \
                self.assertRaisesRegex(LedgerError, "plan changed"):
            self.coordinator.register(CHILD, "Child", inherit="pending")
        plan = self.ledger.onboarding(CHILD.key)
        self.assertEqual((plan["choice"], plan["status"], plan["progress"], plan["error"]),
                         ("fresh", "complete", {}, None))
        self.assertEqual(self.ledger.unread_posts(CHILD.key), [])

    def test_definite_saved_denial_is_unavailable_not_unknown(self):
        self.pending(saved=True)
        with patch.object(self.backend, "set_saved", side_effect=BackendError("Access revoked", status=403)):
            self.coordinator.register(CHILD, "Child", inherit="pending")
        self.restart()
        self.coordinator.register(CHILD, "Child")
        plan = self.ledger.onboarding(CHILD.key)
        self.assertEqual(plan["progress"]["star:request"], "unavailable")
        self.assertEqual(plan["status"], "complete")
        self.assertEqual(self.backend.stars.get(CHILD.key, set()), set())

    def test_definite_retryable_saved_failure_retries_only_on_registration(self):
        self.pending(saved=True)
        with patch.object(self.backend, "set_saved", side_effect=BackendError("Invalid policy", status=400)):
            with self.assertRaises(BackendError):
                self.coordinator.register(CHILD, "Child", inherit="pending")
        self.assertEqual(self.ledger.onboarding(CHILD.key)["progress"]["star:request"], "failed")
        self.restart()
        self.coordinator.register(CHILD, "Child")
        self.assertEqual(self.backend.stars[CHILD.key], {"request"})
        self.assertTrue(self.ledger.onboarding_ready(CHILD.key))

    def test_unknown_absent_save_requires_explicit_plan_bound_skip(self):
        self.pending(saved=True)
        with patch.object(self.backend, "set_saved", side_effect=BackendError("Lost response", uncertain=True)):
            with self.assertRaises(BackendError):
                self.coordinator.register(CHILD, "Child", inherit="pending")
        self.restart()
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            self.coordinator.register(CHILD, "Child")
        plan = self.ledger.onboarding(CHILD.key)
        with self.assertRaisesRegex(LedgerError, "plan changed"):
            self.coordinator.recover_onboarding(CHILD.key, "wrong", "star:request", "Keep the child's absent star")
        with self.assertRaisesRegex(LedgerError, "held saved-copy"):
            self.coordinator.recover_onboarding(CHILD.key, plan["plan_id"], "post:request", "No")
        self.coordinator.recover_onboarding(CHILD.key, plan["plan_id"], "star:request", "Keep the child's absent star")
        self.coordinator.register(CHILD, "Child")
        self.assertTrue(self.ledger.onboarding_ready(CHILD.key))
        self.assertEqual(self.ledger.onboarding(CHILD.key)["progress"]["star:request"], "skipped")
        self.assertEqual(self.backend.stars.get(CHILD.key, set()), set())

    def test_fresh_enrollment_uses_server_first_observation_not_host_time(self):
        self.ledger.db.execute("UPDATE actors SET enrollment_seq=1 WHERE actor=?", (PARENT.key,))
        old = self.backend.add("baseline", when=9999)
        self.backend.add("fresh", when=1)
        self.coordinator.sync(PARENT.key)
        self.backend.events.append(dict(old, update_at=10000))
        self.coordinator.sync(PARENT.key)
        eligible = self.ledger._unread(PARENT.key, eligible=True)
        self.assertEqual([p["id"] for p in eligible], ["fresh"])
        self.restart()
        self.assertEqual([p["id"] for p in self.ledger._unread(PARENT.key, eligible=True)], ["fresh"])

    def test_old_local_mapping_does_not_rebase_unfetched_mail(self):
        self.pending()
        # Represent pre-upgrade mapped actor columns exactly: no original
        # server cutoff exists and no new enrollment intent may be inferred.
        self.ledger.db.execute("UPDATE actors SET enrollment_id=NULL,enrollment_seq=NULL WHERE actor=?", (PARENT.key,))
        self.backend.add("missed", when=1)
        self.restart()
        record = self.ledger.actor(PARENT.key)
        self.assertEqual(record["enrollment_seq"], 0)
        self.assertIsNone(record["enrollment_id"])
        with patch.object(self.backend, "register", wraps=self.backend.register) as register:
            self.coordinator.register(PARENT, "Renamed")
        self.assertNotIn("enrollment_id", register.call_args.kwargs)
        self.assertEqual({p["id"] for p in self.ledger._unread(PARENT.key, eligible=True)}, {"request", "missed"})

    def test_exact_selection_consumes_only_selected_post_after_ack(self):
        self.pending()
        self.backend.add("fresh", channel="dm", root="request", when=102)
        selection = self.coordinator.prepare_read(PARENT.key, post_id="fresh")
        self.assertEqual(selection["ack_ids"], ("fresh",))
        with self.assertRaises(TypeError):
            selection["actor"] = CHILD.key
        self.assertEqual({p["id"] for p in self.ledger.unread_posts(PARENT.key)}, {"request", "fresh"})
        with self.assertRaisesRegex(LedgerError, "another actor"):
            self.coordinator.ack_read(CHILD.key, selection)
        self.coordinator.ack_read(PARENT.key, selection)
        self.assertEqual([p["id"] for p in self.ledger.unread_posts(PARENT.key)], ["request"])

    def test_lost_registration_response_reuses_boundary_after_restart(self):
        class BoundaryBackend(OpenBackend):
            def __init__(inner):
                super().__init__()
                inner.roles[CHILD.key] = {"build": "member"}  # Existing server actor, fresh host ledger.
                inner.boundaries, inner.ids, inner.lose = {}, [], True

            def register(inner, actor, name, parent, **options):
                agent = super().register(actor, name, parent, **options)
                enrollment_id = options["enrollment_id"]
                inner.ids.append(enrollment_id)
                boundary = inner.boundaries.setdefault(enrollment_id, len(inner.events))
                if inner.lose:
                    inner.lose = False
                    raise BackendError("Lost committed registration response", uncertain=True)
                return {"agent": agent, "enrollment_seq": boundary}

        backend = BoundaryBackend()
        backend.add("before", when=9999)
        coordinator = Coordinator(self.ledger, backend)
        with self.assertRaises(BackendError):
            coordinator.register(CHILD, "Child")
        original_id = self.ledger.actor(CHILD.key)["enrollment_id"]
        self.assertIsNone(self.ledger.actor(CHILD.key)["user_id"])
        self.assertIsNone(self.ledger.actor(CHILD.key)["enrollment_seq"])
        backend.add("after", when=1)
        self.restart()
        Coordinator(self.ledger, backend).register(CHILD, "Child")
        self.assertEqual(backend.ids, [original_id, original_id])
        self.assertEqual(self.ledger.actor(CHILD.key)["enrollment_seq"], 1)
        self.assertEqual([p["id"] for p in self.ledger._unread(CHILD.key, eligible=True)], ["after"])

    def test_exact_404_has_no_body_ack_but_emitted_snapshot_survives_deletion(self):
        self.pending()
        selection = self.coordinator.prepare_read(PARENT.key, post_id="request")
        self.ledger.mark_unavailable(PARENT.key, "request")
        self.coordinator.ack_read(PARENT.key, selection)
        receipt = self.ledger.db.execute("SELECT read_at,unavailable_at FROM receipts WHERE actor=? AND post_id='request'",
                                         (PARENT.key,)).fetchone()
        self.assertIsNotNone(receipt["read_at"])  # The returned snapshot really was read.
        self.assertIsNotNone(receipt["unavailable_at"])
        self.backend.add("gone", channel="dm")
        self.coordinator.sync(PARENT.key)
        del self.backend.canonical["gone"]
        with self.assertRaises(BackendError):
            self.coordinator.prepare_read(PARENT.key, post_id="gone")
        gone = self.ledger.db.execute("SELECT read_at,unavailable_at FROM receipts WHERE actor=? AND post_id='gone'",
                                      (PARENT.key,)).fetchone()
        self.assertIsNone(gone["read_at"])
        self.assertIsNotNone(gone["unavailable_at"])

    def test_registration_publishes_mode_and_only_verified_native_edges(self):
        with patch.object(self.backend, "register", wraps=self.backend.register) as register:
            self.coordinator.register(CHILD, "Child")
        self.assertEqual(register.call_args.kwargs["mode"], "native")
        self.assertEqual(register.call_args.kwargs["lineage"], [{"actor": CHILD.key, "parent": PARENT.key}])
        root = NativeIdentity("codex", "44444444-4444-4444-8444-444444444444", parent_known=True)
        with patch.object(self.backend, "register", wraps=self.backend.register) as register:
            self.coordinator.register(root, "Root")
        self.assertEqual(register.call_args.kwargs["lineage"], [{"actor": root.key, "parent": None}])
        manual = NativeIdentity("codex", "55555555-5555-4555-8555-555555555555", parent_known=True,
                                evidence={"lineage_edges": [{"session_id": "ignored", "parent_session_id": None}]})
        with patch.object(self.backend, "register", wraps=self.backend.register) as register:
            self.coordinator.register(manual, "Sidechat", mode="manual")
        self.assertEqual(register.call_args.kwargs["mode"], "manual")
        self.assertIsNone(register.call_args.args[2])
        self.assertNotIn("lineage", register.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
