# Dao

**A flowing conversation, with room to change your mind.**

Dao is a local conversational agent whose working state has immutable revisions and branchable histories. Its runtime records decisions, evidence adjudications, relationships, tool results, and usage independently of model prose. A decision can favor acting, abstaining, or waiting for information while preserving the option to choose differently. A versioned relationship graph tracks weighted beliefs, observed transitions, and unresolved conflicts that can withhold particular actions.

![Dao workspace](workspace.jpg)

## Windows executable

Download the standalone **Windows x64** package from [Releases](https://github.com/Virgo-3/Dao-1/releases/latest) and extract it. No Python installation is needed. Open PowerShell in the package folder:

```powershell
.\Dao.exe
```

The executable starts an interactive terminal. Type a message, or enter `/help` for state, branch, relationship, decision, audit, and usage commands. Use `.\Dao.exe --web --open-browser` to open the browser workspace instead. Both modes share the same durable state engine; standalone state defaults to `%LOCALAPPDATA%\Dao\state.sqlite3`.

The executable is unsigned; compare it with the release's `SHA256SUMS`. See [Windows launch and build instructions](windows.md) for checksums, live AI configuration, and a reproducible build. The [Windows build workflow](https://github.com/Virgo-3/Dao-1/actions/workflows/windows-build.yml) also supplies runnable packages.

## Run from source

Python **3.11 or newer**. No runtime packages, Node build, or API key are required for the offline demo.

```sh
git clone https://github.com/Virgo-3/Dao-1.git
cd Dao-1
python -m dao
```

Open **http://127.0.0.1:8765**. State lives in `.dao/state.sqlite3`; restarting preserves it. `python -m dao --port 9000 --db /path/to/state.sqlite3` changes the port or database.

For an interactive terminal using the same state, run `python -m dao --terminal`. Select an existing branch with `--branch NAME`. See [terminal commands and JSON imports](terminal.md) for source and executable launches.

The default **demo** is a clearly labeled deterministic simulator. Branching, decisions, adjudication, persistence, and the ledger operate normally. For live AI, set environment variables before starting:

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

The model is configurable; use a Responses API model available to your account. Credentials stay on the server. Live inference sends the current conversation, saved memory, and relationship context to OpenAI. The adapter uses the [Responses streaming API](https://developers.openai.com/api/docs/guides/streaming-responses), sets `store=false`, and offers a bounded, read-only `evaluate_decision` tool. It does not expose shell execution or external actions. Live billing and model availability were not exercised in offline verification.

`.env.example` documents the settings; Dao reads **process environment variables**, not `.env` files. Optional `pip install .` installs the `dao` command and static assets.

## Explore

1. Have a conversation. Enter `/remember approach=Prefer reversible experiments` to save branch-local memory, or `/decide` for an example recorded in state.
2. Create a branch from the current checkpoint. Its memory, messages, decisions, and artifacts evolve separately.
3. Restore an earlier moment. Restore appends a new revision; prior revisions, audit events, and incurred usage remain available.
4. Open **Decision** and edit the finite scenario model. Enter explicit probabilities, payoffs, reversibility, costs, and signal likelihoods. Inspect both the recommendation and its calculation.
5. Open **Relationships** to define weighted directed relations, assess beliefs, and record action/context observations. Inspect coherence alongside coverage and unresolved conflicts. Severe cases withhold scoped decision actions until an evidence adjudication resolves them. See the [complete relationship workflow](relationships.md).
6. Open **Audit** to adjudicate submitted evidence. Distinct source labels and reliability scores are operator-supplied assumptions. Contradictions veto approval; a supported verdict is a policy result, not factual proof.
7. Prepare an artifact audit to bind the claim to its name, content SHA-256, and current relationship state. Save only after the latest matching unscoped adjudication allows it. Artifacts are reversible data in SQLite.
8. Open **Ledger** for usage, memory, and artifacts; export the branch or verify integrity from the header.

## Guarantees and boundaries

| Capability | Runtime behavior |
| --- | --- |
| Versioned state | SHA-256-addressed snapshots with parent links; optimistic head checks reject stale writes |
| Branching | A new pointer at any known commit, then isolated state evolution |
| Restore | Append-only restoration to a reachable ancestor; no deletion of history or costs |
| Relationships | Immutable definitions, weighted beliefs, explicit unknowns, persistent conflict cases, and empirical transition estimates |
| Adjudication | Reproducible evidence digest and inspectable rule; newest unscoped verdict governs exact artifact content and relationship state |
| Decisions | Bayes posterior optimization, mean-variance preferences, rollback and irreversibility costs, explicit wait/abstain options |
| Usage | Atomic lifetime token reservations across branches; final usage is idempotent; unknown failures retain conservative estimated charges |
| Model tools | Up to three model requests per turn, each reserved and accounted; one read-only decision tool |
| Browser access | Loopback-only server, Host/Origin checks, mutation CSRF token, restrictive CSP, no remote assets |

This is a **single-user local application**, not an authenticated internet service. Keep it bound to loopback. The owner of the database is trusted: hash chains detect inconsistent modifications but are not signatures, and cannot detect replacement or truncation to a fully consistent older backup without an external trusted digest. State rewind cannot reverse real external effects. There are no external effect adapters in this release.

`DAO_TOKEN_BUDGET` defaults to 100,000 tokens over the database lifetime. Admission counts finalized usage plus outstanding reservations, including across branches. Reservations use conservative UTF-8 context estimates plus output caps, rather than a model-specific tokenizer; actual provider usage can exceed an estimate. Crashed requests remain reserved and continue consuming admission capacity. A failed/disconnected stream is retained with a failed status, and browser disconnection does not stop server-side accounting.

Set both `DAO_INPUT_USD_PER_MILLION` and `DAO_OUTPUT_USD_PER_MILLION` to your contractual rates to enable estimated dollar accounting. Without configured rates the UI reports pricing as unconfigured. Costs are calculated in integer micro-USD, rounded upward; cached-token discounts and provider billing adjustments are not modeled. Demo token figures are estimates and have zero monetary cost. Unknown failed calls use a conservative charge at the higher configured token rate.

## Verify and develop

```sh
python -m unittest discover -s tests -v
node --check dao/static/app.js
```

The suite covers Bayesian information value, abstention and reversibility, contradictory evidence, content-bound authorization, relationship conflict persistence and action exclusion, stale/concurrent updates, global budgets, stream failures, durable restart, hash tampering, HTTP origin/CSRF protection, terminal interactions, and adapter parsing with synthetic provider streams. GitHub Actions runs on Windows/Linux with Python 3.11/3.14. Node is only needed for the JavaScript syntax check. A separate Windows workflow builds the console executable, smoke-tests it, and publishes tagged releases.

See [architecture](architecture.md), [relationships](relationships.md), [decision model and derivation](decision-model.md), and [security boundaries](../SECURITY.md).
