# Conversation structure and source passages

Dao can preserve a decision, its requirements and assumptions, supporting analysis, and conditions for reconsideration as graph records linked to conversation. Routine memory is conversational: “We chose SQLite because this must work offline” saves a decision and reason, while “Maybe Postgres” stays tentative. Start with [talking with Dao](conversation.md); ordinary remembering needs no JSON or review.

This guide covers the underlying records and advanced structure proposals. A proposal keeps its changes separate from accepted relationships until you review them. Manual proposals work offline; live AI can propose changes that need explicit review, such as assessments or policy settings.

## Cite the conversation

New user and assistant messages receive UUID identifiers. `/messages` in the terminal and **Source details** beneath a browser message expose the ID and original content. The browser can copy a citation or follow a saved source link to highlight its passage.

A citation has this shape; replace the placeholder before using it:

```json
{"message_id":"REPLACE_WITH_MESSAGE_ID","start":0,"end":15,"quote":"We chose SQLite"}
```

Offsets count Unicode code points from zero: `start` is inclusive and `end` is exclusive. They refer to the original saved message, including its spaces and line breaks, without model source labels or UI formatting. An emoji such as 🧭 counts as one code point; a displayed character composed of several code points counts accordingly. The runtime checks the exact quote and adds a deterministic `passage_id`. Missing messages, failed assistant messages, or mismatched offsets and quotes are rejected. An exact source establishes provenance; it does not establish that the statement is true or that an interpretation follows from it.

Older messages receive deterministic IDs in detached read views. `/api/state` exposes these as top-level `messages`; the original `head` snapshot and historical hashes remain unchanged. A normal new revision materializes those same IDs. Reading old history does not migrate it in place.

## Try the workflow offline

For a manual example, start `python -m dao --terminal` and use `/say` to send this literal message without automatic local capture:

```text
/say We chose SQLite because this must work offline. Reconsider if concurrent writers become necessary.
/messages
```

Find the **user** message in `/messages`. Save the following as `proposal.json`, replacing **every** `REPLACE_WITH_MESSAGE_ID` with that message's actual ID. These offsets are valid for the exact message above; use your own offsets and quotes for different text.

```json
{
  "rationale": "Preserve the choice, its offline requirement, and a reconsideration condition.",
  "operations": [
    {
      "operation": "node",
      "node": {
        "id": "sqlite-choice", "label": "Use SQLite", "kind": "decision", "importance": 1,
        "citations": [{"message_id": "REPLACE_WITH_MESSAGE_ID", "start": 0, "end": 15, "quote": "We chose SQLite"}]
      }
    },
    {
      "operation": "node",
      "node": {
        "id": "offline-required", "label": "Must work offline", "kind": "requirement", "importance": 1,
        "citations": [{"message_id": "REPLACE_WITH_MESSAGE_ID", "start": 24, "end": 46, "quote": "this must work offline"}]
      }
    },
    {
      "operation": "node",
      "node": {
        "id": "sqlite-reconsider", "label": "Reconsider SQLite if concurrent writers become necessary", "kind": "condition", "importance": 1,
        "citations": [{"message_id": "REPLACE_WITH_MESSAGE_ID", "start": 48, "end": 98, "quote": "Reconsider if concurrent writers become necessary."}]
      }
    },
    {
      "operation": "relation",
      "relation": {
        "id": "sqlite-needs-offline", "source": "sqlite-choice", "target": "offline-required", "kind": "depends_on",
        "weight": 1, "severe": false, "actions": [],
        "citations": [{"message_id": "REPLACE_WITH_MESSAGE_ID", "start": 0, "end": 47, "quote": "We chose SQLite because this must work offline."}]
      }
    },
    {
      "operation": "relation",
      "relation": {
        "id": "writers-affect-choice", "source": "sqlite-reconsider", "target": "sqlite-choice", "kind": "effect",
        "weight": 1, "severe": false, "actions": [],
        "citations": [{"message_id": "REPLACE_WITH_MESSAGE_ID", "start": 48, "end": 98, "quote": "Reconsider if concurrent writers become necessary."}]
      }
    }
  ]
}
```

Then run:

```text
/propose proposal.json
/proposals
```

Inspect the operations and source passages. Run `/accept ID` using the proposal's full returned ID, or `/reject ID`. “Keep that” and “Reject that suggestion” also work when exactly one pending suggestion is available. Staging alone leaves the accepted graph unchanged. In the browser, use **Context & tools → Structure → Stage a proposal**, paste the same JSON, and review its source links before selecting **Accept** or **Reject**. **Start from latest message** can also fill an editable cited claim from your most recent user message; this manual starter copies your words, so review the meaning and label before staging. Use fresh node and relation IDs if these definitions already exist.

