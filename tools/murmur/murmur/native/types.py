"""Native facts and transport outcomes; no enrollment or backend policy."""
from __future__ import annotations

from dataclasses import dataclass, field
import uuid


class NativeError(ValueError):
    """Native evidence is invalid, unsupported, or temporarily unavailable."""


class IdentityError(NativeError):
    """The current native actor or its target could not be verified."""


def validate_session_id(provider: str, value: str) -> str:
    if provider not in {"codex", "claude"} or not isinstance(value, str):
        raise IdentityError("Unsupported native provider or session ID")
    raw = value[6:] if provider == "claude" and value.startswith("local_") else value
    try:
        if str(uuid.UUID(raw)) != raw:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise IdentityError("Expected an exact canonical native session ID") from exc
    return value


@dataclass(frozen=True)
class NativeIdentity:
    provider: str
    session_id: str
    parent_session_id: str | None = None
    parent_known: bool = False
    target: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)

    def __post_init__(self):
        validate_session_id(self.provider, self.session_id)
        if type(self.parent_known) is not bool:
            raise IdentityError("Parent verification must be explicit")
        if self.parent_session_id is not None:
            validate_session_id(self.provider, self.parent_session_id)
            if not self.parent_known or self.parent_session_id == self.session_id:
                raise IdentityError("Unverified or self-referential native parent")
            if self.provider == "claude" and self.session_id.startswith("local_") != self.parent_session_id.startswith("local_"):
                raise IdentityError("Claude Desktop and CLI ancestry cannot be mixed")
        if not isinstance(self.target, dict) or not isinstance(self.evidence, dict):
            raise IdentityError("Native target and evidence must be objects")

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.session_id}"

    @property
    def actor(self) -> str:
        return self.key

    @property
    def epoch(self) -> str | None:
        """Positive native generation evidence, not a producer PID or route hash."""
        return self.target.get("native_epoch")


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    detail: str = ""
    target: dict | None = None

    def __post_init__(self):
        if self.status not in {"accepted", "rejected", "uncertain"}:
            raise NativeError("Unknown native delivery outcome")
        if self.target is not None and not isinstance(self.target, dict):
            raise NativeError("Delivery target evidence must be an object")

    @property
    def evidence(self) -> dict | None:
        return self.target


@dataclass(frozen=True)
class HookEvent:
    identity: NativeIdentity
    event: str
    native_event: str
    turn_key: str | None = None
    prompt: str | None = None
    wake_token: str | None = None
    source: str | None = None
    creation: dict | None = None
    tool_id: str | None = None
    transcript_path: str | None = None

    @property
    def epoch(self) -> str | None:
        return self.identity.epoch
