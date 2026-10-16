# Dao terminal

Start `python -m dao --terminal` from source, or open standalone `Dao.exe`. Select an existing branch with `--branch NAME`. The terminal and browser use the same saved state; see [Windows instructions](windows.md) for executable download, launch, and database selection.

Type a message to converse, `/help` for the command list, or `/quit` to exit. `/say TEXT` sends literal text, including a message beginning with `/`. Terminal commands do not execute a shell.

| Command | Purpose |
| --- | --- |
| `/branches`, `/branch NAME`, `/switch NAME` | List branches, fork the viewed checkpoint, or switch branches |
| `/head`, `/history`, `/restore ID_PREFIX` | Refresh the checkpoint, inspect reachable revisions, or restore as a new revision |
| `/memory`, `/remember KEY=VALUE` | Inspect or save memory in the current branch |
| `/messages` | Inspect stable source message IDs and original content |
| `/propose PATH`, `/proposals` | Stage a citation-backed structure proposal from JSON, or inspect pending and reviewed proposals |
| `/accept ID`, `/reject ID` | Review a pending structure proposal by its full ID |
| `/relationships`, `/conflicts` | Inspect the viewed relationship summary or unresolved conflicts |
| `/relate PATH` | Apply one relationship operation from a JSON file |
| `/decide`, `/decision PATH` | Evaluate the example or an explicit decision problem |
| `/audit PATH`, `/artifact PATH` | Review a claim and evidence, or save an artifact with its current matching verdict |
| `/usage`, `/verify` | Inspect lifetime usage or verify state integrity |
| `/export PATH` | Create a readable JSON export; existing files are never overwritten |

Paths may contain spaces; surrounding single or double quotes are optional. Imports must be UTF-8 JSON objects of at most 128 KiB. Non-finite numbers and branch/head overrides are rejected. `/decision` treats the whole object as its decision problem; other imports use their endpoint's operation fields. `/propose` takes `rationale` and `operations`; it does not apply those operations until `/accept ID` succeeds.

For a relationship example, save this to `action.json`:

```json
{"operation":"node","node":{"id":"launch","label":"Full launch","kind":"action","importance":1}}
```

Then run:

```text
/relate action.json
/relationships
/branch experiment
/conflicts
```

See [relationships](relationships.md) for node, relation, assessment, observation, and resolution operations. An empty, entirely unknown, or entirely neutral graph displays **Not defined** coherence. Changing a belief to positive, neutral, or unknown leaves its conflict open.

To record semantic structure from dialogue, send a message, inspect `/messages`, and copy the chosen ID and exact passage into a proposal JSON file. Then use `/propose proposal.json`, inspect `/proposals`, and run `/accept ID` or `/reject ID`. This workflow works in the offline demo; live AI may also stage proposals during a successful conversation turn. See [conversation structure](structure.md) for a complete template. A proposal whose base graph has changed must be rejected and staged again against current relationships.

Mutations use the terminal's viewed checkpoint. If another view changes the branch, a stale mutation is rejected; `/head` refreshes it before retrying. Read-only memory and relationship commands remain consistent with that viewed checkpoint. Global usage includes all branches and previous restores.

Ctrl+C at the input prompt exits immediately. During a conversation turn, it requests exit after the turn's state and usage have been recorded. A closed output pipe also allows accounting to finish. Displayed text is filtered to remove terminal control sequences and redact configured credentials across streamed boundaries. Restore preserves recorded usage and cannot undo external actions.
