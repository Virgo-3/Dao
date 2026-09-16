# Dao

Dao is a local terminal decision workspace for reasoning under uncertainty. It combines an auditable, deterministic decision model with branchable SQLite-backed history, so you can compare actions, explore alternatives, wait for better information, and revisit earlier assumptions without losing the path that got you there.

AI conversation is optional. The core decision engine, state history, branching, merging, and memory search work locally with Python's standard library.

## What Dao does

- **Scores actions deterministically** from your stated probabilities, payoffs, risk preferences, reversibility, and costs.
- **Values waiting for information** with a one-observation Bayesian model and expected value of sample information (EVSI).
- **Keeps immutable checkpoints** in a local SQLite commit DAG.
- **Branches alternatives** so you can explore different assumptions without overwriting the current path.
- **Diffs, restores, and merges state** while preserving history.
- **Searches memory across the entire saved tree**, including sibling branches and historical snapshots, with source provenance.
- **Optionally uses the OpenAI Responses API** for open-ended conversation and explanations.
- **Never executes external actions**. Recording a choice stores intent only.

## Requirements

- Python **3.11+**
- Git, if installing from a clone

Dao has **no third-party runtime dependencies**.

## Installation

Clone the repository and install it in editable mode:

```bash
git clone https://github.com/Virgo-3/Dao.git
cd Dao

python -m venv .venv
```

Activate the virtual environment:

```bash
# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Then install Dao:

```bash
python -m pip install -e .
```

This installs the `dao` command.

## Quick start

Start the terminal workspace:

```bash
dao
```

or:

```bash
python -m dao
```

By default, Dao runs in **demo mode**. The deterministic decision engine is fully available, but open-ended messages return deterministic guidance rather than calling a model.

To print the built-in launch decision as evaluated JSON:

```bash
dao demo
```

Dao stores local state in:

```text
.dao/state.sqlite3
```

Use a different database when you want a separate workspace:

```bash
dao --db path/to/workspace.sqlite3
```

## AI mode

AI mode adds open-ended conversation through the OpenAI Responses API. Set both required environment variables:

```bash
export OPENAI_API_KEY="..."
export DAO_MODEL="<model-name>"

dao --ai
```

PowerShell:

```powershell
$env:OPENAI_API_KEY = "..."
$env:DAO_MODEL = "<model-name>"

dao --ai
```

The model can explain tradeoffs, but it has no tools and cannot execute terminal commands, mutate state directly, or act on external systems.

In AI mode, Dao sends the current question, a limited amount of recent dialogue, current decision context, current notes and analysis, and selected relevant historical excerpts to the OpenAI API.

## Terminal commands

### Conversation

| Command | Description |
| --- | --- |
| `/decide` | Explain the current decision |
| `/remember key=value` | Save context on the current branch |
| `/forget key` | Remove current context |
| `/observe signal` | Record evidence and update beliefs with Bayes' rule |
| `/search words` | Search relevant memories across the full saved tree |
| `/help` | Show command help |
| `/quit` | Exit Dao |

### Workspace

| Command | Description |
| --- | --- |
| `/branches` | List branches and their checkpoints |
| `/branch name` | Fork at the current checkpoint and switch to the new branch |
| `/switch name` | Switch to an existing branch |
| `/history` | Show up to the latest 100 first-parent checkpoints |
| `/state [ref]` | Print the current or referenced snapshot as JSON |
| `/diff ref` | Compare a branch or full checkpoint ID with the current state |
| `/restore ref` | Restore an earlier snapshot as a **new** checkpoint |
| `/merge source` | Three-way merge another branch into the current branch |
| `/model [file.json]` | Print the current decision model, or load one from JSON |
| `/choose action_id` | Record a choice as intent; no external action is performed |
| `/export file.json` | Export the current snapshot to a new JSON file |

You can also select the initial terminal branch from the command line:

```bash
dao --branch experiment
```

The branch must already exist when Dao starts.

## Decision model

Dao evaluates only the assumptions you provide; it does not infer probabilities or payoffs.

For each action, the score is:

```text
expected payoff
- risk_aversion × expected downside
- irreversibility_aversion × (1 - reversibility) × commitment_cost
- reversibility × rollback_cost
```

`reversibility` is a user-supplied degree of recoverability from `0` to `1`. It is a modeling input, not a calibrated probability.

When a `wait` model is present, Dao evaluates one future observation followed by one declared action. Signal likelihoods are interpreted as:

```text
P(signal | state)
```

Dao computes the posterior state probabilities for each possible signal, the best action after each signal, EVSI, waiting cost and discount, and the resulting net option value.

If doing nothing is a real option, include it explicitly as an action.

### Example decision file

Create `decision.json`:

```json
{
  "title": "Launch now, run a pilot, or wait for evidence?",
  "states": [
    {"id": "demand", "probability": 0.5},
    {"id": "no_demand", "probability": 0.5}
  ],
  "actions": [
    {
      "id": "launch",
      "label": "Launch now",
      "payoffs": {"demand": 100, "no_demand": -60},
      "reversibility": 0.1,
      "rollback_cost": 0,
      "commitment_cost": 20
    },
    {
      "id": "pilot",
      "label": "Run a small pilot",
      "payoffs": {"demand": 20, "no_demand": 10},
      "reversibility": 0.9,
      "rollback_cost": 0,
      "commitment_cost": 2
    },
    {
      "id": "hold",
      "label": "Keep the current plan",
      "payoffs": {"demand": 0, "no_demand": 0},
      "reversibility": 1,
      "rollback_cost": 0,
      "commitment_cost": 0
    }
  ],
  "risk_aversion": 0,
  "irreversibility_aversion": 0,
  "wait": {
    "cost": 5,
    "discount": 1,
    "signals": [
      {
        "id": "positive",
        "likelihoods": {"demand": 0.8, "no_demand": 0.2}
      },
      {
        "id": "negative",
        "likelihoods": {"demand": 0.2, "no_demand": 0.8}
      }
    ]
  }
}
```

Load and inspect it inside Dao:

```text
/model decision.json
/decide
```

If the current waiting model contains a possible signal, record it with:

```text
/observe positive
```

Dao updates state probabilities using Bayes' rule and consumes the current waiting model. Add a new observation model if you want to evaluate waiting again.

## Branching and history

Dao's state store behaves like a small content-addressed version graph:

- Every mutation creates an immutable checkpoint.
- Checkpoint IDs are SHA-256 hashes of their contents and metadata.
- Branch writes use an expected-head check to prevent silently overwriting concurrent changes.
- Restoring an old snapshot creates a new checkpoint; it does not delete history.
- Merges use a three-way merge and fail if both sides changed the same incompatible value.
- Lists, including conversation transcripts, are treated atomically during merges to avoid inventing event order.

Example:

```text
/branch pilot-first
/model pilot-model.json
/decide

