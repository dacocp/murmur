"""Backend-independent native identity, creation evidence and doorbell delivery."""
from __future__ import annotations

import os

from .types import DeliveryResult, HookEvent, IdentityError, NativeError, NativeIdentity
from .hooks import format_doorbell, parse_hook, parse_wake, turn_key, validate_doorbell


def prepare_context_receipt(captured):
    if not isinstance(captured, HookEvent) or not isinstance(captured.identity, NativeIdentity):
        raise NativeError("Native context requires an actual PostToolUse event")
    if captured.identity.provider == "claude":
        from . import claude_context_receipts as module
    elif captured.identity.provider == "codex":
        from . import context_receipts as module
    else:
        raise NativeError("Unsupported native context provider")
    return module.prepare_context_receipt(captured)


def verify_context_receipt(identity, attempt, *, timeout=.5):
    if not isinstance(identity, NativeIdentity):
        return {"status": "unknown", "reason": "Native context recipient identity is invalid"}
    if identity.provider == "claude":
        from . import claude_context_receipts as module
    elif identity.provider == "codex":
        from . import context_receipts as module
    else:
        return {"status": "unknown", "reason": "Unsupported native context provider"}
    return module.verify_context_receipt(identity, attempt, timeout=timeout)


def supports_peer_delivery(identity):
    from .claude_peer_receipts import supports_peer_delivery as supported
    return supported(identity)


def prepare_peer_receipt(identity, turn_key, *, timeout=3):
    from .claude_peer_receipts import prepare_peer_receipt as prepare
    return prepare(identity, turn_key, timeout=timeout)


def warm_peer_history(identity, *, timeout=.3):
    from .claude_peer_receipts import warm_peer_history as warm
    return warm(identity, timeout=timeout)


def peer_emission(identity, message, token, request):
    from .claude_peer_receipts import peer_emission as emission
    return emission(identity, message, token, request)


def deliver_peer(identity, message, token, request, emission, *, timeout=5):
    from .claude_peer_receipts import deliver_peer as deliver
    return deliver(identity, message, token, request, emission, timeout=timeout)


def verify_peer_receipt(identity, attempt, *, timeout=.5):
    from .claude_peer_receipts import verify_peer_receipt as verify
    return verify(identity, attempt, timeout=timeout)


def _adapter(provider):
    if provider == "codex":
        from . import codex
        return codex
    if provider == "claude":
        from . import claude
        return claude
    raise IdentityError("Unsupported native provider")


def current_identity(payload=None, *, env=None, provider=None) -> NativeIdentity:
    environment = os.environ if env is None else env
    if provider is None:
        has_claude = any(environment.get(key) for key in (
            "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID"))
        has_codex = bool(environment.get("CODEX_THREAD_ID"))
        if has_claude == has_codex:
            raise IdentityError("Current native provider is absent or ambiguous")
        provider = "claude" if has_claude else "codex"
    return _adapter(provider).current_identity(payload, env=environment)


def resolve_identity(identity: NativeIdentity, *, timeout=3, lineage_proof=None) -> NativeIdentity:
    options = {"lineage_proof": lineage_proof} if identity.provider == "claude" else {}
    return _adapter(identity.provider).resolve_identity(identity, timeout=timeout, **options)


def verify_target(identity: NativeIdentity, *, timeout=3) -> NativeIdentity:
    return _adapter(identity.provider).verify_target(identity, timeout=timeout)


def prepare_idle_target(identity: NativeIdentity, *, timeout=3) -> NativeIdentity:
    """Prepare only a new idle claim; existing attempts retain strict routing."""
    if not isinstance(identity, NativeIdentity):
        raise IdentityError("Expected a native target")
    if identity.provider == "claude":
        return _adapter(identity.provider).prepare_idle_target(identity, timeout=timeout)
    if identity.provider == "codex" and identity.evidence.get("desktop_wake") is not None:
        from . import codex_queue_recovery
        return codex_queue_recovery.prepare_idle_target(identity, timeout=timeout)
    return verify_target(identity, timeout=timeout)


def recover_idle_target(identity: NativeIdentity, *, timeout=3) -> NativeIdentity:
    """Rediscover only a same-engine Claude recipient with an ended owner."""
    if not isinstance(identity, NativeIdentity) or identity.provider != "claude":
        raise IdentityError("Native idle target rediscovery is unsupported for this provider")
    return _adapter(identity.provider).recover_idle_target(identity, timeout=timeout)


def owner_process_state(identity: NativeIdentity, *, timeout=3) -> dict:
    """Inspect a recorded owner without treating a replacement engine as exit."""
    if identity.provider == "claude":
        return _adapter(identity.provider).owner_process_state(identity, timeout=timeout)
    if identity.provider == "codex" and identity.evidence.get("desktop_wake") is not None:
        from . import desktop_wake
        return desktop_wake.owner_process_state(identity, timeout=timeout)
    return {"status": "unknown", "reason": "This provider exposes no verified owner-process epoch"}


def deliver(identity: NativeIdentity, message: str, *, timeout=5) -> DeliveryResult:
    try:
        token = parse_wake(message)
        if token is None:
            raise NativeError("Native delivery requires one opaque wake marker")
        validate_doorbell(message, token)
    except NativeError as exc:
        return DeliveryResult("rejected", str(exc))
    return _adapter(identity.provider).deliver(identity, message, token, timeout=timeout)


def prepare_idle_handoff(identity, message, token, *, state_root, timeout=3):
    if identity.provider != "codex" or identity.evidence.get("desktop_wake") is None:
        return None
    from .codex_queue_recovery import prepare_idle_handoff as prepare
    return prepare(identity, message, token, state_root=state_root, timeout=timeout)


def probe_idle_recovery(identity, attempt, *, state_root, timeout=3):
    from .codex_queue_recovery import probe_idle_recovery as probe
    return probe(identity, attempt, state_root=state_root, timeout=timeout)


def wake_idle_recovery(session_id, probe, *, before_wake=None, timeout=3):
    from .codex_queue_recovery import wake_idle_recovery as wake
    return wake(session_id, probe, before_wake=before_wake, timeout=timeout)


__all__ = [
    "DeliveryResult", "HookEvent", "IdentityError", "NativeError", "NativeIdentity",
    "current_identity", "resolve_identity", "verify_target", "owner_process_state", "deliver",
    "prepare_idle_target", "recover_idle_target",
    "prepare_idle_handoff", "probe_idle_recovery", "wake_idle_recovery",
    "format_doorbell", "parse_hook", "parse_wake", "turn_key",
    "prepare_context_receipt", "verify_context_receipt",
    "supports_peer_delivery", "prepare_peer_receipt", "warm_peer_history", "peer_emission", "deliver_peer", "verify_peer_receipt",
]
