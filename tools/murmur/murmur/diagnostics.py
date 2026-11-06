"""Read-only, bounded operational diagnostics; support is not native admission."""

import json

from . import native
from .backend import BackendError
from .coordinator import enrollment_capabilities, identity_dict
from .hooks import busy_context_enabled
from .ledger import LedgerError
from .native.capabilities import route_capabilities
from .protocol import ATTENTION_VERSION


_RETIREMENT_EFFECT = (
    "Operator retirement deliberately abandons a pending attempt; it does not repair proof, replay its batch, "
    "read messages or cancel already queued/emitted native input. Later verified context can still settle.")


def ancestry_fields(identity):
    """Keep verified ancestry separate from current target/delivery support."""
    value = identity_dict(identity)
    known = value.get("parent_known") is True
    fields = {"ancestry_status": "verified" if known else "not_checked",
              "parent_known": True if known else None,
              "parent_session_id": value.get("parent_session_id") if known else None}
    if not known:
        fields["ancestry_note"] = (
            "Ancestry has not been checked. Normal registration verifies it; "
            "manual sidechat enrollment does not.")
    evidence = value.get("evidence", {})
    fields["lineage_complete"] = evidence.get("lineage_complete")
    if evidence.get("lineage_reason"):
        fields["lineage_reason"] = evidence["lineage_reason"]
    return fields


def server_health(backend):
    """Keep a bounded remote failure separate from locally observed facts."""
    try:
        # Backend bounds this probe to two seconds, independently of send/read timeouts.
        value = backend.request("GET", "/health")
        if not isinstance(value, dict):
            raise BackendError("Invalid server health result")
        build = value.get("build", {})
        if not isinstance(build, dict) or not isinstance(build.get("compatibility", {}), dict):
            raise BackendError("Invalid server compatibility metadata")
        protocol = build.get("compatibility", {}).get("attention_protocol")
        compatibility = {"required_attention_protocol": ATTENTION_VERSION,
                         "reported_attention_protocol": protocol,
                         "status": "unverified" if protocol is None else
                                   "compatible" if type(protocol) is int and protocol == ATTENTION_VERSION else "incompatible"}
        if compatibility["status"] != "compatible":
            compatibility["reason"] = "A matching attention protocol is required; upgrade the server and client together"
        return {**value, "status": "available", "compatibility": compatibility}
    except (BackendError, OSError) as error:
        return {"status": "unavailable", "error": str(error)}


def enrollment_fields(ledger, record):
    if record is None:
        return {"authorized": False, "account": {"status": "not_requested", "user_id": None}}
    result = {**enrollment_capabilities(record["mode"]), "authorized": True,
              "account": {"status": "provisioned" if record["user_id"] else "pending",
                          "user_id": record["user_id"]}}
    identity = record["identity"]
    parent = identity.get("parent_session_id") if identity.get("parent_known") else None
    if parent:
        actor = identity["provider"] + ":" + parent
        try:
            local = ledger.actor(actor)
        except LedgerError:
            local = None
        result["parent"] = {"actor": actor, "authorized_locally": local is not None,
                            "account_provisioned_locally": bool(local and local["user_id"])}
    else:
        result["parent"] = None
    return result


