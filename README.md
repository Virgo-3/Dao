# Dao

Dao is a local conversational agent with versioned, branchable state. It saves messages, memory, decisions, audits, relationships, artifacts, and usage so you can explore alternatives and revisit earlier checkpoints. Its decision engine compares **act**, **wait**, and **abstain**, accounting for uncertainty, reversibility, and the value of additional information.

Dao starts in an **offline demo** with deterministic replies and no model calls. Live AI is optional. The terminal and browser workspace both support saved state, branches, decisions, and evidence review.

This repository builds two separate applications. **Dao** is the general-purpose app and remains the default. **Dao Narrative** preserves the writing interface, story examples, and terminal language. Each has its own launcher, Windows executable, default database, and browser port; both share the same state and decision engine.

| Application | Browser launch | Terminal launch | Windows executable |
| --- | --- | --- | --- |
| Dao | `python -m dao` · port 8765 | `python -m dao --terminal` | `Dao.exe` |
| Dao Narrative | `python -m dao_narrative` · port 8766 | `python -m dao_narrative --terminal` | `DaoNarrative.exe` |

See the [Dao Narrative guide](docs/narrative.md) for its writing interface and commands.

![Dao browser workspace](docs/workspace.jpg)

## What Dao does

| Capability | Purpose |
| --- | --- |
| Revisions and branches | Save immutable snapshots and explore alternatives from a selected checkpoint |
| Restore | Create a new revision from an earlier checkpoint while retaining history and lifetime usage |
| Decision engine | Compare actions using explicit scenarios, probabilities, payoffs, costs, reversibility, and possible observations |
| Relationship graph | Separate assessed beliefs from reported observations and track persistent conflicts |
| Audit as adjudication | Review submitted evidence under an explicit policy and record a verdict |
| Artifacts | Save named content in the database when the current audit and relationship state permit it |
| Usage accounting | Reserve tokens before requests and account for usage across branches, restores, and failed calls |

The option value of waiting comes from the supplied observation model: Dao evaluates how new information could change the best available action, then accounts for waiting, observation, and delay costs. Recorded reconsideration conditions do not schedule future work.

## Run from source

Use **Python 3.11 or newer**. The offline demo needs no runtime dependencies or API key.

```sh
git clone https://github.com/Virgo-3/Dao.git
cd Dao
python -m dao
```

Open **http://127.0.0.1:8765**. To start the terminal instead:

```sh
python -m dao --terminal
```

Dao saves source-launch state in `.dao/state.sqlite3`; Dao Narrative uses `.dao-narrative/state.sqlite3`. Use `--db PATH` to choose another database, `--port 9000` to change the browser port, or `--branch NAME` to select an existing terminal branch. Each app's browser and terminal share its database. Optional `pip install .` installs `dao`, `dao-terminal`, `dao-narrative`, and `dao-narrative-terminal`.

## Windows executable

Get the current Windows x64 packages from the [Windows workflow](https://github.com/Virgo-3/Dao/actions/workflows/windows-build.yml). It builds separate ZIPs for Dao and Dao Narrative. Tagged builds are published under [Releases](https://github.com/Virgo-3/Dao/releases); releases before 0.4.0 contain only the earlier Dao app. Extract the desired ZIP and open PowerShell in that folder:

```powershell
.\Dao.exe
```

The executable starts the terminal and includes Python. To open the browser workspace:

```powershell
.\Dao.exe --web --open-browser
```

Dao saves executable state in `%LOCALAPPDATA%\Dao\state.sqlite3`; Dao Narrative uses `%LOCALAPPDATA%\DaoNarrative\state.sqlite3`. Both executables are unsigned and each ZIP contains `SHA256SUMS`. See the [Windows guide](docs/windows.md) for launch, verification, and build instructions.

Existing databases remain in place, including any writing work saved by the earlier Dao interface. To open one in either app, pass its path with `--db PATH`. Launching the other app does not copy or rewrite it.

## Try a branch

In the terminal, type a message to converse, then try:

```text
/remember approach=Prefer reversible experiments
/decide
/branch experiment
/history
/switch main
/usage
/verify
```

`/branch experiment` creates a branch from the viewed checkpoint. Subsequent changes stay on that branch. `/restore ID_PREFIX` restores a reachable earlier checkpoint as a new revision. `/help` lists commands; `/quit` exits. See the [terminal guide](docs/terminal.md) for decision, relationship, audit, and artifact JSON imports.

In the browser, open **Tools** for **Decision**, **Relationships**, **Audit**, and **Usage**. Review a decision summary, expand its model to edit the JSON, or inspect beliefs, evidence, usage, memory, and artifacts. On smaller screens, **Back to conversation** (shown as **← Chat** on phones) returns to the conversation.

## Enable live AI

Dao supports the OpenAI Responses API. Set process environment variables before launch:

```powershell
$env:DAO_PROVIDER = "openai"
$env:OPENAI_API_KEY = "your-own-key"
python -m dao --terminal
```

On macOS or Linux:

```sh
export DAO_PROVIDER=openai
export OPENAI_API_KEY=your-own-key
python -m dao --terminal
```

Use `DAO_MODEL` to select a Responses API model available to your account. See [.env.example](.env.example) for all settings; Dao does not load `.env` files automatically.

Live inference sends the current conversation, saved memory, and relationship context to OpenAI with `store=false`. The API key is used for authentication and excluded from Dao's public configuration, saved state, and exports. Model requests have bounded token reservations and a read-only decision tool. The default lifetime token budget is **100,000**; configure `DAO_TOKEN_BUDGET` to change it. Optional input and output rates enable estimated dollar accounting. Estimates are not provider invoices, and offline demo token counts are estimates with zero monetary cost.

## How to interpret results

Decisions depend on the scenarios and probabilities you supply. Relationship transition estimates describe reported associations. Coherence and coverage should be read together; coherence is **Not defined** when there is no positive or negative weight to compare.

Unresolved severe conflicts exclude their declared actions from decision calculations. Changing a belief alone leaves a conflict open. Resolution requires fresh evidence review bound to the current relationship state. An audit verdict records whether submitted evidence meets the policy; source reliability and factual truth are not independently verified. Contradicting evidence prevents approval.

Dao is a single-user application bound to `127.0.0.1`. Artifacts are content saved in its database; the model has no tools for shell execution or external actions. Restoring state cannot undo external actions. Hash-linked history supports consistency checks and does not authenticate changes against the database owner. See [security boundaries](SECURITY.md) for the trust model.

## Documentation and development

- [Project guide](docs/overview.md)
- [Terminal commands](docs/terminal.md) and [Windows instructions](docs/windows.md)
- [Dao Narrative](docs/narrative.md)
- [Relationships and conflict resolution](docs/relationships.md)
- [Decision model](docs/decision-model.md) and [mathematical checks](docs/mathematical-checks.md)
- [Architecture](docs/architecture.md) and [security boundaries](SECURITY.md)

Run the Python tests and browser JavaScript syntax check:

```sh
python -m unittest discover -s tests -v
node --check dao/static/app.js
node --check dao_narrative/static/app.js
```

Node is only needed for the syntax checks. GitHub Actions runs tests on Windows and Linux, and a separate workflow builds and smoke-tests both Windows executables. Provider tests use synthetic responses; live inference and provider billing require separate verification.

Dao is available under the [MIT license](LICENSE).
