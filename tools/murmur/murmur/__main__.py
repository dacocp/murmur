"""Murmur's CLI. Native identity is verified; no caller-selected actor override."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

from . import config
from .backend import Backend
from .coordinator import Coordinator, actor_key
from .hooks import handle, initialize, record_error
from .ledger import Ledger


def bounded_number(low, high):
    def parse(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return number
    return parse


def parser():
    result = argparse.ArgumentParser(prog="murmur", description=
        "Session messaging: inbox lists pending work; read consumes only returned messages; history/show never consume.")
    commands = result.add_subparsers(dest="command", required=True)

    def add(name, summary, **options):
        return commands.add_parser(name, help=summary, description=summary,
                                   formatter_class=argparse.ArgumentDefaultsHelpFormatter, **options)

    configure = add("configure", "Configure this local runtime from a protected token file.")
    configure.add_argument("--url", required=True)
    configure.add_argument("--token-file", type=Path, required=True)
    configure.add_argument("--enable-delivery", action="store_true")
    configure.add_argument("--enable-busy-context", action="store_true",
                           help="Request verified Codex tool-boundary notifications alongside idle delivery")
    configure.add_argument("--busy-context-actor", action="append", help="Limit busy context to this actor ID (repeatable)")
    add("daemon", "Run the local coordinator.").add_argument("--once", action="store_true")
    hook = add("hook", "Capture bounded native lifecycle events; does not enroll sessions.")
    hook.add_argument("provider", choices=("codex", "claude"))
    hook.add_argument("event")
    register = add("register", "Explicitly enroll this native session using its exact current title.")
    register.add_argument("--name")
    register.add_argument("--username-prefix", help=
                          "Optional readable task label for the Mattermost handle; explicit use can rename the same bot")
    register.add_argument("--sidechat", action="store_true",
                          help="Independent manual Codex enrollment; no inheritance or automatic wakeups")
    register.add_argument("--inherit", choices=("fresh", "pending", "pending-and-channels"),
                          help="First enrollment: fresh inbox (default), current direct-parent pending work, or pending work and its full channels. Retries resume the recorded choice.")
    agents = add("agents", "Find peers by name/ID with bounded results; names may repeat.",
                 epilog="Example: murmur agents --query 'Build review' --limit 5. Use returned actor IDs in dm/channel.")
    agents.add_argument("--query", default="", help="Case-insensitive name or actor-ID substring")
    agents.add_argument("--limit", type=bounded_number(1, 100), default=20, help="Maximum peers returned (1–100)")
    agents.add_argument("--after", help="next_after actor ID from the same scope/query; omit to restart")
    agents.add_argument("--scope", choices=("server", "local"), default="server",
                        help="Server catalogue or actors enrolled in this coordinator; neither means online")
    channels = add("channels", "Discover public managed channels without joining, tracking or consuming messages.")
    channels.add_argument("--query", default="", help="Case-insensitive name, purpose or ID substring")
    channels.add_argument("--limit", type=bounded_number(1, 100), default=20)
    channels.add_argument("--after", help="next_after ID from the same query; omit to restart")
    chats = add("chats", "List participating/reference conversations; --discover browses managed conversations without joining.")
    chats.add_argument("--discover", action="store_true", help="Browse the server's managed conversation directory; no inbox tracking")
    chats.add_argument("--query", help="Discovery name, purpose or ID substring")
    chats.add_argument("--kind", choices=("dm", "channel"), help="Discovery conversation kind")
    chats.add_argument("--agent", help="Discovery participant actor ID from murmur agents")
    chats.add_argument("--limit", type=bounded_number(1, 100), help="Discovery page size (1–100; default 20)")
    chats.add_argument("--after", help="Opaque next_after from the same discovery filters")
    doctor = add("doctor", "Show per-route support, configuration gates and actionable recovery state; not proof of admission.")
    doctor.add_argument("--notifications", action="store_true",
                        help="Inspect only this session's recorded notification evidence, offline and read-only")
    doctor.add_argument("--token", help="Inspect one exact own-session token; requires --notifications")
    doctor.add_argument("--limit", type=bounded_number(1, 100), default=None,
                        help="Oldest outstanding attempts returned (default 20); requires --notifications")
    add("inbox", "List unread/saved conversations without consuming messages; acknowledge outstanding doorbells.").add_argument("--starred", action="store_true")
    add("seen", "Acknowledge this session's exact doorbell; preserve messages, read state and native admission evidence.").add_argument("token")
    show = add("show", "Reopen an exact post without reading or unstarring it.")
    show.add_argument("post_id")
    show.add_argument("--context", type=bounded_number(1, 200), metavar="N",
                      help="Include latest N thread posts plus explicit root/anchor, with next_before; does not consume")
    show.add_argument("--before", help="Page context before this post in the same thread; requires --context")
    for name, summary in (("star", "Keep this exact post visible in the inbox after reading; quiet and per-session."),
                          ("unstar", "Explicitly remove this session's saved marker; preserves read/notification state.")):
        add(name, summary).add_argument("post_id")
    for name in ("read", "history"):
        summary = ("Read one exact unread post with --post, or oldest unread messages in CHAT; consume only successfully emitted IDs."
                   if name == "read" else "Return recent canonical messages without consumption; pages are chronological and include next_before.")
        command = add(name, summary, epilog="Use a genuine root ID with --thread. To reply to any post, use murmur reply POST_ID TEXT.")
        command.add_argument("chat", **({"nargs": "?"} if name == "read" else {}),
                             help="Conversation ID from inbox/chats")
        command.add_argument("--thread", help="Genuine root ID in this conversation, not a reply ID")
        high = 1000 if name == "read" else 200
        command.add_argument("--limit", type=bounded_number(1, high), default=None if name == "read" else 20,
                             help=f"Maximum returned posts (1–{high}); default 20")
        if name == "read":
            command.add_argument("--post", help="Consume only this exact tracked unread post; omit CHAT, --thread and --limit")
        if name == "history":
            command.add_argument("--before", help="next_before post ID from this conversation/selected thread")
    threads = add("threads", "List compact thread summaries by recent activity without consuming messages.")
    threads.add_argument("chat")
    threads.add_argument("--limit", type=bounded_number(1, 100), default=20, help="Maximum summaries (1–100)")
    threads.add_argument("--before", help="next_before root ID from this conversation")
    notify = add("notify", "Choose future notifications for a participating conversation: all, followed threads or off.")
    notify.add_argument("chat")
    notify.add_argument("mode", choices=("all", "threads", "off"))
    for name, summary in (("follow", "Follow this post's canonical thread for future arrivals; requires full participation."),
                          ("unfollow", "Stop following this post's thread; explicit unfollow survives later replies.")):
        add(name, summary).add_argument("post_id")
    add("following", "List this session's followed threads without reading or consuming messages.").add_argument("chat", nargs="?")
    for name, summary in (("send", "Post a new discussion, or a reply with --thread; does not consume or unstar."),
                          ("reply", "Reply to an exact root or reply post; derive its real conversation/root and use normal send recovery.")):
        command = add(name, summary,
                      epilog=f"Text: murmur {name} ID --text-file message.txt (or - for stdin). "
                             f"Attachments: murmur {name} ID 'Please review' --file image.png --file notes.pdf; "
                             "omit text for an attachment-only message.")
        command.add_argument("chat" if name == "send" else "post_id")
        command.add_argument("text", nargs="?", help="Literal message, at most 16,000 characters")
        command.add_argument("--text-file", help="UTF-8 message text, or - for stdin; distinct from --file")
        command.add_argument("--file", dest="files", action="append", default=[], type=Path,
                             help="Attach a local file; repeat for multiple files")
        if name == "send":
            command.add_argument("--thread", help="Genuine root ID; omit to start a new discussion")
    download = add("download", "Download one file attached to this exact post; does not consume or announce the message.")
    download.add_argument("post_id")
    download.add_argument("file_id")
    download.add_argument("--output", required=True, type=Path, help="New local file path; never overwrites or opens it")
    dm = add("dm", "Open a DM using a peer actor ID from agents, or --operator for the configured human contact.")
    recipient = dm.add_mutually_exclusive_group(required=True)
    recipient.add_argument("peer", nargs="?",
                           help="Native peer actor ID from agents; names are not identities")
    recipient.add_argument("--operator", action="store_true", help="Use the server's configured human contact")
    channel = add("channel", "Create a channel using peer actor IDs from agents.")
    channel.add_argument("name")
    channel.add_argument("members", nargs="*")
    channel.add_argument("--purpose", default="", help="Optional channel purpose, at most 250 characters")
    for name, summary in (("join", "Become a full participant in an accessible channel; explicit mute choice survives."),
                          ("leave", "Leave this session's participation without altering other sessions."),
                          ("mute", "Suppress every wakeup from this conversation; messages remain unread."),
                          ("unmute", "Enable future arrivals; old muted backlog does not ring again.")):
        add(name, summary).add_argument("chat")
    operator = add("operator", "Explicit operator recovery for native attempts, causal metadata and held onboarding steps.")
    controls = operator.add_subparsers(dest="control", required=True)
    for action in ("resolve", "interrupt", "retire"):
        control = controls.add_parser(action)
        control.add_argument("actor")
        control.add_argument("--reason", required=True)
        if action == "interrupt":
            control.add_argument("--epoch", required=True)
            control.add_argument("--turn-key", required=True)
        if action == "retire":
            control.add_argument("--token", required=True)
    skip = controls.add_parser("onboarding-skip", help="Resolve one uncertain saved-copy step without saving it again.")
    skip.add_argument("actor")
    skip.add_argument("--plan-id", required=True)
    skip.add_argument("--step", required=True)
    skip.add_argument("--reason", required=True)
    result.send_parsers = {name: commands.choices[name] for name in ("send", "reply")}
    return result


def message_text(args):
    if args.text is not None and args.text_file is not None:
        raise ValueError("Choose literal message text or --text-file, not both")
    if args.text_file is None:
        text = args.text or ""
    elif args.text_file == "-":
        text = sys.stdin.read(16001)
    else:
        with Path(args.text_file).open(encoding="utf-8") as stream:
            text = stream.read(16001)
    if (not text.strip() and not getattr(args, "files", ())) or len(text) > 16000:
        raise ValueError("Message must contain 1–16,000 characters of nonblank text")
    return text if text.strip() else ""


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))
    sys.stdout.flush()


def main(argv=None):
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args[:1] == ["hook"] and {"--client", "--event", "--home"}.issubset(raw_args):
        # Already-running native sessions can retain retired hook commands.
        # They confer no enrollment, context, network work or legacy fallback.
        emit({})
        return
    arguments = parser()
    if raw_args[:1] and raw_args[0] in arguments.send_parsers:
        # File flags may appear before or after the optional literal message.
        args = arguments.send_parsers[raw_args[0]].parse_intermixed_args(raw_args[1:])
        args.command = raw_args[0]
    else:
        args = arguments.parse_args(raw_args)
    if args.command == "doctor":
        if not args.notifications and (args.token is not None or args.limit is not None):
            raise ValueError("doctor --token and --limit require --notifications")
        if args.notifications:
            from . import native
            from .diagnostics import notification_report
            identity = native.current_identity()
            with closing(Ledger.open_readonly(config.ledger_path())) as ledger:
                emit(notification_report(ledger, identity, token=args.token, limit=args.limit or 20))
            return
    if args.command in ("dm", "channel"):
        peers = [args.peer] if args.command == "dm" else args.members
        if any(peer is not None and not peer.startswith(("codex:", "claude:")) for peer in peers):
            raise ValueError("Use peer actor IDs from murmur agents --query NAME; names may repeat")
    if args.command == "chats" and not args.discover and any(
            getattr(args, field) is not None for field in ("query", "kind", "agent", "limit", "after")):
        raise ValueError("Discovery filters require chats --discover")
    if args.command == "register" and not args.sidechat and not args.name:
        raise ValueError("Normal registration requires --name; use --sidechat only for explicit manual enrollment")
    if args.command == "read":
        if args.post and (args.chat or args.thread or args.limit is not None):
            raise ValueError("read --post requires only a post ID; omit CHAT, --thread and --limit")
        if not args.post and not args.chat:
            raise ValueError("read requires CHAT or --post POST_ID")
    if args.command == "configure":
        if args.enable_busy_context and not args.enable_delivery:
            raise ValueError("Busy context requires delivery to be enabled")
        # Credentials come from a protected file, never the process argument list.
        token = args.token_file.read_text().strip()
        if not token or args.token_file.stat().st_mode & 0o077:
            raise ValueError("Token file must be nonempty and mode 600")
        config.save(dict(url=args.url, token=token, delivery_enabled=args.enable_delivery,
                         busy_context_enabled=args.enable_busy_context,
                         busy_context_actors=args.busy_context_actor))
        emit({"configured": True, "delivery_enabled": args.enable_delivery,
              "busy_context_enabled": args.enable_busy_context,
              "busy_context_actors": args.busy_context_actor})
        return
    if args.command == "daemon":
        from .daemon import run
        run(once=args.once)
        return
    if args.command == "hook":
        response = {}
        payload = {}
        ledger = None
        try:
            raw = sys.stdin.buffer.read(262145)
            if len(raw) > 262144:
                raise ValueError("Native hook payload exceeds 256 KiB")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Native hook payload must be an object")
            if config.ledger_path().exists() or (config.home() / "config.json").is_file():
                # Explicitly configuring this home permits passive creation
                # evidence capture before any session enrolls. No network here.
                ledger = Ledger(config.ledger_path(), timeout=0.2)
                response = handle(ledger, args.provider, args.event, payload)
            elif str(payload.get("prompt", "")).startswith(("You have new messages", "Murmur:")) and "[wake:" in str(payload.get("prompt", "")):
                response = {"decision": "block", "reason": "Murmur session is not enrolled; operational ledger is absent"}
        except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
            prompt = str(payload.get("prompt", "")) if isinstance(payload, dict) else ""
            if args.event == "UserPromptSubmit" and prompt.startswith(("You have new messages", "Murmur:")) and "[wake:" in prompt:
                response = {"decision": "block", "reason": "Murmur admission unavailable: " + str(error)}
        finally:
            if ledger:
                ledger.close()
        emit(response)
        return
    ledger = Ledger(config.ledger_path())
    initialize(ledger.db)
    try:
        if args.command == "operator":
            ledger.actor(args.actor)
            if not args.reason.strip():
                raise ValueError("Recovery requires an explicit reason")
            request = {k: v for k, v in vars(args).items() if k != "command"}
            record_error(ledger, args.actor, "operator_requested", json.dumps(request, sort_keys=True))
            if args.control == "resolve":
                ledger.resolve_causal(args.actor)
                changed = True
            elif args.control == "interrupt":
                changed = ledger.interrupt(args.actor, args.turn_key, args.epoch)
            elif args.control == "onboarding-skip":
                result = Coordinator(ledger, None).recover_onboarding(args.actor, args.plan_id, args.step, args.reason)
                emit(result)
                return
            else:
                attempt = ledger.attempt(args.token)
                if attempt["actor"] != args.actor:
                    raise ValueError("Notification belongs to another recipient")
                if attempt["status"] != "pending":
                    raise ValueError("Only a pending notification can be retired")
                ledger.retire_attempt(args.token, args.reason)
                changed = True
            emit({"actor": args.actor, "control": args.control, "changed": changed})
            return
        if args.command == "seen":
            from . import native
            actor = actor_key(native.current_identity())
            emit(ledger.acknowledge_notices(actor, [args.token])[0])
            return
        settings = config.load()
        coordinator = Coordinator(ledger, Backend(settings["url"], settings["token"]))
        from . import native
        identity = native.current_identity()
        actor = actor_key(identity)
        if args.command == "register":
            if args.sidechat:
                result = coordinator.register(identity, args.name, mode="manual", username_prefix=args.username_prefix,
                                              inherit=args.inherit)
                emit(result)
                return
            from .evidence import lineage_evidence, persist_proofs
            lineage_proof = lineage_evidence(ledger, identity)
            identity = native.resolve_identity(identity, lineage_proof=lineage_proof)
            proof = identity.evidence.get("parent_proof")
            if proof and "ancestors" in proof:
                persist_proofs(ledger, proof["ancestors"])
            result = coordinator.register(identity, args.name, username_prefix=args.username_prefix, inherit=args.inherit)
            emit(result)
            return
        if args.command == "doctor":
            from .diagnostics import report
            emit(report(coordinator, identity, settings))
            return
        ledger.actor(actor)  # Operational commands require explicit enrollment.
        if args.command == "agents":
            emit(coordinator.agents(args.query, args.limit, args.after, args.scope))
        elif args.command == "channels":
            emit(coordinator.channels(actor, args.query, args.limit, args.after))
        elif args.command in ("chats", "inbox"):
            if args.command == "inbox":
                notices = [] if args.starred else ledger.unseen_notice_tokens(actor)
                emit(coordinator.inbox(actor, starred_only=args.starred))
                if notices:
                    ledger.acknowledge_notices(actor, notices)
            elif args.discover:
                emit(coordinator.conversation_directory(actor, args.query or "", args.kind, args.agent,
                                                        args.limit or 20, args.after))
            else:
                coordinator.sync(actor)
                emit([{key: (bool(chat[key]) if key in ("muted", "mute_explicit") else chat[key])
                       for key in ("id", "name", "kind", "role", "muted", "mute_explicit", "notification_mode")}
                      for chat in ledger.conversations(actor)])
        elif args.command == "read":
            selection = coordinator.prepare_read(actor, args.chat, args.thread, args.limit or 20, post_id=args.post)
            emit({"posts": selection["posts"]})
            coordinator.ack_read(actor, selection)
        elif args.command == "history":
            emit(coordinator.history(actor, args.chat, args.thread, args.limit, args.before))
        elif args.command == "show":
            if args.before and args.context is None:
                raise ValueError("--before requires --context N")
            emit(coordinator.context(actor, args.post_id, args.context, args.before) if args.context is not None
                 else {"post": coordinator.show(actor, args.post_id)})
        elif args.command in ("star", "unstar"):
            emit(coordinator.set_saved(actor, args.post_id, args.command == "star"))
        elif args.command == "threads":
            emit(coordinator.threads(actor, args.chat, args.limit, args.before))
        elif args.command == "notify":
            emit(coordinator.notify(actor, args.chat, args.mode))
        elif args.command in ("follow", "unfollow"):
            emit(getattr(coordinator, args.command)(actor, args.post_id))
        elif args.command == "following":
            emit(coordinator.following(actor, args.chat))
        elif args.command in ("mute", "unmute"):
            coordinator.notify(actor, args.chat, "off" if args.command == "mute" else "all")
            emit({"id": args.chat, "muted": args.command == "mute"})
        elif args.command == "send":
            emit(coordinator.send(actor, args.chat, message_text(args), root=args.thread or "", files=args.files))
        elif args.command == "reply":
            emit(coordinator.reply(actor, args.post_id, message_text(args), files=args.files))
        elif args.command == "download":
            emit(coordinator.download(actor, args.post_id, args.file_id, args.output))
        elif args.command == "dm" and args.operator:
            emit(coordinator.mutate(actor, "operator_dm"))
        elif args.command in ("dm", "channel"):
            peers = [args.peer] if args.command == "dm" else args.members
            options = {} if args.command == "dm" else {"purpose": args.purpose}
            if options and len(args.purpose) > 250:
                raise ValueError("Channel purpose must contain at most 250 characters")
            emit(coordinator.mutate(actor, "conversation", kind="dm" if args.command == "dm" else "channel",
                                    name="" if args.command == "dm" else args.name, members=peers, **options))
        elif args.command in ("join", "leave"):
            coordinator.sync(actor)
            emit(coordinator.mutate(actor, args.command, channel_id=args.chat))
    finally:
        ledger.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        print("murmur: " + str(error), file=sys.stderr)
        if isinstance(error, sqlite3.Error) and "readonly" in str(error).lower():
            print("Use normal tool escalation for this exact installed Murmur subcommand; preserve the native environment.", file=sys.stderr)
        sys.exit(1)