def notification_attempt(actor, row, now):
    """Present recorded facts; neither age nor a missing receipt establishes loss."""
    item = {key: row[key] for key in (
        "token", "mode", "status", "transport", "epoch", "turn_key", "expected_turn_key", "tool_id",
        "created_at", "emitted_at", "admitted_at", "completed_at", "agent_seen_at", "reason")}
    item["verification_present"] = bool(row["verification_present"])
    item["receipt_blocks_new_notifications"] = False
    item["age_ms"] = max(0, now - row["created_at"])
    recovery = json.loads(row["idle_recovery_json"]) if row["idle_recovery_json"] else {}
    if not isinstance(recovery, dict):
        recovery = {}
    if actor.startswith("codex:") and row["mode"] == "idle":
        item["restart_qualified"] = row["handoff_kind"] == "codex-idle-handoff-v1"
        item["restart_qualification_scope"] = "recorded_handoff"
        item["current_recovery_verification"] = "not_checked"
        if recovery:
            item["idle_recovery"] = {}
            for key in ("status", "detail", "queue_id", "action_id"):
                if isinstance(recovery.get(key), str):
                    item["idle_recovery"][key] = recovery[key][:1000]
            for key in ("claimed_at", "finished_at", "lease_until"):
                if type(recovery.get(key)) is int:
                    item["idle_recovery"][key] = recovery[key]
    progress = [(row[key], event) for key, event in (
        ("created_at", "attempt_created"), ("emitted_at", "handoff_recorded"),
        ("admitted_at", "native_admission"), ("agent_seen_at", "agent_awareness"),
        ("completed_at", "native_completion"), ("last_read_at", "message_read")) if row[key] is not None]
    for key, event in (("claimed_at", "wake_only_claim"), ("finished_at", "wake_only_result")):
        if type(recovery.get(key)) is int:
            progress.append((recovery[key], event))
    stages = ("attempt_created", "handoff_recorded", "wake_only_claim", "wake_only_result",
              "native_admission", "agent_awareness", "message_read", "native_completion")
    at, event = max(progress, key=lambda entry: (entry[0], stages.index(entry[1])))
    item["last_progress"] = {"event": event, "recorded_at": at, "age_ms": max(0, now - at)}
    observation = row.get("observation")
    item["last_observation"] = ({"source": observation["surface"], "recorded_at": observation["created_at"],
                                 "reason": observation["reason"], "historical": True} if observation else None)
    if row["admitted_at"] is not None:
        explanation = {"source": "native_admission", "reason": "Native admission is recorded",
                       "recorded_at": row["admitted_at"]}
    elif item["reason"]:
        explanation = {"source": "attempt", "reason": item["reason"], "recorded_at": None}
    elif observation:
        explanation = dict(item["last_observation"])
    elif isinstance(row.get("transport_detail"), str) and row["transport_detail"]:
        explanation = {"source": "transport", "reason": row["transport_detail"], "recorded_at": None}
    else:
        explanation = {"source": "recorded_state", "recorded_at": None,
                       "reason": "Native admission is recorded" if row["admitted_at"] is not None else
                                 "Native admission is not confirmed; the ledger does not establish why"}
    item["explanation"] = explanation
    item["native_admission"] = {"state": "confirmed" if row["admitted_at"] is not None else "unconfirmed",
                                 "recorded_at": row["admitted_at"]}
    item["agent_awareness"] = {"state": "acknowledged" if row["agent_seen_at"] is not None else "not_recorded",
                               "recorded_at": row["agent_seen_at"]}
    item["messages"] = {key: row[column] for key, column in (
        ("batch_count", "batch_post_count"), ("tracked_count", "tracked_post_count"),
        ("read_count", "read_post_count"), ("unread_count", "unread_post_count"),
        ("unavailable_count", "unavailable_post_count"))}
    item["completion"] = {"state": row["status"] if row["status"] in ("completed", "interrupted") else "not_recorded",
                           "recorded_at": row["completed_at"], "enclosing_turn_status": row["enclosing_turn_status"]}
    item["reservation"] = {"scope": "none" if row["transport"] == "rejected" else "frozen_batch_only",
                            "post_count": 0 if row["transport"] == "rejected" else row["batch_post_count"]}
    item["commands"] = {"check_inbox": "murmur inbox"}
    if row["agent_seen_at"] is None:
        item["commands"]["seen_if_received"] = "murmur seen " + row["token"]
    if row["status"] == "pending":
        item["operator_retirement_effect"] = _RETIREMENT_EFFECT
        item["commands"]["operator_retire_if_deliberate"] = (
            "murmur operator retire " + actor + " --token " + row["token"] + " --reason 'EXPLICIT OPERATOR REASON'")
    return item


def notification_report(ledger, identity, *, token=None, limit=20):
    """A local snapshot: no native target probes, credentials or message-body reads."""
    actor, now = identity.key, ledger.clock()
    account = ledger.actor(actor)
    window = ledger.notification_diagnostics(actor, token=token, limit=limit)
    return {"actor": actor, "inspection": "read_only", "checked_at": now,
            "enrollment_mode": account["mode"], "delivery_configuration": "not_checked",
            "identity": {"source": "current_native_context", "target_verification": "not_checked",
                         "ancestry_verification": "not_checked"},
            "scope": "exact_token" if token is not None else "outstanding_notifications",
            "attempts": [notification_attempt(actor, row, now) for row in window["attempts"]],
            "total": window["total"], "unconfirmed_native_count": window["unconfirmed_native_count"],
            "truncated": window["truncated"], "limit": limit,
            "guidance": [
                "Each attempt reserves only its frozen batch; newer eligible arrivals can notify independently.",
                "Admission, agent awareness, message reading and native completion are separate recorded facts.",
                "Age or missing proof does not establish that a notification was lost or remains in flight.",
                "inbox checks pending work and acknowledges outstanding notices after successful output; it does not read messages.",
                "Use seen only if you received that exact doorbell. It records awareness, not native admission or reading.",
                _RETIREMENT_EFFECT,
                "Last progress uses recorded lifecycle/message/wake timestamps. Historical observations are deduplicated, not a last-check heartbeat."]}


