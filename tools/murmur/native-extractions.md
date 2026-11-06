# Native adapter extractions

The native package is independent of Mattermost, enrollment, SQLite, read state
and causal budgets. It returns evidence and transport outcomes; the coordinator
owns all state transitions. No file-based runtime is installed or used.

## Reviewed source

Selected reference: `codex/murmur-v1` through `6ec7821` (its native code is
unchanged by the later `ddc8fd3` skill-only commit).

| New module | Selected source and reason |
| --- | --- |
| `native/codex.py` | Exact current-thread checks from `native.py`; only bounded initialize/read metadata RPC from `native_probe.py`; supported queue CLI operation from `notifier.py`. No thread resume, Store, or scheduler. |
| `native/process.py` | macOS argc-delimited process/executable inspection from `claude_process.py`. It excludes environment values and brackets identity with owner/birth checks. |
| `native/claude.py` | Invoking registry/process checks, Desktop mirror agreement and selected socket/receipt operations. No copied-host fallback or native database writes. |
| `native/lineage.py` | Observed startup and native `/branch` proof rules from `cli_lineage.py`, rebuilt as pure bounded certification over supplied observations/proofs. No old schema or replay database. |
| `native/hooks.py` | Exact native-key and subagent-exclusion requirements, implemented for the new concise doorbell. No old lifecycle/enrollment handler. |

Tests select negative identity, process, proof and transport scenarios. They use
temporary native-shaped files, fake executables and local synthetic sockets.
They are new implementation evidence, not the old runtime's live test results.

## Linux profiles

The Linux process adapter reads bounded `stat`, `status`, `cmdline`, `exe` and
PID-namespace entries from a pinned `/proc/PID` directory. It excludes environment
values, verifies the UID fields agree, brackets inspection with process-birth and
executable/argument checks, and rejects replaced executables and foreign namespaces.

The measured Linux x86_64 Claude CLI profile is 2.1.311, SHA-256
`86dc1fcc69498159eaf15f446613e847010f9036bfbbdffd4c49350be8489ada`.
Its native registry uses raw `/proc/PID/stat` field 22 as a string for `procStart`,
and `linux:<machine-id>:pid:[namespace-id]` for `pidDomain`. Creation certificates
and target epochs retain this domain; they cannot adopt macOS certificates.
The macOS 2.1.310 profile and legacy proof shapes remain unchanged. Linux Claude
Desktop is unsupported. The Linux 2.1.311 CLI also supports a measured
`PostToolUse` context route. macOS CLI profiles retain idle delivery; the measured
Desktop peer route is separate.

Claude context receipts freeze the exact native prompt/tool and file prefix
before emission. Verification requires a newly appended main-chain
`hook_additional_context` attachment, exact rendered notification/token,
recipient/version/tool identifiers, and ancestry through that tool's successful
result to the original prompt. Native tool-result rows may repeat the current
`promptId`; they are distinct from a new user prompt with `promptSource` metadata.
A different prompt/result, quoted text, missing provenance, duplicate UUID,
replaced file, changed prefix or unsafe file mode cannot establish admission.
The receipt format pins its own version/hash separately from identity support;
upgrading a CLI identity profile cannot silently certify a new busy format.

Linux Codex 0.164.0 was measured with SHA-256
`b93f6811d3f653f0fb83be4050d857fb9abbb111dfa2ce160563327daff7f306`.
Its Linux busy-context profile was exercised independently: a genuine
`PostToolUse` inserted one developer message with harness provenance and
`hooks.additional_context` into the original native turn; the CLI consumed only
the fresh thread, replied once, preserved the original tool output, and completed
that turn. The version string alone never inherits another build's certification.
Idle queue admission remains a separate proof.

The macOS Codex 0.166 profiles pin these backend bytes:

| Engine | SHA-256 |
|---|---|
| 0.166.0-alpha.5.1 | `58a91f6ea19bea477ae3a2ec48d619f0039afe75d3476260747e11d201fa7cea` |
| 0.166.0-alpha.6 | `508388df35319042107560d095b271a98a512f59a542848c908b23ed1bad4b44` |

