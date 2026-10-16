# Dao

**A conversation with room to change your mind.**

Dao is a local conversational workspace that keeps your conversation and its semantic structure together: decisions, reasons, possibilities, assumptions, and the passages they came from. Follow a thought, explore another direction, and return to the reasoning behind an earlier choice.

Talk naturally in the browser workspace or the streaming terminal. Both use the same saved history. A tentative idea stays tentative, a decision can keep its reasons attached, and a correction preserves the earlier context.

**The default is an offline demo.** Common memory, recall, and branching phrases work locally without an account, API key, or model call. Open-ended replies are short canned responses; enable live AI for a model-backed conversation.

![Dao conversation beside remembered decisions, reasons, and tentative ideas](docs/workspace.jpg)

*Conversation and remembered context, side by side. Shown with offline demo data.*

## Get started

### From source

Requires **Python 3.11 or newer**. The offline demo uses only the Python standard library.

```sh
git clone https://github.com/Virgo-3/Dao.git
cd Dao
python -m dao
```

Open [localhost:8765](http://127.0.0.1:8765) in your browser. To start a conversation in the terminal:

```sh
python -m dao --terminal
```

Optional: `pip install .` adds the `dao` and `dao-terminal` commands. For browser launches, add `--open-browser` to open the workspace automatically, or `--port 9000` to choose another port.

### Windows executable

Download the current `Dao-windows-x64` artifact from the [Windows build workflow](https://github.com/Virgo-3/Dao/actions/workflows/windows-build.yml), or a tagged package from [Releases](https://github.com/Virgo-3/Dao/releases). Extract the ZIP and open PowerShell in that folder:

```powershell
.\Dao.exe
```

Python is bundled. To use the browser workspace:

```powershell
.\Dao.exe --web --open-browser
```

The executable is unsigned; the package includes `SHA256SUMS` for verification. See the [Windows guide](docs/windows.md) for checksums and build instructions.

## Start with a conversation

Send these messages in the browser or terminal, one at a time:

```text
We chose SQLite because this must work offline.
Maybe Postgres.
Actually, multiple people need to write at once.
Why did we decide that?
Let's explore another direction
Go back to main
```

Dao records SQLite as the decision and offline operation as its reason. Postgres remains an option under consideration. The new assumption preserves the earlier reasoning and marks the related decision for reconsideration. Recall returns the saved reason. Exploring another direction creates an `alternative` branch, or a numbered alternative if that name exists; going back to `main` returns to its own saved context.

Local recognition covers common phrases rather than every paraphrase. Recognized competing choices or a later retraction prompt clarification before a new decision is saved. See [talking with Dao](docs/conversation.md) for phrases and boundaries.

In the terminal, `/history` lists saved revisions and `/restore ID_PREFIX` restores a reachable checkpoint as a new revision. Earlier history and recorded usage remain available. `/help` lists commands; `/quit` exits. See the [terminal guide](docs/terminal.md).

## See what Dao remembers

The browser opens on the conversation, with the current branch visible above it. Starter prompts fill an editable message; send it when you are ready to save it. **History** reveals branches and revisions. **Context & tools** opens on **Memory**, organized by the stance recorded with each note:

- **Decided** — stated choices and the reasons they depend on.
- **Considering** — possibilities that are still open.
- **Working context** — beliefs, requirements, and assumptions you have shared.
- **Mentioned & other notes** — context without a recorded commitment.

Expand **Original words** to inspect a source passage, then select **View original message** to highlight it in the conversation. Later corrections flag affected decisions for reconsideration. Explicitly replaced notes remain inspectable under **Earlier context**, and named memory entries appear separately.

Memory follows the branch you are viewing. Exploring an alternative keeps its subsequent notes separate; switching back restores that branch's own context. On smaller screens, the context panel opens separately, with a control to return to the conversation.

Routine remembering needs no JSON or proposal review. A pending advanced suggestion appears in Memory with a link to its review in **Structure**. Editors and relationship diagnostics stay collapsed until needed.

## What you can work with

| Where | What you can do |
| --- | --- |
| **History** | Inspect saved revisions, explore branches independently, and restore earlier state. |
| **Decision** | Compare acting, waiting for more information, and abstaining. Inspect immediate utility, signal value, risk penalties, and reversibility from explicit inputs. |
| **Structure** | Follow directed dependencies and source passages, review advanced proposals, and inspect assessed beliefs, reported observations, coverage, and unresolved conflicts. |
| **Audit** | Review a claim against supplied evidence. Set the required source count and reliability threshold in **Review rule**; save named content when its current verdict and relationship state permit it. |
| **Usage** | Track token usage across branches, restores, and failed requests, with optional estimated costs. |

The advanced tabs are inside **Context & tools**. **Export conversation** and **Verify history** are above them.

The decision model can value waiting when an observation could change the best choice. Its result depends on the assumptions you enter. Relationship estimates describe reported associations. An audit counts distinct supporting sources at each source's lowest submitted reliability; any contradiction contests the claim. You supply source reliability, and meeting the review policy does not independently establish factual truth. See the [decision and evidence rules](docs/decision-model.md).

Unresolved severe relationship conflicts exclude their declared actions from decision calculations. Resolving a conflict requires a current evidence review; changing a belief alone leaves it open. The [relationship guide](docs/relationships.md) explains the workflow.

Advanced structure proposals support changes that need explicit review, including relationship assessments and policy settings. In **Context & tools → Structure**, inspect a proposal's source passages before accepting or rejecting it. “Keep that” or “Reject that suggestion” can review a single unambiguous pending suggestion. The [structure guide](docs/structure.md) documents optional JSON imports and dependency semantics.

## Enable live AI

Dao supports the OpenAI Responses API. Set environment variables in the shell that launches it.

**PowerShell**

```powershell
$env:DAO_PROVIDER = "openai"
$env:OPENAI_API_KEY = "your-own-key"
python -m dao --terminal
```

**macOS or Linux**

```sh
export DAO_PROVIDER=openai
export OPENAI_API_KEY=your-own-key
python -m dao --terminal
```

For the Windows package, replace the final command with `.\Dao.exe`. Use `DAO_MODEL` to select a Responses API model available to your account.

Live AI sends the current conversation, saved memory, accepted relationship context, recent activity summaries, and pending structure proposals to OpenAI. The API key is used for authentication and excluded from saved state, exports, and browser configuration. The model can remember low-impact context cited to your latest message, request read-only decision evaluation, and stage advanced proposals for your review. It cannot accept proposals, resolve conflicts, change action policy through routine memory, or perform external actions. Common local phrases continue to work without a provider request.

| Setting | Purpose |
| --- | --- |
| `DAO_PROVIDER` | `demo` by default; set to `openai` for live AI. |
| `DAO_MODEL` | Model to use for live requests. |
| `DAO_TOKEN_BUDGET` | Lifetime token budget for the database; defaults to `100000`. |
| `DAO_MAX_OUTPUT_TOKENS` | Output token limit per model request; defaults to `2048`. |

See [.env.example](.env.example) for all settings. Dao reads process environment variables and does **not** load `.env` files automatically. Set both `DAO_INPUT_USD_PER_MILLION` and `DAO_OUTPUT_USD_PER_MILLION` to enable estimated dollar accounting. These estimates are not provider invoices; demo usage has zero monetary cost.

## Where your work is saved

| Launch method | Default database |
| --- | --- |
| From source | `.dao/state.sqlite3`, relative to the working directory |
| Windows executable | `%LOCALAPPDATA%\Dao\state.sqlite3` |

Restarting Dao preserves your work. Use `--db PATH` to open an existing database or create a separate workspace. Point the browser and terminal at the same path to share state across interfaces.

Dao runs as a single-user local application, bound to `127.0.0.1`. Restoring a checkpoint preserves history and usage; it cannot undo external actions. Integrity checks detect inconsistent edits, but do not authenticate changes against the database owner. Read the [security boundaries](SECURITY.md) for the trust model and the [architecture guide](docs/architecture.md#state-and-persistence) for backup details.

## Documentation

- [Project guide](docs/overview.md) — concepts, workflows, configuration, and limits
- [Talking with Dao](docs/conversation.md) — natural memory, recall, corrections, and alternatives
- [Terminal guide](docs/terminal.md) — commands and JSON imports
- [Windows guide](docs/windows.md) — downloads, verification, and packaging
- [Relationships](docs/relationships.md) — beliefs, observations, conflicts, and resolution
- [Conversation structure](docs/structure.md) — source passages, proposals, and dependencies
- [Decision model](docs/decision-model.md) — assumptions and calculations
- [Mathematical checks](docs/mathematical-checks.md) — verification of the decision model
- [Architecture](docs/architecture.md) — runtime, persistence, and policy
- [Security boundaries](SECURITY.md) — access and trust assumptions

## Development

Run from the repository root:

```sh
python -m unittest discover -s tests -v
node --check dao/static/app.js
```

Node is needed only for the JavaScript syntax check. GitHub Actions runs the Python suite on Windows and Linux with Python 3.11 and 3.14. A separate workflow builds and smoke-tests `Dao.exe`. Provider tests use synthetic responses; live inference and provider billing need separate verification.

## License

[MIT](LICENSE).
