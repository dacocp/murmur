"""Fair host scheduling; slow HTTP cannot monopolize native maintenance.

Each worker owns its SQLite connection. The main loop publishes a lease only
for actors whose local maintenance actually completes. A stuck proof/owner
inspection therefore loses its lease even while network workers remain alive.
"""

from concurrent.futures import ThreadPoolExecutor
from collections import deque
import json
import sqlite3
import time

from . import config, native
from .backend import Backend
from .coordinator import Coordinator
from .hooks import CONTROL_LEASE_MS, busy_context_enabled, publish_control, record_error
from .ledger import Ledger


WORKERS = 4
POLL_INTERVAL = 1.0


def network_tick(root, actor, stopped, after_op=None):
    settings = config.load(root)
    ledger = Ledger(config.ledger_path(root))
    try:
        coordinator = Coordinator(ledger, Backend(settings["url"], settings["token"]), native)
        coordinator.sync(actor, max_pages=1)
        operations = ledger.unresolved_outbounds(actor, 1, after_op=after_op)
        coordinator.reconcile_outbound(actor, operations=operations, can_dispatch=lambda: not stopped.is_set())
        result = {"op_id": operations[-1]["op_id"] if operations else after_op}
        # A successful sync must precede an idle send. Reload the enable flag
        # after HTTP, and never start a new send while shutdown is requested.
        settings = config.load(root)
        record = ledger.actor(actor)
        if stopped.is_set() or settings.get("delivery_enabled") is not True or record["mode"] == "manual":
            return result
        candidates = ledger.notification_candidates(actor, limit=1)
        open_attempts = [attempt for attempt in ledger.pending_attempts(actor)
                         if attempt["mode"] == "idle" and attempt.get("agent_seen_at") is None]
        if not candidates and not open_attempts:
            return result
        recorded = native.NativeIdentity(**record["identity"])
        active = ledger.active_turn(actor)
        if active:
            if native.supports_peer_delivery(recorded):
                def enabled():
                    latest = config.load(root)
                    selected = latest.get("busy_context_actors")
                    return (not stopped.is_set() and latest.get("delivery_enabled") is True
                            and latest.get("busy_context_enabled") is True
                            and (selected is None or isinstance(selected, list) and actor in selected)
                            and busy_context_enabled(ledger, actor))
                if candidates and enabled():
                    coordinator.deliver_peer(recorded, active["turn_key"],
                                              expected_identity=record["identity"], enabled=enabled)
            return result
        if open_attempts and recorded.provider == "codex":
            def recovery_enabled():
                return (not stopped.is_set() and config.load(root).get("delivery_enabled") is True
                        and ledger.actor(actor)["mode"] == "native")
            try:
                recovery = coordinator.recover_idle(open_attempts[0], record["identity"], enabled=recovery_enabled)
                if recovery is not None:
                    result["idle_recovery"] = {**recovery, "token": open_attempts[0]["token"]}
            except (ValueError, RuntimeError, OSError) as error:
                result["idle_recovery"] = {"status": "held", "token": open_attempts[0]["token"],
                                           "reason": str(error)}
            # Recovery can only wake the old retained input. Its uncertainty
            # does not reserve new arrivals or authorize resending the old batch.
        if not candidates:
            return result
        identity = native.prepare_idle_target(recorded)
        settings = config.load(root)
        if stopped.is_set() or settings.get("delivery_enabled") is not True:
            return result
        coordinator.deliver(identity, expected_identity=record["identity"])
        return result
    finally:
        ledger.close()


def local_tick(root, actor, after_token):
    ledger = Ledger(config.ledger_path(root))
    try:
        if ledger.actor(actor)["mode"] == "manual":
            return {"token": after_token, "unknown": [], "recovery": None}
        coordinator = Coordinator(ledger, None, native)
        attempts = ledger.unconfirmed_contexts(actor, limit=1, after_token=after_token)
        active = ledger.active_turn(actor)
        # Cold parsing progresses outside hooks, and only for actual delivery
        # work. A cache miss never retires a handoff or consumes its arrivals.
        identity = attempts[0].get("native_identity") if attempts else (
            active.get("identity") if active and ledger._unread(actor, eligible=True, limit=1) else None)
        if identity:
            recipient = native.NativeIdentity(**identity)
            if native.supports_peer_delivery(recipient):
                try:
                    native.warm_peer_history(recipient, timeout=.3)
                except native.NativeError:
                    # Reconciliation or the eligible network capture reports the
                    # exact failure. Warming alone is no delivery outcome.
                    pass
        unknown = coordinator.reconcile_context(actor, attempts=attempts)
        recovery = None
        if active and active.get("identity"):
            owner = native.NativeIdentity(**active["identity"])
            evidence = native.owner_process_state(owner)
            if evidence["status"] == "ended" and ledger.interrupt(actor, active["turn_key"], active["epoch"]):
                recovery = evidence
        return {"token": attempts[-1]["token"] if attempts else after_token,
                "unknown": unknown, "recovery": recovery}
    finally:
        ledger.close()


