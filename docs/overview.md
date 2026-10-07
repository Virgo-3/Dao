# Dao

**A flowing conversation, with room to change your mind.**

Dao is a local conversational agent with saved revision history and branches. It records messages, decisions, audits, relationships, tool results, and usage. Its decision engine compares acting, waiting for information, and abstaining. Its relationship graph keeps beliefs separate from reported observations and tracks unresolved conflicts that can exclude particular actions from the calculation.

![Dao workspace](workspace.jpg)

## Windows executable

Download the standalone **Windows x64** package from [Releases](https://github.com/Virgo-3/Dao/releases/latest) and extract it. No Python installation is needed. Open PowerShell in the package folder:

```powershell
.\Dao.exe
```

The executable starts an interactive terminal. Type a message, or enter `/help` for state, branch, relationship, decision, audit, and usage commands. Use `.\Dao.exe --web --open-browser` to open the browser workspace. Both interfaces use the same saved state; the executable's default database is `%LOCALAPPDATA%\Dao\state.sqlite3`.

The executable is unsigned; compare it with the release's `SHA256SUMS`. See [Windows instructions](windows.md) for checksums, live AI configuration, and building from source. The [Windows build workflow](https://github.com/Virgo-3/Dao/actions/workflows/windows-build.yml) also supplies runnable packages.

## Run from source

Python **3.11 or newer**. No runtime packages, Node build, or API key are required for the offline demo.

```sh
git clone https://github.com/Virgo-3/Dao.git
cd Dao
python -m dao
```

Open **http://127.0.0.1:8765**. State lives in `.dao/state.sqlite3`; restarting preserves it. `python -m dao --port 9000 --db /path/to/state.sqlite3` changes the port or database.

For an interactive terminal using the same state, run `python -m dao --terminal`. Select an existing branch with `--branch NAME`. See [terminal commands and JSON imports](terminal.md) for source and executable launches.

The default **offline demo** produces deterministic replies without calling an AI model. Messages, branches, decisions, audits, and usage are still saved. For live AI, set environment variables before starting:

```powershell
$env:DAO_PROVIDER = "openai"
$env:OPENAI_API_KEY = "your-own-key"
$env:DAO_MODEL = "gpt-4.1-mini"
python -m dao
```

```sh
export DAO_PROVIDER=openai
export OPENAI_API_KEY=your-own-key
export DAO_MODEL=gpt-4.1-mini
python -m dao
```

Use a Responses API model available to your account. Live inference sends the current conversation, saved memory, and relationship context to OpenAI. The API key is sent for provider authentication and excluded from Dao's public configuration, state, and exports. The adapter uses the [Responses streaming API](https://developers.openai.com/api/docs/guides/streaming-responses), sets `store=false`, and offers a bounded, read-only `evaluate_decision` tool. It does not expose shell execution or external actions. Live billing and model availability were not tested during offline verification.

`.env.example` documents the settings; Dao reads **process environment variables**, not `.env` files. Optional `pip install .` installs the `dao` command and static assets.

## Explore

1. Have a conversation. Enter `/remember approach=Prefer reversible experiments` to save branch-local memory, or `/decide` for an example recorded in state.
2. Create a branch from the current checkpoint. Its memory, messages, decisions, and artifacts evolve separately.
3. Restore an earlier checkpoint. Restore creates a new revision; prior revisions, audit events, and recorded usage remain available.
4. Open **Decision** and edit the finite scenario model. Enter explicit probabilities, payoffs, reversibility, costs, and signal likelihoods. Inspect both the recommendation and its calculation.
5. Open **Relationships** to define weighted directed relations, assess beliefs, and record reported observations with their action and context. Review coherence, coverage, and unresolved conflicts together. Unresolved severe conflicts exclude their declared actions until an audit allows resolution. See the [relationship workflow](relationships.md).
6. Open **Audit** to review evidence for a claim and record a verdict. Source labels and reliability scores are supplied by you. Contradictions prevent approval; a supported verdict means the evidence meets the policy, without establishing factual truth.
7. Prepare an artifact claim for its exact name, content, and current relationship state. Review the claim, then save with the latest matching verdict. Saving requires a verdict without an action scope and no unresolved severe conflicts. Artifacts are content stored in Dao's database.
8. Open **Ledger** for usage, memory, and artifacts; export the branch or verify integrity from the header.

## Terms

| Term | Meaning |
| --- | --- |
| State | The messages, memory, decisions, audits, relationships, and artifacts at a revision |
| Revision | An immutable saved snapshot with a link to its parent |
| Checkpoint | The revision currently being viewed or selected for branching or restoration |
| Branch | A named history whose head points to its latest revision |
| Audit | A review of submitted evidence under an explicit policy, also called adjudication |
| Verdict | The audit's recorded result; permission also depends on the current state and operation |
| Artifact | Named content saved in Dao's database |

## Behavior and limits

| Capability | Runtime behavior |
| --- | --- |
| Versioned state | SHA-256-addressed snapshots with parent links; optimistic head checks reject stale writes |
| Branching | A new branch at any known revision, with its own subsequent state changes |
| Restore | A new revision copied from a reachable ancestor; history and usage remain saved |
| Relationships | Immutable definitions, weighted beliefs, explicit unknowns, persistent conflict cases, and empirical transition estimates |
| Audit | Inspectable evidence policy; the latest matching verdict binds exact artifact content and relationship state |
| Decisions | Bayes posterior optimization, mean-variance preferences, rollback and irreversibility costs, explicit wait/abstain options |
| Usage | Atomic lifetime token reservations across branches; final usage is idempotent; unknown failures retain conservative estimated charges |
| Model tools | Up to three model requests per turn, each reserved and accounted; one read-only decision tool |
| Browser access | Loopback-only server, Host/Origin checks, mutation CSRF token, restrictive CSP, no remote assets |

This is a **single-user local application**. Keep it bound to loopback; it has no authentication for public internet access. The owner of the database is trusted. Hash chains detect inconsistent edits, but provide no signing identity. Detecting replacement with a fully consistent older backup requires an externally trusted digest. Restoring a checkpoint preserves usage and cannot undo external actions. This release has no tools that perform external actions.

`DAO_TOKEN_BUDGET` defaults to 100,000 tokens over the database lifetime. Admission counts finalized usage plus outstanding reservations, including across branches. Reservations use conservative UTF-8 context estimates plus output caps, rather than a model-specific tokenizer; actual provider usage can exceed an estimate. Crashed requests remain reserved and continue consuming admission capacity. A failed/disconnected stream is retained with a failed status, and browser disconnection does not stop server-side accounting.

Set both `DAO_INPUT_USD_PER_MILLION` and `DAO_OUTPUT_USD_PER_MILLION` to your contractual rates to enable estimated dollar accounting. Without configured rates the interface shows **Pricing not configured**. Costs are calculated in integer micro-USD, rounded upward; cached-token discounts and provider billing adjustments are not modeled. Offline demo token figures are estimates and have zero monetary cost. Unknown failed calls use a conservative charge at the higher configured token rate.

## Verify and develop

```sh
python -m unittest discover -s tests -v
node --check dao/static/app.js
```

The suite covers Bayesian information value, abstention and reversibility, contradictory evidence, content-bound authorization, relationship conflict persistence and action exclusion, stale/concurrent updates, global budgets, stream failures, durable restart, hash tampering, HTTP origin/CSRF protection, terminal interactions, and adapter parsing with synthetic provider streams. GitHub Actions runs on Windows/Linux with Python 3.11/3.14. Node is only needed for the JavaScript syntax check. A separate Windows workflow builds the console executable, smoke-tests it, and publishes tagged releases.

See [architecture](architecture.md), [relationships](relationships.md), [decision model and derivation](decision-model.md), and [security boundaries](../SECURITY.md).
