# Dao writing terminal

Start `python -m dao --terminal` from source, or open standalone `Dao.exe`. Select an existing draft with `--branch NAME`. The writing interface uses drafts for branches, saved versions for revisions, and story notes for memory. The terminal and browser share the same saved state; see [Windows instructions](windows.md) for executable download, launch, and database selection.

Bring a scene, a character, or an unfinished idea. Type `/help` for the command list, or `/quit` to exit. `/say TEXT` sends literal text, including a message beginning with `/`. Terminal commands do not execute a shell.

| Command | Purpose |
| --- | --- |
| `/drafts`, `/draft NAME`, `/switch NAME` | List drafts, create an alternate draft from the viewed version, or switch drafts |
| `/version`, `/versions`, `/restore ID_PREFIX` | Refresh the current version, inspect saved versions, or restore as a new version |
| `/notes`, `/remember KEY=VALUE` | Inspect or save story notes in the current draft |
| `/connections`, `/conflicts` | Inspect connection assessments or unresolved conflicts |
| `/connect PATH` | Apply one connection operation from a JSON file |
| `/explore`, `/explore PATH` | Compare the illustrative story example or a supplied decision model |
| `/review PATH`, `/manuscript PATH` | Review a claim against sources, or save a document with its current matching review |
| `/activity`, `/verify` | Inspect lifetime usage or verify saved versions and activity |
| `/export PATH` | Create a readable JSON export; existing files are never overwritten |

Earlier names remain aliases: `/branches`, `/branch`, `/head`, `/history`, `/memory`, `/relationships`, `/relate`, `/decide`, `/decision`, `/audit`, `/artifact`, and `/usage`. JSON schemas and API names retain their existing fields. Comparisons use explicit numerical assumptions, not literary scores. Reviews check supplied evidence under the existing policy and do not decide canon or resolve intentional ambiguity.

Paths may contain spaces; surrounding single or double quotes are optional. Imports must be UTF-8 JSON objects of at most 128 KiB. Non-finite numbers and branch/head overrides are rejected. `/explore PATH` treats the whole object as its decision problem; other imports use their endpoint's operation fields. `/manuscript PATH` expects `name`, `content`, and `verdict_id`, just like `/artifact PATH`.

For a connection example, save this to `choice.json`:

```json
{"operation":"node","node":{"id":"reveal","label":"Commit to the ending","kind":"action","importance":1}}
```

Then run:

```text
/connect choice.json
/connections
/draft alternate-ending
/conflicts
```

See [relationships](relationships.md) for node, relation, assessment, observation, and resolution operations. An empty, entirely unknown, or entirely neutral graph displays **Not defined** coherence. Changing a belief to positive, neutral, or unknown leaves its conflict open.

Changes use the terminal's viewed version. If another view changes the draft, a stale change is rejected; `/version` refreshes it before retrying. Story notes and connections remain consistent with that viewed version. Global usage includes all drafts and previous restores.

Ctrl+C at the input prompt exits immediately. During a conversation turn, it requests exit after the turn's state and usage have been recorded. A closed output pipe also allows accounting to finish. Displayed text is filtered to remove terminal control sequences and redact configured credentials across streamed boundaries. Restore preserves recorded usage and cannot undo external actions.