The reconsideration condition is recorded knowledge, not a running monitor. Dao does not detect concurrent writers or revise the choice automatically.

## Review and persistence

A proposal contains 1–32 `node`, `relation`, `assess`, or `observe` operations. Each operation requires at least one citation: put `citations` inside the `node` or `relation` record, or at the top level of an assessment or observation operation. Definitions must precede operations that reference them. Direct relationship operations also support these citations, but keep them optional for existing workflows.

Proposals accept only `rationale` and `operations`. The rationale is nonempty text of at most 4,000 characters, the payload is limited to 128 KiB, and a branch state may contain at most 100 proposals, including reviewed ones. Saved `state.proposals` records retain their ID, source (`operator` or `model`), timestamps, base graph digest, operations, and `pending`, `accepted`, or `rejected` status.

Acceptance checks source passages and every operation again, then saves the entire graph update and review status in one revision. It is bound to the graph digest at staging. If relationships changed, reject the stale proposal and stage a fresh one against the current graph; refreshing the checkpoint alone does not update its base digest. Rejection retains the reviewed candidate without changing accepted relationships. Branching and restoring preserve the proposal state at the selected revision. Accepting a graph change requires a fresh artifact audit under the existing graph-bound permission policy.

The live model's `propose_structure` tool can stage candidates only. It cannot accept or reject them, resolve conflicts, or mutate accepted relationships. Its separate `remember_conversation` tool saves routine nodes and relations cited only to the latest user message, without review. Those relations must have `severe: false` and `actions: []`; routine capture cannot assess beliefs, record observations, or change action policy. The adapter shows stable message IDs, accepted structure, recent activity summaries, and pending proposals to help the model cite sources and avoid duplicates.

Completed assistant messages store semantic and navigation `activities`; activity events are emitted after durable turn finalization. A failed conversation turn restores its prior graph and proposal state and retains a labeled failed reply and usage evidence. No successful memory activity is emitted for changes that failed to commit.

## Meaning and dependencies

Node kinds are `person`, `goal`, `claim`, `action`, `decision`, `assumption`, `requirement`, `condition`, and `analysis`. Relation kinds are `effect`, `support`, `compatibility`, `resource`, `depends_on`, `contradicts`, and `supersedes`. Existing required fields such as node importance and relation weight, severity, and action scope still apply.

Nodes may also contain optional `content` and `stance`. Stance is `mentioned`, `believed`, `considering`, or `decided`; old records without it remain valid. Routine capture preserves the distinction between a mentioned topic, a stated belief, a tentative option, and an affirmative decision. It does not turn quotations, hypotheticals, uncertainty, or negation into settled choices.

For `depends_on`, the **source depends on the target**. If analysis depends on an assumption and a decision depends on that analysis, the assumption's transitive dependents include both. The snapshot's top-level `dependencies` maps each node ID to its sorted transitive dependents; it excludes the node itself even when the graph contains a cycle. It reports declared links without inferring additional real-world relationships or invalidating conclusions automatically.

`supersedes` can preserve a newer conclusion alongside its predecessor. `contradicts` can preserve disagreement. Both are declarations: neither deletes a record, proves truth or inconsistency, changes a belief, or closes a conflict. Conversational recall can prefer a record that has not been explicitly superseded, while the earlier record and source remain saved. An “Actually” correction preserves earlier reasons and highlights related decisions to revisit; it creates a replacement link only when the user's wording explicitly supports one. Definitions remain immutable; use a new ID for a revised definition. Existing assessment and conflict rules continue to apply.

## HTTP interface

Both mutations use the selected `branch` and current `expected_head`. Browser requests also require the application's usual CSRF header. Successful requests return the new `head` and stored proposal as `result`.

| Endpoint | Additional JSON fields |
| --- | --- |
| `POST /api/structure/propose` | `rationale`, `operations` |
| `POST /api/structure/review` | `proposal_id`, `decision` (`accept` or `reject`) |
| `POST /api/decision` | `problem`, optional `citations` and `dependency_ids` |

Decision records save their own ID, the problem and result, source citations, explicitly supplied existing-node `dependency_ids`, and the relationship graph digest. Numeric decisions do not infer dependency IDs from action names or utilities. To represent semantic reasoning, create `decision` or `analysis` graph nodes and connect them to assumptions or requirements using `depends_on`. The terminal's `/decision PATH` still imports a plain decision problem; optional record metadata is available through the HTTP route.
