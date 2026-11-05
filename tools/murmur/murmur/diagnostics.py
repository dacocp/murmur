"""Read-only, bounded operational diagnostics; support is not native admission."""

import json

from . import native
from .backend import BackendError
from .coordinator import enrollment_capabilities, identity_dict
from .hooks import busy_context_enabled
from .ledger import LedgerError
from .native.capabilities import route_capabilities
from .protocol import ATTENTION_VERSION


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
    attempts = [dict(row) for row in ledger.db.execute("""SELECT token,mode,status,transport,
        epoch,turn_key,expected_turn_key,tool_id,created_at,emitted_at,admitted_at,
        completed_at,agent_seen_at,substr(reason,1,1000) AS reason,
        verification_json IS NOT NULL AS verification_present,
        json_extract(emission_json,'$.kind') AS handoff_kind,
        json_extract(evidence_json,'$.recovery') AS idle_recovery_json
        FROM attempts WHERE actor=? AND status IN ('pending','admitted')
        ORDER BY created_at DESC,token LIMIT 20""", (actor,))]
    for attempt in attempts:
        attempt["verification_present"] = bool(attempt["verification_present"])
        attempt["receipt_blocks_new_notifications"] = False
        kind, recovery = attempt.pop("handoff_kind"), attempt.pop("idle_recovery_json")
        if identity.provider == "codex" and attempt["mode"] == "idle":
            attempt["restart_qualified"] = kind == "codex-idle-handoff-v1"
            if recovery is not None:
                attempt["idle_recovery"] = json.loads(recovery)
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
