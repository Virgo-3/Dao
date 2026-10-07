# Runtime architecture

```mermaid
flowchart LR
    UI[Browser workspace] --> HTTP[Loopback HTTP + origin/CSRF checks]
    HTTP --> Runtime[Dao runtime + branch lock]
    Runtime --> Store[SQLite state + journal + usage]
    Runtime --> Decision[Scenario decision engine]
    Runtime --> Audit[Evidence adjudicator]
    Runtime --> Provider[Demo or Responses adapter]
    Provider --> Decision
```

The model generates conversational text and may request read-only decision evaluation. It does not own branch pointers, adjudication permissions, or accounting. The runtime alone commits snapshots and records model/tool attempts.

## Turn lifecycle

1. Validate input/context, acquire a branch lock, and check the expected head.
2. Commit the user message. A concurrent write through another process must still pass SQLite's optimistic compare-and-swap.
3. Execute an explicit local command or reserve a model request against the global lifetime token budget. Journal every admitted request before calling the provider.
4. Stream deltas. For a decision tool call, validate its input and preserve the result. A follow-up model request requires its own reservation. Three provider requests is the turn limit.
5. Finalize each admitted request exactly once using provider usage, or retain the reservation as an estimated unknown charge after failure. On browser disconnect, continue the bounded request to completion so usage is recorded.
6. Commit the complete assistant response or a labeled failed partial response. Failed partial responses remain inspectable and are excluded from subsequent model input.

Each provider request is limited to 4 MB of decoded body data, 50,000 SSE lines, and 2 MB per line. Empty text deltas are ignored, non-string deltas are rejected, and a running character counter bounds accumulated output without repeatedly scanning prior chunks. The reader stops at the first terminal response. It checks a 90-second elapsed deadline between decoded transport reads, with a separate 15-second socket-stall timeout; synchronous transport operations can delay a deadline check. Limit failures use the same conservative usage accounting as other interrupted streams.

Decision evaluation uses the same limits for HTTP and model inputs: at most 64 entries per array, 200 characters per name, and 250,000 action-scenario evaluations across the prior and all signals. Work above that budget is rejected before scoring. Each provider response may request at most one tool call; multiple calls fail before any evaluation, bounding aggregate work across the three rounds. Model tool rejection is journaled and returned as a diagnostic; it cannot bypass the computation limits after a stream finishes.

The database uses per-operation connections and immediate write transactions. Snapshot insertion, head movement, and the corresponding hash-linked journal event are atomic. Usage reservation/finalization and their journal entries are also atomic. The full turn spans multiple transactions deliberately, preserving a durable user turn and usage evidence if the process stops midstream.

## State and persistence

A revision contains `messages`, `memory`, `decisions`, `artifacts`, and optional `audits`. Its identifier hashes parent, kind, label, full snapshot, and UTC timestamp. Branches are named pointers. A restore copies an ancestor's snapshot into a new child revision, preserving the abandoned path.

The global journal links events across all branches with SHA-256. Integrity verification recalculates revision/event hashes and replays branch heads, metadata, and ledger entries, then checks SQLite integrity. Verification is performed before startup. It detects inconsistent local edits and missing middle records, not an adversary who can replace every hash or a consistent historical suffix.

An export is a readable branch snapshot plus ancestry, branch events, configuration without credentials, and the lifetime ledger. It is not a complete database backup or an import format. For disaster recovery, stop Dao cleanly before copying `.dao/state.sqlite3`; never assume copying a live WAL database's main file alone captures recent writes.

## Policy and trust

Audit claims for artifacts take the form `artifact:<trimmed-name>:<SHA256(trimmed-content)>`. The latest stored verdict for the exact claim must be allowed and its identifier must match the supplied identifier. A newer contradiction revokes an older verdict in that branch. Creating a branch or restoring a checkpoint can deliberately restore prior policy state; this is an explicit operator choice, and journal history remains available.

Source labels, evidence reliability, scenario probabilities, and utilities are operator inputs, not verified measurements. The model's decision results are advisory and have no automatic external effect. Local artifact saving is the only adjudication-gated action currently implemented.

## Source map

| File | Responsibility |
| --- | --- |
| `dao/store.py` | Revisions, branches, journal, usage reservations, integrity |
| `dao/service.py` | Turn lifecycle, branch locks, commands, artifact gate |
| `dao/decision.py` | Validated decision problem, posterior utility, waiting |
| `dao/audit.py` | Deterministic evidence adjudication and digest |
| `dao/provider.py` | Demo and live model streams; bounded read-only tools |
| `dao/server.py` | Loopback JSON/NDJSON API and static assets |
| `dao/config.py` | Server-side configuration and cost accounting |
| `dao/static/` | Accessible dependency-free browser workspace |