Their resumed-history compatibility includes only the measured alpha11 creation
header pair. Desktop wake checks select a complete application profile, including
version, build, app, archive, backend and launcher hashes; profiles cannot mix.
The profiles include ChatGPT 26.1023.51421/build22530, 26.1027.30824/build22943,
26.1027.51404/build23880, 26.1027.61615/build24141 and 26.1029.61003/build24614.
Build 24614 changes the backend to alpha6 while preserving the strict wake-only
URL and nonempty-queue resume contract. Fresh recipient preparation validates
the creation header against the actual verified backend. Retained-input recovery
still requires the exact original Desktop profile; a new engine or changed
protected runtime never retroactively qualifies an old handoff. Capture,
admission, unloaded wake and restart require their separate native evidence.

## Claude Desktop peer route

The macOS Desktop peer profiles pin these engine bytes:

| Engine | SHA-256 |
|---|---|
| 2.1.308 | `8cea5edf53fbb5a521d42f5fa7015288733e41855fbb79bcb48cbce976ed6064` |
| 2.1.309 | `40fa5522dc98b3b85d7122c1159b900891107ef6c198e3c8d0fd222586c13e31` |
| 2.1.317 | `577c8ac6411ef57dd741e3c491e89f57ff6f42bb835a5ac5d4f496c29f14c5d5` |

Genuine native hooks capture the transcript path, engine version and owner
generation. Recovery cannot qualify a replacement owner using earlier capture
facts; a genuine hook must refresh them. Outside
hooks, the coordinator verifies the actual executable, host/engine mapping,
process generation, socket and active prompt. A version string alone does not
qualify another engine. No additional `PostToolUse` hook is installed.

Before the single possible socket write, the ledger freezes the current unread
batch, native history prefix and exact `priority: next` peer frame. Admission
requires a newly recorded peer input with that message ID, sender and doorbell,
intact history continuity and its actual prompt ancestry. A string echo or socket
receipt is insufficient. The expected prompt stays recorded even if the packet
lands after Stop in a later genuine prompt. Only that actual prompt's lifecycle
can settle completion. On 2.1.317, ordinary `next` input is absorbed after the
tool batch and its hooks, then query execution continues in the same prompt.
The measured two-tool receipt uses exact shared response/request IDs and each
result's source-tool pointer when those results occupy sibling history branches.
The coordinator has no qualified automatic startup route
for a stopped Claude engine.

The 2.1.309 and 2.1.317 resolvers also recognize the exact pre-capture automatic,
precomputed compaction shape with a paired preserved segment and display-only
summary. Boundary, manifest order, parent links and the preserved current prompt
remain required; a summary timestamp cannot select an older human prompt over a
preserved peer prompt. The 317 extension uses its pinned writer and a redacted
genuine structural sample, including non-main metadata between tail and boundary.
Manual, non-precomputed, nested and incomplete-manifest shapes remain unsupported.
This structural evidence and synthetic receipt tests are separate from a fresh
live automatic-compaction delivery result.

Desktop peer history uses an ephemeral compact offset index and streamed full-prefix
SHA256 checks. Cold parsing advances in bounded coordinator maintenance steps, not
hooks. Lazy node reads and final file validation share one stable descriptor/stat
window; a scoped captured-frontier view cannot borrow later records. The cache
has a 384 MiB accounting budget including builders, pinned readers and 128 MiB
scratch per lease; this is not a process-RSS guarantee. Per-record/depth bounds
remain 2 MiB/128, with at most one million conversation UUIDs and two million
complete records per index. Cache loss delays proof; it never retires a handoff.

The 2.1.309 receipt verifier also recognizes the measured adjacent two-tool
response: distinct tool results must name their exact source assistant/tool IDs,
and both frozen assistant chunks must share message and request IDs. This does
not permit arbitrary siblings or relax either prewrite descent check. Stop,
compaction, file identity and exact native-packet checks still apply.

Native history must pass the existing ownership and mode checks. On the measured
Ubuntu host, inherited umask 0002 created Codex JSONL files as 0664, correctly
rejecting busy context. The successful fresh fixture started its native backend
and frontend with umask 0077 and created private history. Set the umask on the
process that creates history, including an app-server backend; setting it only
on a frontend attached to an existing daemon cannot repair that daemon's policy.
The implementation never rewrites native history or relaxes the file checks.

## Coordinator boundary

