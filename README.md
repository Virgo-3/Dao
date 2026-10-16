# Dao

**A conversation with room to change your mind.**

Dao is a local conversational workspace where you can follow a thought, branch into another possibility, and return to an earlier checkpoint. It saves your conversation and working state together, with tools for comparing decisions, reviewing evidence, and tracking usage. Decisions, assumptions, requirements, and analysis can become structured records linked to the exact conversation passages behind them.

Use the browser workspace or the streaming terminal. Both work with the same saved history.

**The default is an offline demo.** It produces deterministic replies without an account, API key, or model call. You can try the full state and decision workflow before enabling live AI.

![Dao browser workspace](docs/workspace.jpg)

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

## Try another direction

Start the terminal, type a message, then try:

```text
/remember plan=Compare options before committing
/branch experiment
/remember plan=Try a small pilot
/decide
/switch main
/memory
```

The `experiment` branch keeps the pilot plan and its decision result. Back on `main`, `/memory` shows the original plan. Each branch carries its own messages, memory, decisions, relationships, audits, and saved content.

Use `/history` to inspect saved revisions and `/restore ID_PREFIX` to restore a reachable checkpoint as a new revision. Earlier history and recorded usage remain available. `/help` lists commands; `/quit` exits.

In the browser, use the branch sidebar and the **Decision**, **Relationships**, **Audit**, and **Usage** tabs. The header provides export and integrity checks. See the [terminal guide](docs/terminal.md) for commands and JSON imports.

## What you can work with

| Tool | What it does |
| --- | --- |
| **History and branches** | Save checkpoints, explore alternatives independently, and restore earlier state. |
| **Decisions** | Compare acting, waiting for more information, and abstaining using explicit scenarios, probabilities, payoffs, costs, and reversibility. |
| **Relationships** | Record directed relationships, assessed beliefs, and reported observations. Inspect coverage and unresolved conflicts. |
| **Structure proposals** | Review citation-backed updates before accepting them into the graph; trace declared dependencies to affected conclusions. |
| **Evidence review** | Review a claim under an explicit evidence policy. Save named content when the current verdict and relationship state permit it. |
| **Usage** | Track token usage across branches, restores, and failed requests, with optional estimated costs. |

The decision model can value waiting when an observation could change the best choice. Its result depends on the assumptions you enter. Relationship estimates describe reported associations, and an audit verdict records whether submitted evidence meets the policy. You supply source reliability; the review does not independently establish factual truth.

Unresolved severe relationship conflicts exclude their declared actions from decision calculations. Resolving a conflict requires a current evidence review; changing a belief alone leaves it open. The [relationship guide](docs/relationships.md) explains the workflow.

In **Relationships**, stage a JSON structure proposal or review a live model's proposal, inspect its source passages, then accept or reject it. The terminal provides `/messages`, `/propose PATH`, `/proposals`, `/accept ID`, and `/reject ID`. Manual proposals work in the offline demo. See the [conversation structure guide](docs/structure.md) for an example and dependency semantics.

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

Live AI sends the current conversation, saved memory, accepted relationship context, and pending structure proposals to OpenAI. The API key is used for authentication and excluded from saved state, exports, and browser configuration. The model can request read-only decision evaluation and stage structure proposals for your review. Accepting or rejecting proposals remains an operator action; the model has no shell or external-action tools.

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
