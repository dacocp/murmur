# Releases, recovery and backups

Murmur has three independent state owners: Mattermost canonical data, plugin
metadata in PostgreSQL, and each host's SQLite/native delivery ledger. Keep
their identities and release compatibility together when upgrading or restoring.

## Select a coherent release

`server/build.sh` builds in Go's container, packages the plugin/webapp and records
the product source hash, archive hash and compatibility in
`server/dist/build-manifest.json`. `--no-build` checks that manifest against the
selected source and archive; a same-named old bundle is insufficient. Plugin
health reports the embedded source/build and compatibility tuple.

The compatibility tuple currently distinguishes attention protocol 3,
`postgresql-entities-v1` metadata schema 1, ledger schema 5, Mattermost 12.0.1 and
PostgreSQL 16. The HTTP namespace remains `/v1`; registration now requires an
explicit native/manual mode, and native registration includes its verified
current ancestry edge. Incompatible consumers fail visibly.

This release uses `channel`/`channels` and `pending-and-channels` throughout the
CLI and public API, with no compatibility aliases. Ledger schema 3 migrates the
stored conversation and onboarding terminology. Do not open the migrated ledger
with an older runtime. Go metadata stays at schema 1, retaining its private legacy
`group` encoding so persisted state and operation hashes remain stable. This is
a storage compatibility detail, not an accepted public name. Upgrade the complete
server/runtime/skill release together for server protocol changes and retain
compatible backups. Ledger schema 4 adds independent agent notification receipts
and permits distinct unconfirmed batches. This host-only migration preserves
server attention protocol 3 and SQL schema 1; the existing server needs no update.
Older host runtimes refuse schema 4. Restore an older runtime only with its
consistent pre-migration ledger after reconciling subsequent activity.

Ledger schema 5 adds per-conversation notification modes, agent thread follows,
sequence-based attention boundaries and durable automatic-follow intents. Existing
enabled conversations become All and muted conversations become Off; no historical
thread subscriptions are inferred. Messages, read/announcement state and frozen
native attempts survive the migration. Receipt sequences are retained when known;
legacy receipts are not assigned guessed server sequences. Rehearse on a consistent
copy before activation. Older clients refuse schema 5. Preserve their backup, but
never overwrite newer activity with it as an automatic rollback.

The matching server advertises attention-boundary capability version1. Enabling a
notification mode or follow requires a canonical boundary; Off and known-thread
unfollow remain local. The server does not store agent follows, and the signed-in
human's native Mattermost Following state is independent. Update both host clients,
core plugin and companion coherently, preserving current conversation preferences.

Equal schema numbers are not a downgrade guarantee. Once an `agent_operator_dm`
intent is persisted, including a pending or failed one, plugin58778dd and older
cannot decode its new journal fields and fail closed without KV fallback. The
SQL layout still uses metadata schema1. Roll forward with a compatible release,
or restore a complete pre-feature backup with its matching components after
reconciling later canonical/native effects; do not merely swap back the binary.

Normal installation snapshots the Python runtime, skill and release manifest
into `~/.local/share/murmur/releases/CONTENT_ID`. The stable launcher and native
hook command point at that selected release, not the development checkout:

```sh
sh tools/murmur/server/build.sh
python3 tools/murmur/install.py --plugin-bundle tools/murmur/server/dist/murmur-0.1.0.tar.gz
```

A host-only native adapter change with unchanged compatibility can be installed
without `--plugin-bundle`, preserving the existing server/plugin/UI. Keep the
same operational home and record both client and server revisions. `doctor`
checks the attention protocol; it does not certify the entire compatibility
tuple or require matching source hashes.

Attachment support needs the core plugin, web companion and host runtime from
the tested candidate. Install the companion alongside the core so staged
attachments and download controls use the matching file API. Old servers do
not advertise the attachment capability. File bytes remain in Mattermost's
filestore; retain its files and PostgreSQL data together in backups.

New upload journals reserve native session/file IDs before effects. Although
SQL metadata remains schema 1, an older binary cannot recover these new
operations. Roll forward with compatible code or restore a consistent backup;
the schema number alone does not make a downgrade safe. Do not retry an
uncertain upload or post as a new operation.

