"""Bounded native-hook decoding and the visible doorbell correlation contract."""
from __future__ import annotations

import json
import os
import re

from .types import HookEvent, NativeError, validate_session_id

MAX_HOOK_BYTES = 1024 * 1024
MAX_DOORBELL_BYTES = 4096
MAX_TOOL_ID_BYTES = 256
TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
MARKER = re.compile(r"\[wake:([A-Za-z0-9_-]{1,128})\]")
EVENTS = {
    "SessionStart": "session_start", "UserPromptSubmit": "submit",
    "Stop": "complete", "StopFailure": "complete", "Interrupt": "interrupt",
    "SessionEnd": "session_end",
    "PostToolUse": "post_tool",
}


def parse_wake(prompt: str) -> str | None:
    """Recognize our doorbell only; quoting a marker in ordinary work is inert."""
    if not isinstance(prompt, str):
        raise NativeError("Native prompt must be text")
    if not prompt.startswith(("You have new messages", "Murmur:")):
        return None
    markers = list(MARKER.finditer(prompt))
    if "[wake:" not in prompt:
        return None
    if len(markers) != 1 or prompt.count("[wake:") != 1:
        raise NativeError("Malformed or duplicate Murmur wake marker")
    if len(prompt.encode("utf-8")) > MAX_DOORBELL_BYTES:
        raise NativeError("Native doorbell exceeds its size bound")
    return markers[0][1]


def validate_doorbell(text: str, token: str) -> None:
    if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
        raise NativeError("Invalid opaque wake token")
    if parse_wake(text) != token:
        raise NativeError("Doorbell does not match its delivery token")


def format_doorbell(chat_names, token: str) -> str:
    if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
        raise NativeError("Invalid opaque wake token")
    names = []
    for value in chat_names:
        if not isinstance(value, str):
            raise NativeError("Conversation names must be text")
        name = "".join(c for c in value if c.isprintable()).replace("[", "(").replace("]", ")").strip()[:64]
        if name and name not in names:
            names.append(name)
        if len(names) == 3:
            break
    suffix = " in " + ", ".join(f"‘{name}’" for name in names) if names else ""
    return f"You have new messages{suffix}. [wake:{token}]"


def turn_key(provider: str, payload: dict) -> str:
    if provider not in {"codex", "claude"}:
        raise NativeError("Unsupported native hook provider")
    field = "turn_id" if provider == "codex" else "prompt_id"
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise NativeError(f"Native hook requires a distinct {field}")
    if provider == "codex":
        return f"codex:{value}"
    engine = validate_session_id("claude", payload.get("session_id"))
    if engine.startswith("local_"):
        raise NativeError("Claude hook session ID must name the engine, not Desktop host")
    return f"claude:{engine}:{value}"


def parse_hook(provider: str, event: str, payload: dict, *, env=None) -> HookEvent | None:
    """Decode local facts only; enrollment/admission/SQL belong to the caller."""
    if provider not in {"codex", "claude"} or event not in EVENTS:
        raise NativeError("Unsupported native hook event")
    if (provider == "codex" and event == "StopFailure"
            or provider == "claude" and event == "Interrupt"):
        raise NativeError("This native client does not supply that hook")
    if not isinstance(payload, dict) or payload.get("hook_event_name") != event:
        raise NativeError("Native payload does not match its configured hook event")
    try:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise NativeError("Native hook payload must be a JSON object") from exc
    if len(encoded) > MAX_HOOK_BYTES:
        raise NativeError("Native hook payload exceeds its size bound")
    if payload.get("agent_id"):
        return None  # Native subagents may carry their parent's session address.
    tool_id = transcript_path = None
    if event == "PostToolUse":
        tool_id = payload.get("tool_use_id")
        if (not isinstance(tool_id, str) or not tool_id.strip()
                or len(tool_id.encode("utf-8")) > MAX_TOOL_ID_BYTES):
            raise NativeError("PostToolUse requires a bounded nonempty tool_use_id")
        transcript_path = payload.get("transcript_path")
        if transcript_path is not None and (not isinstance(transcript_path, str)
                or "\0" in transcript_path or not os.path.isabs(transcript_path)):
            raise NativeError("Native transcript path must be an absolute local path")
    from . import current_identity
    creation = None
    if provider == "claude" and event in {"SessionStart", "SessionEnd"}:
        from .claude import capture_creation
        creation = capture_creation(payload, env=env)
    if creation is not None:
        from .claude import identity_from_creation
        identity = identity_from_creation(creation)
    else:
        identity = current_identity(payload, env=env, provider=provider)
    prompt = payload.get("prompt") if event == "UserPromptSubmit" else None
    if event == "UserPromptSubmit" and not isinstance(prompt, str):
        raise NativeError("Submit hook requires the actual prompt text")
    source = payload.get("source")
    if source is not None and (not isinstance(source, str) or len(source) > 128):
        raise NativeError("Invalid native hook source")
    return HookEvent(
        identity=identity, event=EVENTS[event], native_event=event,
        turn_key=turn_key(provider, payload) if event not in {"SessionStart", "SessionEnd"} else None,
        prompt=prompt, wake_token=parse_wake(prompt) if prompt is not None else None,
        source=source, creation=creation, tool_id=tool_id, transcript_path=transcript_path,
    )
