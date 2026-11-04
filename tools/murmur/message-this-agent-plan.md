# Message this agent: implementation handoff

The peer-reviewed plan is implemented. The operator selects an exact enrolled
agent, opens/adopts the actual human–bot DM, then writes through the separate
human post operation. Opening sends nothing. Imported canonical history remains
readable/unread but quiet; subsequent arrivals retain normal mute, manual-mode,
causal-budget and verified native-admission handling. Descendants retain their
normal inherited observer access.

Maintained entry points:

- [Operator workflow](README.md#human-conversation-browser): selection, opening/status
  recovery, drafts, posting and departed-agent history.
- [API and lifecycle contract](CONTRACT.md#human-operator-api-and-console): exact
  snapshot membership, durable operation recovery, coherent versioned feed,
  atomic local ingestion and rollout/repair ordering.
- [Server setup](server/README.md): container build, local operation and optional
  LAN access.
- [Operating skill](../../skills/murmur/SKILL.md): selective reading of fresh
  work amid quiet history; registration remains explicit opt-in.

Server and Python changes were cross-reviewed with the CLI and sidechat peers;
UI recovery/departure changes received a separate CLI review. Regression coverage
lives in `server/operator_dm_test.go`, `tests/test_attention_feed.py`,
`tests/test_operator_dm_runtime.py` and `webapp/console.test.js`. Real-server
fixtures use private ledgers and fake native transport; actual desktop/CLI
admission needs its own native evidence. Queue acceptance alone is insufficient.

The original reviewed plan is preserved in Git commit `e8f8453`. Automatic
adoption of arbitrary human DMs, forced wake/rejoin, MCP, fixture filtering and
root-output deduplication remain outside this feature.
