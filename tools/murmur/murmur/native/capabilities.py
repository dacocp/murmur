"""Read-only native route compatibility, separate from runtime delivery gates.

Target verification is supplied by the caller. Neither a local queue producer
nor a certified context build proves delivery, model admission or consumption.
"""
from __future__ import annotations

import math
import time

from . import context_receipts
from .types import NativeIdentity


def _status(status, reason, **evidence):
    result = {"status": status, "reason": reason}
    if evidence:
        result["evidence"] = evidence
    return result


def route_capabilities(identity, verified_target=None, *, mode="native", transcript_path=None, timeout=0.5):
    """Inspect this recipient's routes without sending, writing or re-verifying.

    ``verified_target`` must be the NativeIdentity returned by target verification
    for this recipient. Leave it absent on failure. Codex context inspection is
    possible only from the recipient's own invoking session and captured route.
    Pass a trusted exact ``transcript_path`` to inspect its immutable creation
    header against this append-owner profile; absence leaves history unverified.
    Full record continuity and a genuine
    PostToolUse receipt remain capture/admission checks.
    """
    if not isinstance(identity, NativeIdentity):
        raise ValueError("Route capabilities require a native recipient identity")
    if mode not in {"native", "manual"}:
        raise ValueError("Unsupported enrollment mode")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("Capability timeout must be finite and positive")
    if mode == "manual":
        return {route: _status("disabled", "Manual enrollment disables automatic native delivery")
                for route in ("idle_queue", "busy_context")}

    source_status = "unverified"
    if identity.provider == "codex":
        from . import codex
        candidates = [identity]
        if isinstance(verified_target, NativeIdentity) and verified_target.actor == identity.actor:
            candidates.append(verified_target)
        for candidate in candidates:
            reason = codex.automatic_enrollment_reason(candidate)
            if reason:
                return {route: _status("unsupported", reason, thread_source_status="verified",
                                       thread_source=codex.verified_thread_source(candidate))
                        for route in ("idle_queue", "busy_context")}
        if any(isinstance(source := codex.verified_thread_source(candidate), str)
               and source in {"cli", "vscode", "exec", "appServer"} for candidate in candidates):
            source_status = "verified"

    busy = _status("unverified", "The invoking recipient's Codex owner profile has not been verified")
    if identity.provider == "claude":
        busy = _status("unsupported", "This Claude profile has no certified PostToolUse context route")
        if identity.target.get("platform") == "linux" and identity.target.get("version") == "2.1.311":
            busy = _status("unverified", "The invoking Linux Claude recipient and native history are not verified")
    if (not isinstance(verified_target, NativeIdentity) or verified_target.actor != identity.actor
            or not verified_target.target):
        return {"idle_queue": _status("unverified", "This recipient's native target is not verified"),
                "busy_context": busy}

    if identity.provider == "claude":
        target = verified_target.target
        runtime = target.get("runtime")
        if runtime not in {"cli", "desktop"} or not target.get("socket_path"):
            idle = _status("unverified", "Verified Claude target has no supported idle socket route")
        else:
            idle = _status("supported", "Claude target/profile supports idle socket delivery; admission is unconfirmed",
                           transport="socket", runtime=runtime, native_epoch=verified_target.epoch)
        if (target.get("platform") == "linux" and target.get("version") == "2.1.311"
                and runtime == "cli"):
            from . import claude_context_receipts
            busy = claude_context_receipts.inspect_context_history(
                verified_target, transcript_path, timeout=timeout)
        elif runtime == "desktop":
            from . import claude_peer_receipts
            busy = claude_peer_receipts.inspect_peer_history(
                verified_target, transcript_path, timeout=timeout)
        return {"idle_queue": idle, "busy_context": busy}

    idle = _status("unverified", "Captured queue producer route is available; owner startup overrides and queue acceptance remain unverified",
                   transport="queue", local_target_verified=True, startup_overrides_verified=False,
                   thread_source_status=source_status)
    if verified_target.evidence.get("desktop_wake") is not None:
        idle = _status("supported" if source_status == "verified" else "unverified",
                       "Verified local Desktop queue and wake-only route; model admission is unconfirmed"
                       if source_status == "verified" else
                       "Verified Desktop transport; this thread's source and direct-input eligibility remain unverified",
                       transport="queue+desktop-wake", local_target_verified=True, thread_source_status=source_status)
    deadline = time.monotonic() + timeout
    try:
        # Resolve the actual caller here. A daemon or a different enrolled actor
        # cannot substitute its own Codex executable/profile for this recipient.
        from . import current_identity
        invoking = current_identity()
        if invoking.actor != identity.actor or invoking.target != verified_target.target:
            return {"idle_queue": idle, "busy_context": _status(
                "unverified", "Context profile inspection requires this recipient's invoking identity and route")}
        owner = context_receipts.inspect_context_owner(timeout=deadline - time.monotonic())
        version = context_receipts.context_profile_version(owner["sha256"])
        if version is None or owner.get("version") != version:
            busy = _status("unsupported", "Invoking Codex owner hash is not a certified context profile",
                           owner=owner, profile_status="unsupported")
        else:
            history = _status("unverified", "No trusted native transcript path was supplied; immutable history header is not checked")
            if transcript_path is not None:
                history = context_receipts.inspect_context_history(
                    verified_target, owner, transcript_path, timeout=deadline - time.monotonic())
            reason = "Certified owner profile; " + history["reason"]
            if history["status"] == "supported":
                reason += "; full capture and admission remain separate checks"
            busy = _status(history["status"], reason, owner=owner, profile_status="supported",
                           history=history, scope="invoking_recipient_owner_and_history_header")
    except (OSError, ValueError, RuntimeError) as error:
        busy = _status("unverified", "Invoking Codex owner inspection is unavailable: " + str(error))
    return {"idle_queue": idle, "busy_context": busy}
