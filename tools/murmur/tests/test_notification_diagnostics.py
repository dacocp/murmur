"""Offline notification inspection preserves admission, awareness, mail and recovery."""

from contextlib import closing, redirect_stdout
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from murmur import __main__ as cli, diagnostics, hooks, native
from murmur.ledger import Ledger, LedgerError, LEDGER_SCHEMA_VERSION
import test_coordinator as fixtures


class NotificationDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        fixtures.CoordinatorTests.setUp(self)
        hooks.initialize(self.ledger.db)

    tearDown = fixtures.CoordinatorTests.tearDown

    def pending(self, token="notice", *, when=100):
        self.ledger.clock = lambda: when
        self.backend.add("post-" + token, when=when)
        self.coordinator.sync(self.actor)
        self.ledger.create_attempt(self.actor, "recorded-owner", token)
        self.ledger.set_transport(token, "uncertain", {"detail": "Queue result was not observed"})
        return self.ledger.attempt(token)

    def inspect(self, *, identity=None, token=None, limit=20, now=1000):
        with closing(Ledger.open_readonly(self.path, clock=lambda: now)) as ledger:
            return diagnostics.notification_report(ledger, identity or self.identity, token=token, limit=limit)

    def invoke(self, *options, identity=None, path=None):
        output = io.StringIO()
        with patch.object(cli.config, "ledger_path", return_value=path or self.path), \
                patch.object(cli.config, "load", side_effect=AssertionError("Inspection needs no credentials")), \
                patch.object(cli, "Backend", side_effect=AssertionError("Inspection needs no server")), \
                patch.object(cli, "initialize", side_effect=AssertionError("Do not initialize operational state")), \
                patch.object(Ledger, "__init__", side_effect=AssertionError("Do not migrate or create state")), \
                patch.object(native, "current_identity", return_value=identity or self.identity), \
                patch.object(native, "resolve_identity", side_effect=AssertionError("Do not resolve ancestry")), \
                patch.object(native, "verify_target", side_effect=AssertionError("Do not inspect native targets")), \
                patch.object(native, "verify_context_receipt", side_effect=AssertionError("Do not inspect native history")), \
                patch.object(native, "verify_peer_receipt", side_effect=AssertionError("Do not inspect native history")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("Do not use the network")), redirect_stdout(output):
            cli.main(["doctor", "--notifications", *options])
        return json.loads(output.getvalue())

    def test_four_old_uncertain_batches_remain_visible_and_do_not_block_new_arrivals(self):
        for number in range(4):
            self.pending("legacy-" + str(number), when=100 + number)
        self.backend.add("new-arrival", when=200)
        self.coordinator.sync(self.actor)
        before = tuple(self.ledger.db.iterdump())
        result = self.inspect(limit=2)
        self.assertEqual((result["total"], result["unconfirmed_native_count"], result["truncated"]), (4, 4, True))
        self.assertEqual([a["token"] for a in result["attempts"]], ["legacy-0", "legacy-1"])
        attempt = result["attempts"][0]
        self.assertEqual(attempt["age_ms"], 900)
        self.assertEqual(attempt["native_admission"]["state"], "unconfirmed")
        self.assertEqual(attempt["reservation"], {"scope": "frozen_batch_only", "post_count": 1})
        self.assertFalse(attempt["receipt_blocks_new_notifications"])
        self.assertFalse(attempt["restart_qualified"])
        self.assertEqual(attempt["restart_qualification_scope"], "recorded_handoff")
        self.assertEqual(attempt["current_recovery_verification"], "not_checked")
        self.assertIn("cancel already queued/emitted native input", attempt["operator_retirement_effect"])
        self.assertIn("Later verified context can still settle", attempt["operator_retirement_effect"])
        self.assertIn(attempt["operator_retirement_effect"], result["guidance"])
        self.assertEqual([p["id"] for p in self.ledger.notification_candidates(self.actor)], ["new-arrival"])
        self.assertEqual(tuple(self.ledger.db.iterdump()), before)

    def test_cli_is_read_only_and_offline_and_does_not_dump_frozen_native_data(self):
        identity = replace(self.identity, target={"env": {"PRIVATE": "private-route-marker"}},
                           evidence={"request": "private-envelope-marker"})
        self.ledger.update_identity(self.actor, asdict(identity))
        self.pending()
        before = tuple(self.ledger.db.iterdump())
        result = self.invoke("--token", "notice", identity=identity)
        self.assertEqual(result["identity"], {"source": "current_native_context",
                         "target_verification": "not_checked", "ancestry_verification": "not_checked"})
        self.assertEqual((result["inspection"], result["scope"]), ("read_only", "exact_token"))
        rendered = json.dumps(result)
        for forbidden in ("private-route-marker", "private-envelope-marker", "Question about", "post_ids_json", "native_identity"):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(tuple(self.ledger.db.iterdump()), before)

    def test_native_admission_awareness_reads_and_completion_are_independent(self):
        self.backend.add("read-me")
        self.backend.add("unavailable")
        self.coordinator.sync(self.actor)
        self.ledger.create_attempt(self.actor, "owner", "notice")
        self.ledger.set_transport("notice", "accepted", {"detail": "Queue accepted"})
        self.ledger.clock = lambda: 150
        self.ledger.mark_read(self.actor, ["read-me"])
        self.ledger.mark_unavailable(self.actor, "unavailable")
        self.ledger.clock = lambda: 170
        self.ledger.acknowledge_notices(self.actor, ["notice"])
        result = self.inspect()["attempts"][0]
        self.assertEqual(result["native_admission"], {"state": "unconfirmed", "recorded_at": None})
        self.assertEqual(result["agent_awareness"], {"state": "acknowledged", "recorded_at": 170})
        self.assertEqual(result["messages"], {"batch_count": 2, "tracked_count": 2,
            "read_count": 1, "unread_count": 0, "unavailable_count": 1})
        self.assertEqual(result["completion"]["state"], "not_recorded")
        self.assertEqual(result["last_progress"], {"event": "agent_awareness", "recorded_at": 170, "age_ms": 830})
        self.assertNotIn("seen_if_received", result["commands"])
        # Awareness/reads do not fabricate the missing hook. Actual admission and
        # completion then change only their own recorded dimensions.
        self.backend.add("next", when=200)
        self.coordinator.sync(self.actor)
        self.ledger.create_attempt(self.actor, "owner", "admitted")
        self.ledger.admit("admitted", self.actor, "owner", "actual-turn")
        self.ledger.complete(self.actor, "actual-turn", "owner")
        proven = self.inspect(token="admitted")["attempts"][0]
        self.assertEqual(proven["native_admission"]["state"], "confirmed")
        self.assertEqual(proven["completion"]["state"], "completed")
        self.assertEqual(proven["last_progress"]["event"], "native_completion")
        self.assertEqual(proven["explanation"]["source"], "native_admission")
        self.assertEqual(proven["agent_awareness"]["state"], "not_recorded")
        self.assertEqual(proven["messages"]["unread_count"], 1)
        self.assertNotIn("operator_retire_if_deliberate", proven["commands"])

    def test_retired_unverified_context_remains_discoverable_without_reopening_it(self):
        self.backend.add("request")
        self.coordinator.sync(self.actor)
        identity = self.ledger.actor(self.actor)["identity"]
        self.ledger.start_turn(self.actor, "owner", "enclosing", native_identity=identity)
        attempt = self.ledger.claim_context(self.actor, "owner", "enclosing", "tool", native_identity=identity)
        self.ledger.mark_context_emitted(attempt["token"], self.actor, "owner", "enclosing", "tool")
        self.ledger.complete(self.actor, "enclosing", "owner")
        self.ledger.retire_attempt(attempt["token"], "Operator deliberately abandoned this unresolved handoff")
        before = tuple(self.ledger.db.iterdump())
        result = self.inspect()
        notice = result["attempts"][0]
        self.assertEqual((result["total"], notice["status"]), (1, "retired"))
        self.assertEqual(notice["native_admission"]["state"], "unconfirmed")
        self.assertEqual(notice["completion"], {"state": "not_recorded", "recorded_at": None,
                                                "enclosing_turn_status": "completed"})
        self.assertEqual(notice["explanation"]["source"], "attempt")
        self.assertNotIn("operator_retire_if_deliberate", notice["commands"])
        self.assertEqual(tuple(self.ledger.db.iterdump()), before)

    def test_historical_token_observations_do_not_become_native_progress_or_foreign_evidence(self):
        self.pending()
        def observation(actor, surface, error, when):
            self.ledger.db.execute("INSERT INTO diagnostics(actor,surface,error,created_at) VALUES(?,?,?,?)",
                                   (actor, surface, error, when))
        observation(self.actor, "native_receipt", json.dumps([{"token": "notice", "reason": "Receipt absent in inspected history"}]), 200)
        observation(self.actor, "idle_recovery", json.dumps({"token": "notice", "reason": "Recorded owner cannot be established"}), 250)
        observation("codex:foreign", "idle_recovery", json.dumps({"token": "notice", "reason": "foreign-secret"}), 300)
        observation(self.actor, "native_receipt", json.dumps([{"token": "notice-other", "reason": "wrong-token"}, "ignored primitive"]), 350)
        observation(self.actor, "native_receipt", '[{"token":"notice","reason":', 400)
        observation(self.actor, "hook", json.dumps({"token": "notice", "reason": "irrelevant-surface"}), 450)
        observation(self.actor, "native_receipt", json.dumps({"token": "notice", "reason": {"request": "not-a-reason"}}), 500)
        before = tuple(self.ledger.db.iterdump())
        result = self.inspect()["attempts"][0]
        self.assertEqual(result["last_observation"], {"source": "idle_recovery", "recorded_at": 250,
            "reason": "Recorded owner cannot be established", "historical": True})
        self.assertEqual(result["explanation"], result["last_observation"])
        self.assertEqual(result["last_progress"]["recorded_at"], 100)
        self.assertEqual(result["native_admission"]["state"], "unconfirmed")
        self.assertNotIn("foreign-secret", json.dumps(result))
        self.assertEqual(tuple(self.ledger.db.iterdump()), before)

    def test_foreign_and_unknown_token_errors_are_indistinguishable(self):
        self.pending()
        other = replace(self.identity, session_id="33333333-3333-4333-8333-333333333333")
        self.ledger.enroll(other.key, {"provider": other.provider, "session_id": other.session_id}, "Other", "other-user")
        errors = []
        for token in ("notice", "never-existed"):
            with self.assertRaises(LedgerError) as caught:
                self.invoke("--token", token, identity=other)
            errors.append(str(caught.exception))
        self.assertEqual(errors, ["Notification is not recorded for this session"] * 2)

    def test_empty_manual_session_and_delivery_preferences_are_not_changed(self):
        other = replace(self.identity, session_id="33333333-3333-4333-8333-333333333333")
        self.ledger.enroll(other.key, {"provider": other.provider, "session_id": other.session_id}, "Manual", "other-user", mode="manual")
        hooks.publish_control(self.ledger, False, delivery_enabled=False)
        self.ledger.set_mute(self.actor, "build", True)
        before = tuple(self.ledger.db.iterdump())
        self.assertEqual(self.inspect(identity=other)["attempts"], [])
        self.assertEqual(tuple(self.ledger.db.iterdump()), before)

    def test_missing_or_unsupported_ledger_never_creates_or_migrates_state(self):
        missing = Path(self.home.name) / "absent" / "operations.sqlite3"
        with self.assertRaisesRegex(LedgerError, "No existing Murmur ledger"):
            self.invoke(path=missing)
        self.assertFalse(missing.parent.exists())
        unsupported = Path(self.home.name) / "unsupported.sqlite3"
        with sqlite3.connect(unsupported) as database:
            database.execute("CREATE TABLE untouched(value TEXT)")
            database.execute("INSERT INTO untouched VALUES('keep')")
            database.execute("PRAGMA user_version=" + str(LEDGER_SCHEMA_VERSION + 1))
        before = unsupported.read_bytes()
        with self.assertRaisesRegex(LedgerError, "no migration was run"):
            self.invoke(path=unsupported)
        self.assertEqual(unsupported.read_bytes(), before)

    def test_readonly_snapshot_includes_wal_but_cannot_write_or_follow_concurrent_changes(self):
        self.pending()
        with closing(Ledger.open_readonly(self.path)) as reader:
            self.assertEqual(reader.notification_diagnostics(self.actor)["total"], 1)
            with self.assertRaises(sqlite3.OperationalError):
                reader.db.execute("UPDATE actors SET name='must not change'")
            self.pending("later", when=200)
            self.assertEqual(reader.notification_diagnostics(self.actor)["total"], 1)
        self.assertEqual(self.inspect()["total"], 2)

    def test_identity_failure_does_not_fall_back_to_an_enrolled_actor_or_open_ledger(self):
        with patch.object(native, "current_identity", side_effect=native.IdentityError("Current provider is absent or ambiguous")), \
                patch.object(Ledger, "open_readonly", side_effect=AssertionError("No actor fallback")):
            with self.assertRaisesRegex(native.IdentityError, "absent or ambiguous"):
                cli.main(["doctor", "--notifications"])

    def test_focused_options_require_focused_view_and_limits_are_bounded(self):
        for options in (["--token", "notice"], ["--limit", "2"]):
            with self.assertRaisesRegex(ValueError, "require --notifications"):
                cli.main(["doctor", *options])
        for number in ("0", "101"):
            with redirect_stdout(io.StringIO()), patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
                cli.main(["doctor", "--notifications", "--limit", number])


if __name__ == "__main__":
    unittest.main()