class HostControl:
    def __init__(self, root, ledger, stopped, *, interval=POLL_INTERVAL, workers=WORKERS, clock=time.monotonic):
        self.root, self.ledger, self.stopped = root, ledger, stopped
        self.interval, self.workers, self.clock = interval, workers, clock
        self.local_progress, self.context_cursor, self.operation_cursor, self.previous_errors = {}, {}, {}, {}

    def error(self, actor, surface, message, *, key=None):
        key = key or (actor, surface)
        if self.previous_errors.get(key) != message:
            record_error(self.ledger, actor, surface, message)
            self.previous_errors[key] = message

    def collect(self, jobs, kind, completed):
        for actor, future in list(jobs.items()):
            if not future.done():
                continue
            del jobs[actor]
            completed.add(actor)
            try:
                result = future.result()
                self.previous_errors.pop((actor, kind), None)
                if kind == "local":
                    self.local_progress[actor] = self.clock()
                    self.context_cursor[actor] = result["token"]
                    receipt_key = (actor, "native_receipt", result["token"])
                    if result["unknown"]:
                        self.error(actor, "native_receipt", json.dumps(result["unknown"], sort_keys=True), key=receipt_key)
                    else:
                        self.previous_errors.pop(receipt_key, None)
                    if result["recovery"]:
                        record_error(self.ledger, actor, "native_recovery", json.dumps(result["recovery"], sort_keys=True))
                else:
                    self.operation_cursor[actor] = result["op_id"]
                    recovery = result.get("idle_recovery")
                    if recovery and recovery.get("status") == "held":
                        self.error(actor, "idle_recovery", json.dumps(recovery, sort_keys=True))
                    else:
                        self.previous_errors.pop((actor, "idle_recovery"), None)
            except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
                self.error(actor, kind, str(error))

    def publish(self, settings, records):
        enabled = settings.get("delivery_enabled") is True and settings.get("busy_context_enabled") is True
        selected = settings.get("busy_context_actors")
        # Reuse central validation even when configuration disables delivery.
        if selected is not None and (not isinstance(selected, list) or len(selected) > 1000
                or any(not isinstance(a, str) or not a or len(a) > 160 for a in selected)):
            raise ValueError("Busy context recipients must be a bounded list of actor IDs")
        now = self.clock()
        healthy = [record["actor"] for record in records if record["mode"] == "native"
                   and 0 <= now - self.local_progress.get(record["actor"], float("-inf")) < CONTROL_LEASE_MS / 1000
                   and (selected is None or record["actor"] in selected)]
        publish_control(self.ledger, enabled, healthy,
                        delivery_enabled=settings.get("delivery_enabled") is True)

    def run(self, *, once=False):
        pools = {kind: ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="murmur-" + kind)
                 for kind in ("local", "network")}
        jobs = {kind: {} for kind in pools}
        queues = {kind: deque() for kind in pools}
        completed = {kind: set() for kind in pools}
        try:
            while not self.stopped.is_set():
                settings = config.load(self.root)
                records = self.ledger.actors()
                actors = {record["actor"] for record in records}
                for kind in pools:
                    self.collect(jobs[kind], kind, completed[kind])
                    queued = set(queues[kind])
                    for actor in sorted(actors):
                        if actor not in jobs[kind] and actor not in queued and (not once or actor not in completed[kind]):
                            queues[kind].append(actor)
                    while queues[kind] and len(jobs[kind]) < self.workers:
                        actor = queues[kind].popleft()
                        if actor not in actors:
                            continue
                        if kind == "local":
                            jobs[kind][actor] = pools[kind].submit(local_tick, self.root, actor, self.context_cursor.get(actor))
                        else:
                            jobs[kind][actor] = pools[kind].submit(network_tick, self.root, actor, self.stopped,
                                                               self.operation_cursor.get(actor))
                self.publish(settings, records)
                if once and all(actors <= completed[kind] for kind in pools):
                    break
                self.stopped.wait(self.interval)
        finally:
            # Revoke before waiting for any deadline-bound in-flight work.
            self.stopped.set()
            try:
                publish_control(self.ledger, False)
            finally:
                for pool in pools.values():
                    pool.shutdown(wait=True, cancel_futures=True)