def report(coordinator, identity, settings):
    ledger = coordinator.ledger
    actor = identity.key
    try:
        record = ledger.actor(actor)
    except LedgerError:
        record = None
    manual = record is not None and record["mode"] == "manual"
    target = None
    if manual:
        native_status = {"ok": True, "transport": "manual",
                         "meaning": "Manual account; automatic native delivery is disabled"}
    elif record:
        try:
            target = native.verify_target(native.NativeIdentity(**record["identity"]))
            native_status = {"ok": True, "target": target.target, "epoch": target.epoch,
                             "recorded_target": record["identity"].get("target", {}),
                             "target_matches_recorded": target.target == record["identity"].get("target", {}),
                             "meaning": "Target verification only; see routes for support, not admission"}
        except (ValueError, RuntimeError, OSError) as error:
            native_status = {"ok": False, "error": str(error),
                             "recorded_target": record["identity"].get("target", {}),
                             "target_matches_recorded": None}
    else:
        native_status = {"target": identity.target, "meaning": "Not enrolled; target and ancestry have not been verified"}
    native_status.update(ancestry_fields(record["identity"] if record else identity))
    delivery = bool(settings.get("delivery_enabled", False))
    busy = bool(settings.get("busy_context_enabled", False))
    plan = ledger.onboarding(actor) if record else None
    onboarding = None
    if plan:
        onboarding = {key: plan[key] for key in ("choice", "source_actor", "status", "error")}
        onboarding["plan_id"] = plan.get("plan_id")
        onboarding["progress"] = plan["progress"]
        onboarding["capture"] = {key: value for key, value in (plan["snapshot"] or {}).items()
                                 if key in ("local_captured_at", "saved_started_at", "saved_finished_at")}
        if plan["status"] != "complete":
            onboarding["resume"] = "Repeat register without changing --inherit; the frozen copy is retained"
    lease = record is not None and not manual and busy_context_enabled(ledger, actor)
    # Lightweight captures omit immutable thread-source metadata. A stale owner
    # must not hide the enrolled recipient's verified unsupported source.
    route_identity = native.NativeIdentity(**record["identity"]) if record else identity
    routes = route_capabilities(route_identity, verified_target=target, mode=record["mode"] if record else "native")
    for name, route in routes.items():
        route["configured"] = delivery and (name == "idle_queue" or busy)
        route["blocked_by"] = (["manual enrollment"] if manual else [])
        if record is None:
            route["blocked_by"].append("not enrolled")
        elif not record["user_id"]:
            route["blocked_by"].append("server account not provisioned")
        if plan and plan["status"] != "complete":
            route["blocked_by"].append("pending onboarding")
        if not route["configured"]:
            route["blocked_by"].append("delivery disabled" if not delivery else "busy context disabled")
        if name == "busy_context" and not lease:
            route["blocked_by"].append("no current coordinator lease for this actor")
    counts = {row["state"]: row["count"] for row in ledger.db.execute(
        "SELECT state,count(*) AS count FROM outbound WHERE actor=? GROUP BY state", (actor,))}

    def operations(condition, limit):
        return [dict(row) for row in ledger.db.execute(
            "SELECT op_id,state,substr(error,1,1000) AS error FROM outbound WHERE actor=? AND "
            + condition + " ORDER BY updated_at DESC,op_id LIMIT ?", (actor, limit))]

    errors = [dict(row) for row in ledger.db.execute("""SELECT surface,error,
        min(created_at) AS first_seen,max(created_at) AS created_at,count(*) AS count
        FROM diagnostics WHERE actor=? GROUP BY surface,error
        ORDER BY max(created_at) DESC,surface,error LIMIT 10""", (actor,))]
    attempt_count = ledger.db.execute("SELECT count(*) FROM attempts WHERE actor=? AND status IN ('pending','admitted')",
                                      (actor,)).fetchone()[0]
    window = ledger.notification_diagnostics(actor, limit=20, active_only=True, newest_first=True) if record else {"attempts": []}
    now = ledger.clock()
    attempts = [notification_attempt(actor, row, now) for row in window["attempts"]]
    admitted = ledger.db.execute("""SELECT token,mode,status,transport,epoch,turn_key,
        admitted_at,completed_at FROM attempts WHERE actor=? AND admitted_at IS NOT NULL
        ORDER BY admitted_at DESC,token LIMIT 1""", (actor,)).fetchone()
    activity = {"state": "manual"} if manual else coordinator.activity(identity) if record else {"state": "unknown"}
    notification_state = None
    if record:
        follows = ledger.following(actor)
        pending = ledger.pending_auto_follows(actor)
        chats = ledger.conversations(actor)
        notification_state = {
            "conversations": [{"id": chat["id"], "role": chat["role"],
                               **ledger.notification_mode(actor, chat["id"]),
                               "followed_thread_count": sum(f["channel_id"] == chat["id"] for f in follows)}
                              for chat in chats[:20]],
            "conversation_count": len(chats),
            "following": follows[:20], "following_count": len(follows),
            "pending_auto_follows": pending[:20], "pending_auto_follow_count": len(pending),
        }
    return {"actor": actor, "enrolled": record is not None, "native": native_status,
            "enrollment": enrollment_fields(ledger, record),
            "onboarding": onboarding,
            "activity": activity,
            "active_turn": ledger.active_turn(actor) if record else None,
            "delivery_enabled": delivery, "busy_context": {"configured": busy, "coordinator_lease": lease},
            "routes": routes, "server": server_health(coordinator.backend),
            "attempts": attempts, "attempt_count": attempt_count,
            "last_admission": dict(admitted) if admitted else None,
            "notifications": notification_state,
            "operations": operations("state IN ('pending','uncertain')", 20),
            "failed_operations": operations("state='failed'", 10), "operation_counts": counts,
            "errors": errors, "errors_scope": "Historical exact surface/error groups; current route support is above"}
