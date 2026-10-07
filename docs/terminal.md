# Dao terminal

Start `python -m dao --terminal` from source, or open standalone `Dao.exe`. Select an existing branch with `--branch NAME`. The terminal and browser use the same durable runtime; see [Windows instructions](windows.md) for executable download, launch, and database selection.

Type a message to converse, `/help` for the command list, or `/quit` to exit. `/say TEXT` sends literal text, including a message beginning with `/`. Terminal commands do not execute a shell.

| Command | Purpose |
| --- | --- |
| `/branches`, `/branch NAME`, `/switch NAME` | List branches, fork the viewed checkpoint, or switch branches |
| `/head`, `/history`, `/restore ID_PREFIX` | Refresh, inspect reachable revisions, or append a restoration |
| `/memory`, `/remember KEY=VALUE` | Inspect or save branch-local notes |
| `/relationships`, `/conflicts` | Inspect the viewed relationship summary or unresolved cases |
| `/relate PATH` | Apply one relationship operation from a JSON file |
| `/decide`, `/decision PATH` | Evaluate the example or an explicit decision problem |
| `/audit PATH`, `/artifact PATH` | Adjudicate evidence or save an artifact with its matching current verdict |
| `/usage`, `/verify` | Inspect lifetime usage or verify state integrity |
| `/export PATH` | Create a readable JSON export; existing files are never overwritten |

Paths may contain spaces; surrounding single or double quotes are optional. Imports must be UTF-8 JSON objects of at most 128 KiB. Non-finite numbers and branch/head overrides are rejected. `/decision` treats the whole object as its decision problem; other imports use their endpoint's operation fields.

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

See [relationships](relationships.md) for remaining node, relation, assessment, observation, and resolution operations. An empty or wholly unknown graph reports **Unassessed** coherence. Reclassifying adverse evidence does not remove its conflict.

Mutations use the terminal's viewed checkpoint. If another view changes the branch, a stale mutation is rejected; `/head` refreshes it before retrying. Read-only memory and relationship commands remain consistent with that viewed checkpoint. Global usage includes all branches and previous restores.

Ctrl+C at the input prompt exits immediately. During an admitted conversation turn, it requests exit after the turn's state and usage have settled. A closed output pipe also allows accounting to finish. Displayed text is filtered to remove terminal control sequences and redact configured credentials across streamed boundaries. Restore preserves expenditure and cannot undo external effects.