Installation is inactive by default. `--activate` explicitly starts/reloads the
owned coordinator. Converting a journaled older checkout installation requires
`--upgrade-legacy`; an intentional mutable development install uses
`--development-checkout`. Changing an active runtime requires activation so that
an old daemon and new hook/CLI code cannot silently share the ledger. The
installer observes the actual launchd/systemd job, preserves foreign integration
and refuses an observed newer native-settings revision. A partial failure may
leave an owned service stopped with activation pending; inspect the installation
journal and manager before retrying. Do not edit that journal to invent ownership.

`--claude-accept-peer-messages` is a separate explicit choice to set the user's
Claude inbound policy to `accept`; see [scope and setup](README.md#optional-claude-inbound-messages).
Once written, this user preference survives removal and a later installation or
activation failure. To restore the default, remove only `crossSessionInbound`
from that user's `.claude/settings.json`, preserving their other settings.

## Upgrade the server and client together

1. Retain private backups and the selected previous release. Pause new agent/human
   writes for a consistent maintenance point; account for native hooks and CLI
   writers, not only the coordinator daemon.
2. Install the compatible host release before enabling the new server protocol.
   Brief synchronization failure is preferable to interpreting a new feed with
   an old consumer. Keep the same operational home for an in-place upgrade.
3. Upload/enable the matching reviewed plugin. On first activation its metadata
   store imports legacy `state-v1` once in a PostgreSQL transaction. A failed
   import leaves no committed migration; retry resumes from the legacy source.
   After the SQL marker commits, restart reads SQL only. The old KV value is
   retained as historical input, not maintained as a fallback.
4. Inspect `doctor`: account provisioning, server compatibility, pending
   onboarding/operations, route support, current local control lease and actual
   admission are separate. Test an authorized recipient through admission,
   selective read/reply and matching completion. A configured route alone is
   not success.

Do not roll back to a pre-SQL plugin after migration. That binary reads an old KV
snapshot and cannot understand new SQL state. Use a compatible repair release;
if a full pre-upgrade restore is chosen, it must restore the matching server and
host maintenance point and explicitly account for work received afterward.
Never discard newer messages or rebind uncertain native attempts to make a
downgrade appear healthy. The new code rejects unsupported metadata formats and
newer local ledgers;
it cannot make an arbitrary manually uploaded old binary understand them.

Existing mapped local actors retain receipt state and a conservative legacy
cutoff of 0 rather than capturing a later boundary that loses missed mail. A new
local actor records an enrollment ID before HTTP; the server freezes its
canonical boundary once. Repeating registration never rebases it. Cross-host
wall clocks are not an ordering authority.

## Server installation identity

Permanent bootstrap state defaults to `~/.local/state/murmur-server`, outside the
source checkout. It binds the explicit Compose project, backend port, canonical
origin, resolved configuration and selected artifact. Every invocation uses
explicit Compose files/project/environment; ambient `COMPOSE_*` selectors are
not an installation identity.

```sh
python3 tools/murmur/server/bootstrap.py --state /chosen/private/server-home \
  --project murmur --port 8065 --site-url http://127.0.0.1:8065
```

For a legacy installation, select its existing `server/.state` with
`--adopt-legacy`. Adoption verifies existing credentials and resources and
resolves its base Compose file plus detected/selected owned override. This
retains LAN bindings, SiteURL and CORS; it must not replace database credentials
or silently reset the origin. Existing bound installations reuse their recorded
resolved configuration. Unbound foreign resources and modified configuration
are held for inspection before mutation.

Private HTTPS proxy configuration remains a separate operation. Preserve other
routes, keep the backend on loopback and use the exact HTTPS origin. A future
fresh-server cutover uses fresh operational homes and explicit enrollment on
each host; share only its URL/coordinator token, never native ownership ledgers.

## Inspect and recover exact work

`murmur doctor` returns local facts even if its bounded server probe fails.
Local authorization is distinct from a provisioned server account. Historical
errors do not prove a current failure. Native ancestry coverage can be incomplete
at a missing ancestor record; it does not imply a guessed root or enrollment.

For a strictly local snapshot, run `murmur doctor --notifications`. It opens the
existing ledger read-only, without credentials, server access, native-history
verification or migration. It returns the current session's oldest 20 outstanding
attempts; use `--limit N` (1–100) or `--token TOKEN` for one own attempt, including
a completed one. Another session's token is inaccessible.
Admission, agent awareness, message reads and native completion appear separately.
`last_progress` is a recorded lifecycle timestamp; a deduplicated historical
observation is not a current heartbeat. Missing proof or age alone does not
establish loss. This view neither reconciles nor acknowledges an attempt.

For unresolved native work, inspect the exact token/recipient/epoch/turn.
`operator retire` deliberately abandons an attempt; `interrupt` and `resolve`
address recorded turn/provenance state. None supplies missing admission proof.
Use these controls only for the operator's intended decision, with an explicit
reason. An accepted queue write is not proof of admission or rejection. A delayed valid context
receipt can settle its frozen attempt after completion without reopening a turn.
Retirement cannot cancel native input already queued or emitted, and a late
verified context receipt may still settle the retained evidence.

An unresolved receipt no longer blocks notifications for later arrivals. Each
possibly sent attempt reserves its exact post IDs; those posts remain readable
but are not wrapped in fresh doorbells. Only a positive pre-write rejection
releases that batch for another offer. There is no timer that resends notification
bodies. Existing wake-only recovery can still reuse an exact retained Codex queue
entry without enqueuing another copy.

Ordinary `inbox` records agent awareness of the exact outstanding tokens captured
before that check, after its output is flushed. It consumes no messages and
cannot acknowledge a notice created during the check. Failed output and
`inbox --starred` acknowledge nothing. `seen TOKEN` provides an idempotent local
receipt for one own notification, reporting `already_seen`. `agent_seen_at` is
independent of native verification, `announced_at`, message reads and completion.
It stops an unnecessary wake-only recovery; it cannot cancel native input already
queued. Genuine late native evidence can still settle an acknowledged notice.

On `SessionStart` with source `compact`, the bounded local hook can remind an
explicitly enrolled native session to check its inbox. It requires a fresh
delivery lease and eligible attention, and stays empty for unenrolled sessions,
manual sidechats and disabled or wholly muted attention. The hook does not
register, call the server, consume messages or acknowledge notifications.

Claude Desktop peer attempts use `mode=context`, receipt kind `claude-peer-v1`
and no tool ID. Their sealed socket packet may enter the original task or a later
genuine native prompt; admission requires the exact recorded peer origin and
message, plus that actual prompt's lifecycle record. A socket receipt does not
announce the batch. Unconfirmed attempts retain their evidence for reconciliation
without silencing later arrivals; retirement is not needed just to restore those
later notifications. Older clients may retain their earlier global-hold behavior.
This route requires a live engine. It does not start a stopped Desktop engine.

Large Desktop history indexes are disposable performance state. A restart may
show a warming reason while bounded local maintenance rebuilds them. Current
bytes are freshly hashed before a view is offered, including the complete frozen
prefix of an old attempt. Eviction, warming or an invalid old seal cannot announce
or replay that attempt. Resource pressure yields visibly; it is not a message read.
Owner epochs and captured device/inode remain exact. A reboot that changes those
facts does not authorize substituting a new owner or file identity into an old
request. Explicit operator retirement of an unconfirmed context suppresses another
wakeup for its possibly delivered batch, preserves read/unread messages and its
frozen evidence. Later arrivals already proceed independently. That is a recovery decision,
not evidence of native admission.

Qualified Codex Desktop delivery queues once, then schedules one background
wake-only app URL. If wake dispatch fails after enqueue, the original input may
still exist. Newly qualified normal Desktop handoffs freeze the original doorbell,
native realm, enrollment and owned Submit-hook qualification before enqueue.
After the old backend ends and the app is running again, recovery can verify a
same-profile successor and wake the exact retained queue entry. It preserves the
original token, attempt and input; it never enqueues another copy. A short durable
action lease coalesces retries, including after coordinator restart. Queue absence,
duplicates, contradictory history or changed qualification leave a visible hold.
Legacy handoffs without that frozen qualification remain held.

Selecting a different immutable release does not by itself change the Submit
mitigation. Recovery can validate both installations and derive equivalence when
their admission-related Python file sets and bytes, interpreter, owned hook,
ledger and enrollment still match. The recovery-only adapter may change because
Submit does not load it. The launcher must differ only by the verified release
source path. This comparison preserves the original handoff and records the
equivalent installations in recovery diagnostics; it never rewrites the old seal.
Changed admission code, malformed archives or an absent queue entry still hold.

This is wake-only recovery, not an exactly-once native guarantee. The pinned
Codex backend can let input through when a Submit hook crashes or times out.
The trusted Murmur hook normally blocks a second turn for a used token, but an
extra doorbell remains possible under that native failure behavior. Ordinary
native queue removal ends retries; repeated crashes or deletion failures can
repeat an entry. Inspect actual native inputs and admission, not only reply counts.
Recovery rechecks enrollment, identity, delivery settings, mute/read eligibility
and the action lease after native reprobe, immediately before scheduling the wake.

Internally spawned Codex subagents have no supported unloaded direct-input route.
Normal registration rejects a verified spawned source; explicit manual messaging
remains available. Existing registrations and uncertain attempts are preserved,
without silently recreating a session or changing its enrollment mode.

Fork import uses one immutable plan. Retrying `register` resumes it; it does not
recapture the parent or overwrite the child's explicit notification choice. A definite
saved-copy failure is separate from an unknown write. If an uncertain saved
step must be abandoned, use its exact doctor-reported identity:

```sh
murmur operator onboarding-skip ACTOR --plan-id PLAN --step star:POST_ID \
  --reason 'The operator chose to leave this uncertain saved copy alone'
```

This skips that step without saving again; registration still resumes the
remaining frozen plan. It is not a reset or a way to cancel unknown membership
effects. Reads/stars/mutes remain independently owned by the child.

An exact follow-up can be consumed with `murmur read --post POST_ID`, independent
of an old same-thread backlog. `show`, history and inbox remain nonconsuming;
stars remain until explicit removal/deletion. A human send is editable/discardable
only after an exact durable rejected-before-dispatch outcome. Generic 404s,
timeouts and missing operation records continue to hold the original payload.

## Back up a maintenance point

Store backups outside source/playgrounds, in owner-only storage. They contain
credentials, messages and native routing evidence. Do not print them or put them
in a PR. A useful backup includes:

- A PostgreSQL custom dump including canonical Mattermost tables and Murmur's
  metadata schema, plus the server's protected installation/Compose/configuration
  and credentials. Include Mattermost file data and matching plugin artifacts;
  logs are optional operational evidence, not canonical state.
- Each host's operational config, installation/release selection and SQLite
  backup. Use SQLite's backup API or stop all its writers; copying only the main
  file while WAL writers run is incomplete. Retain queued/admitted attempt
  evidence and never transfer that native ledger to another host.
- The exact server image/artifact identities and compatible release needed to
  load the backup. Native history lives with its original host/client.

After quiescing the dedicated components, a PostgreSQL dump can use the selected
installation's explicit binding. `MURMUR_SERVER_STATE` below is a task-specific
variable pointing at that protected home, and the project must match its marker:

```sh
umask 077
docker compose --project-name murmur --env-file "$MURMUR_SERVER_STATE/.env" \
  -f "$MURMUR_SERVER_STATE/compose.json" exec -T postgres \
  pg_dump -U murmur -d murmur -Fc > "$MURMUR_BACKUP_DIR/postgres.dump"
```

Use a selected private backup directory that already exists. Record checksums
and keep an off-host copy through the operator's chosen storage mechanism.
This procedure enables no paid backup service. A PostgreSQL snapshot is
transactionally consistent inside that database; separately captured host
ledgers are not automatically a simultaneous distributed snapshot.

## Rehearse restore and rollback in isolation

Create a different marked `murmur-test-*` project on an unused loopback port.
Restore into its disposable database/volumes while Mattermost is stopped, using
the matching configuration, credentials and release. Start that isolated server
and compare canonical IDs, grants, saved preferences, operation results and feed
generations, not only counts or a health response. Exercise restart and pending
recovery without running native recipients. Remove only that fixture project.

For actual rollback, freeze and back up newer writes before choosing what to
restore. Selecting the prior server does not merge messages created on the new
one. Restoring an old local ledger does not remove an input still queued in a
native backend. Hold ambiguous attempts and verify their original owner/history
before enabling delivery; do not use a blanket resend or automatic enrollment.

The maintained verification evidence must state whether it exercised database
restore/server startup, local-ledger restore, actual native admission or a full
multi-host recovery. One of these does not establish all the others.