/switch main
/branch launch-now
/model launch-model.json

/switch main
/diff pilot-first
/merge pilot-first
```

Restoring conversation state cannot undo anything that already happened outside Dao.

## Memory search

Dao searches saved state across **every checkpoint in the selected database**, including:

- messages
- notes
- decisions
- recorded choices
- observations
- sibling branches
- both parents of merges
- content that no longer exists in the current state after a restore or `/forget`

Search is local and lexical. It uses BM25-style ranking implemented with ordinary SQLite tables and Python's standard library; no embedding service or SQLite extension is required.

```text
/search supplier rollback deadline
```

Results include the source checkpoint, JSON Pointer path, branch provenance, and whether the record is present in the current snapshot.

Inspect a result without restoring it:

```text
/state FULL_CHECKPOINT_ID
```

See [`docs/memory-search.md`](docs/memory-search.md) for ranking, limits, provenance, indexing behavior, and privacy details.

### Privacy note

Branches are alternatives inside one trusted workspace; **they are not privacy boundaries**.

A forgotten value or restored-away snapshot remains in immutable history and can still be found by memory search. In AI mode, selected excerpts from any branch or historical checkpoint in the same database may be sent to the OpenAI API when relevant.

Use separate database files for workspaces that should not share memory.

The SQLite database is plaintext and protected only by your operating system's file permissions. Keep it out of source control.

## Project structure

```text
dao/
├── __main__.py    # CLI entry point
├── agent.py       # Conversation and state-mutation orchestration
├── decision.py    # Deterministic decision model and Bayesian waiting logic
├── examples.py    # Built-in launch scenario and initial state
├── memory.py      # Tree-wide local memory indexing and search
├── provider.py    # Optional OpenAI Responses API adapter
├── store.py       # Immutable SQLite commit DAG, branches, diffs, restores, merges
└── terminal.py    # Interactive terminal and workspace commands

docs/
└── memory-search.md

tests/
├── test_agent.py
├── test_cli.py
├── test_decision.py
├── test_memory.py
├── test_memory_unicode.py
├── test_provider.py
├── test_store.py
└── test_terminal.py
```

## Development and tests

Run the test suite:

```bash
python -m unittest discover -s tests -v
```

Compile-check the package and tests:

```bash
python -m compileall -q dao tests
```

The GitHub Actions workflow runs both commands on Linux and Windows with Python 3.11 and 3.14.

## Security

Dao is designed as a **local, single-user terminal workspace**. It does not expose an HTTP server or inbound network listener.

Important boundaries include:

- model output is treated as untrusted text
- AI output cannot execute tools or authorize state changes
- terminal control characters are escaped before display
- imported decision files are size-bounded and schema-validated
- snapshot exports never overwrite existing files
- branch writes use atomic expected-head checks
- search uses immutable snapshot provenance
- no telemetry is sent by Dao

Read [`SECURITY.md`](SECURITY.md) before changing provider, storage, terminal, import/export, or memory-search behavior.

## Backups

The default database is excluded by `.gitignore`.

Stop Dao before copying the SQLite database for backup. `/export` saves only the current snapshot; it is **not** a full branch-history backup.

## Version

Current package version: **0.1.0**

## License

This repository currently does not include a `LICENSE` file. Add or consult an explicit license before assuming reuse or redistribution rights.