`NativeIdentity.key`/`.actor` is `provider:session_id`. Providers are `codex` and
`claude`; Claude Desktop IDs are `local_UUID`, while standalone IDs are UUIDs.
`parent_known=False` means unresolved, not a root. Explicit registration must
resolve ancestry before server provisioning. Fast current identity/hook capture
performs no remote provisioning or metadata RPC.

`parse_hook(provider, event, payload, env=...)` returns `HookEvent` or `None`
for a native subagent. Events normalize to `session_start`, `submit`, `complete`,
`interrupt`, `session_end` and Codex `post_tool`. Actual turn events carry their
native key; `post_tool` additionally carries the actual tool ID and local
transcript path. SessionEnd never invents a key. No hook result enrolls an agent.

`prepare_context_receipt(captured)` locally certifies the invoking Codex backend
and freezes a bounded canonical JSONL baseline before context emission. It records
the actual starting ordinal, which can be nonzero in a fork, and verifies exact
continuity. Older schema-1 captures without that field retain their certified
zero base; a nonzero header cannot use that compatibility default.
`verify_context_receipt(identity, attempt, timeout=0.5)` returns `accepted` with
evidence or `unknown` with a reason. It checks the exact appended developer
message, hook classification, recipient, turn, token and immutable baseline;
SQLite UI projections are insufficient. The coordinator owns claims, frozen
context, receipt verification and announcement. Delayed verification can settle
an ended turn without requiring its former process to remain alive.

`native/context_receipts.py` is certified to exact measured backend hashes for
0.163.0-alpha.10.2, 0.164.0, 0.164.0-alpha.11.1, 0.165.0-alpha.1.2 and
0.166.0-alpha.5.1 and 0.166.0-alpha.6. The immutable header records creation,
independently of the current append owner. The exact 0.166.0-alpha.6 hash accepts
supported historical header structure, with creator version retained as printable
metadata of 1–128 characters with no surrounding whitespace. Both recipient IDs,
paginated mode and the starting ordinal remain mandatory. The creator label never
certifies an executing engine. Earlier profiles retain their measured
creation-version pairs. Native resume and append compatibility need their own
actual admission evidence; idle preparation inspects only the header, while busy
capture validates and freezes the full baseline. Modern append profiles require strict harness-hook provenance;
only the exact legacy 0.163 profile retains its earlier
provenance compatibility. Existing receipts retain their frozen append profile.
The 256 MiB source bound, independent 8 MiB JSONL record budget, one-second
capture deadline and half-second verification deadline fail visibly. These are
reader resource budgets, not native format maxima. Full baseline and compaction
records retain strict JSON and ordinal validation; nested replacement history
cannot supply a receipt. Receipt means native history acceptance, not model
sampling, inbox reading or power-loss durability. Unsupported versions,
quotations, inherited history, replacement/truncation and uncertain evidence
cannot announce mail. Context-only delivery never blocks the original task.

Claude CLI creation observations are captured before the registry may switch:

```text
{payload: {hook_event_name, session_id, source/reason, prompt_id?, transcript_path?},
 process: {pid, proc_start, entrypoint, version, root_start_verified,
           config_dir, home_dir}, event_key: SHA256}
```

The coordinator durably stores ordered observations and previously certified
proofs. Pass `lineage_proof={observations: [...], known: [...]}` to
`resolve_identity`. Its `evidence.parent_proof.ancestors` is a root-first list
of immutable certified records to retain individually for later resolution and
cold resume, alongside the target proof. Keep ordered unresolved and failed
observations as well; do not silently discard them with a latest-N query.
An unobserved session, unsupported version/profile or missing creation evidence
remains visibly unresolved. Transcript similarity never establishes ancestry.

`verify_target` validates recorded routing/generation evidence without discovering
a replacement process. Codex retains its routing epoch separately from captured
Desktop process facts. Its owner-state check can prove that the captured backend
ended; app-only exit with a surviving backend is insufficient. Claude uses the
engine/PID/process-birth epoch. Custom Codex profiles/backends need explicit route
validation.

`prepare_idle_target` is a separate preflight for a new idle attempt. Claude must
have an explicit idle status and a unique, verified live owner and socket. If the
recorded owner has positively ended, bounded registry discovery may recover an
already-running replacement in the same config and PID domain. It preserves the
certified identity and ancestry evidence: CLI engine UUIDs must agree; Desktop
host, engine and direct-parent mirrors must agree. A changed Desktop engine needs
fresh native identity capture. `recover_idle_target` exposes only this ended-owner
case. Neither preflight API starts or resumes a native runtime. Codex new-idle
preflight also permits a uniquely verified successor after positive backend exit,
within the same default native domain and qualified profile. It checks normal
persisted thread source, native cwd and creation history; unsupported spawned
subagents cannot acquire an automatic route. Only evidence is refreshed, under
the ledger's identity/enrollment comparison; frozen attempts remain unchanged.

The new `native/desktop_wake.py` adapter uses the vendor's wake-only
`codex://threads/wake/UUID` route after successful enqueue. Its background app
request resumes the existing queued input through Desktop's own local backend;
it supplies no prompt and requests no navigation. Current-client facts live in
identity evidence, not queue target or native epoch. Native hooks may refresh
only evidence using a stored-identity comparison, leaving frozen work unchanged.
An explicit absent capture clears stale Desktop evidence on a CLI invocation.
Successor discovery checks direct parentage, supported startup arguments and
actual native-database FD device/inode identities. Socket ownership uses a
bounded connection with zero protocol bytes and kernel `LOCAL_PEERPID`, bracketed
by process birth and filesystem checks. No app-tools command or UI request is sent.

Qualified idle handoffs freeze their text and current owned synchronous Submit
hook, runtime, ledger and enrollment before the first possible enqueue. Pending
restart recovery verifies the exact retained queue UUID and payload, unchanged
native realm and a verified successor, then schedules only a wake URL. It neither
adds nor deletes native input and never treats a queue row or wake response as
admission. The native adapter reprobes before the final ledger callback and wake;
the callback rejects stale claims, active turns and changed eligibility. Journal
leases survive coordinator restart without altering the original handoff fields.
An immutable release change is accepted only through a separately derived
installation-equivalence check. It validates the original archived seals and
current installation, exact launcher source-path substitution, and identical
relative Python file sets and hashes except for the recovery-only
`codex_queue_recovery.py` module. Codex Submit never imports that module; every
other Python module remains protected. Interpreter, ledger, enrollment and trusted
Submit hook remain exact. The final wake rederives the comparison, and compact
release/dependency digests are recorded through the existing recovery detail.
Missing or contradictory evidence leaves that original handoff unresolved,
without blocking a distinct new cohort. This policy accepts possible
duplicate doorbells when the vendor's hook subprocess fails open; the existing
Submit token gate is mitigation, not a universal native exactly-once contract.

After preflight, the coordinator compares the original stored identity and
rechecks enrollment, onboarding, active work and unoffered eligible arrivals
in one SQLite transaction. Only then does it refresh the route and freeze that
exact identity into a new attempt. A newer hook wins the comparison; a late read
or mute leaves no route update or empty attempt. Existing pending, admitted or
uncertain attempts retain their token and frozen recipient. A replacement process
does not prove that an earlier queued input ran or completed.

`owner_process_state` reports `ended`, `alive` or `unknown` for a recorded Claude
owner or qualified Codex Desktop backend. Only a missing PID or positively
different process birth proves that the old owner ended; a replacement registry
engine and inspection failures do not.
Codex still has no native routing epoch; routes without a verified Desktop owner
return `unknown`. The coordinator
must compare the evidence with the frozen old target and interrupt only the
exact old active turn, retaining causal, announcement and read state. This says
nothing about whether a queued, unadmitted input ran.

`format_doorbell` emits one `[wake:TOKEN]`. `deliver` returns `accepted`,
`rejected` or `uncertain`, with target evidence; it neither admits nor announces
a turn. The token remains visible. Existing evidence does not establish a
hidden transport-ID-to-Submit bridge or a successful hook prompt-rewrite path.

A generic nonzero queue exit after process launch is **uncertain**: enqueue can
persist before reporting fails. A timeout or partial socket write is likewise
uncertain. A Desktop wake failure after enqueue is also uncertain, with no text
replay or transport fallback after restart. Successful OS dispatch does not prove
model admission. Do not blindly retry. Socket receipts must match the exact outbound
message ID; queue acceptance and receipts never acknowledge a Murmur inbox.

Before activation, the new coordinator must pass actual four-client admission,
inbox-selection/reply/completion and recovery tests. Synthetic adapter tests do
not establish those live outcomes or arbitrary native-version compatibility.
