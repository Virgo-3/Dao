# Dao

A local system that advances objectives through evidence, decisions, independent
adjudication, execution, and verified outcomes. One durable work record connects
the conversational agent, branchable state, approvals, operation receipts, and
global usage accounting. Decisions compare acting, abstaining, and waiting for
evidence. Approval binds a concrete effect to the exact state reviewed; spending
and physical outcomes survive branch restoration. CLI and local MCP expose the
same lifecycle. Optional LangGraph workflows retain their scheduling,
streaming, and interrupt/resume roles while Dao owns authority and accounting.

## License

This distribution is proprietary. Access to the repository or an installed
package does not grant permission to use, copy, modify, distribute, host, or
commercialize Dao. See [LICENSE](LICENSE) for the current terms and
[NOTICE](NOTICE) for the MIT license that remains applicable to earlier
releases through commit `ab492d4`. For permission to use or commercialize a
current release, contact the applicable rights holder(s) through the
[repository](https://github.com/Virgo-3/Dao).

## Run

Python 3.11 or later. The core and offline demonstration use only the standard
library. Install optional extras for MCP, OpenAI, and development:

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev,mcp,openai]"
dao init
dao chat
python examples/demo.py
dao decide examples/waiting.json
```

To run the same conversation interface through persistent LangGraph checkpoints:

```sh
python -m pip install -e ".[langgraph]"
dao chat --runtime langgraph --checkpoint-db .dao/checkpoints.db
python examples/langgraph_demo.py
```

The [LangGraph guide](docs/langgraph.md) covers native compiled graph APIs,
independent action review, checkpoint recovery, and CLI/MCP integration. The
offline demo closes and reopens both databases before resuming an approved
memory action and verifies that replay does not duplicate conversation usage.

Offline mode is a scripted demonstration. For AI conversation, configure the
environment variables in [.env.example](.env.example), then run
`dao chat --provider openai`. The template is not loaded automatically. Choose
your model and verify your account's rates explicitly. There is no API key in
this repository. The provider uses the [OpenAI Responses API](https://developers.openai.com/api/docs/libraries),
disables automatic retries and remote response storage, and reports measured
token counts with costs estimated from configured prices.

`dao chat` displays replies as a conversation. Use `dao chat --json "MESSAGE"`
when you need the reply text, state head, and proposal metadata as
structured output. Other CLI commands continue to return JSON.

The model can read memory and propose memory changes through function calls.
It cannot approve its own proposals. Conversation context comes from the
selected branch. A single turn permits four provider calls and one memory proposal.

## Advance an objective

```sh
dao run "Remember that I prefer concise answers" --provider openai --success success.json
dao work inspect WORK_ID
dao work adjudicate WORK_ID approved --actor operator --reason "Reviewed the exact proposal"
dao work history WORK_ID
python examples/work_demo.py
```

Here `success.json` contains `{"preference":"concise"}`. Work progresses until it
needs approval, evidence, operator input, or verified completion. A model saying
"done" does not certify success. `dao run --no-advance` starts work without a
provider call; explicit decision models can then be supplied with `dao work plan`.
Offline mode remains a scripted conversation, so the work demonstration uses
explicit plans. Polling approval or waiting does not spend provider tokens.

The [work guide](docs/work.md) covers source-specific wakeup conditions,
revision guards, attributed usage, completion evidence, recovery after restart,
and operator-installed external executors. The coordinator's history advances
even when branch snapshots are restored. Memory changes and work outcomes commit
atomically. External effects are claimed before dispatch and reconciled after
ambiguous outcomes; they are never automatically resent.

## Branch state and review an action

```sh
dao branch experiment
dao chat --branch experiment '/remember goal "preserve options"'
dao proposals
```

Copy the proposal `id` and `base` returned by the last command:

```sh
dao adjudicate PROPOSAL_ID approved --actor operator --reason "Reviewed exact memory patch"
dao execute PROPOSAL_ID --expected-head PROPOSAL_BASE
dao diff main experiment
dao state --branch main
dao merge experiment --expected-head MAIN_HEAD
dao log
dao revert PREVIOUS_COMMIT --expected-head CURRENT_HEAD
dao verify
dao usage
```

`revert` restores the named snapshot as a **new commit**; it never deletes history
or refunds usage. `merge` uses a common ancestor and rejects conflicts, including
multiple merge bases. Arrays such as conversation messages are merged atomically.
Every state write uses an expected head to prevent lost updates. State lives in
`.dao/state.db`; use `--db PATH` before the command for an independent store.
For an existing store, pass its current file with `--db PATH` or set `DAO_DB`;
you can also move that file to the new default path to keep its history and ledger.

## Narrated state projection

`dao project --branch main` describes Dao's recorded state in natural language.
It reads the selected branch's snapshot together with the branch references,
audit records, observations, and global usage ledger at one database cut. Run
`dao project --json` for the underlying claim references, counts, and omissions.
MCP hosts can call `project_state` or read `dao://projection/{branch}`. In chat,
`/projection` speaks the same narrative; the request and reply are recorded as
normal conversation messages. `dao chat --json "/projection"` includes the
projection's exact source references from that same chat turn. Its
`projection.head` identifies the read cut; the reply's top-level `head` is the
new head after Dao records its answer.

Projection computation is deterministic and read-only on an existing store: it
makes no model call, spends no tokens, and cannot adjudicate or execute a
proposal. Opening a new store still initializes its default database. Its branch
head and event sequence identify the version described. The narrative indexes
recorded facts. It neither replaces the underlying records nor claims to know
the model's internal state. Where details are bounded or data is unavailable, the
coverage field says so explicitly.

## Audit as adjudication

The lifecycle is `propose → adjudicate → execute`. A proposal records the decision
inputs, engine result, exact memory patch, and branch head. Rulings are immutable
records with actor, reason, evidence, and verdict (`approved`, `rejected`, or
`deferred`). The latest ruling governs execution; completed executions cannot be
ruled on again. Execution is atomic, idempotent, and rejects a changed branch head.
Wait and abstain recommendations cannot be approved for immediate action.

The `/remember` convenience command uses a declared one-scenario preference model
for an explicit local memory request. Its utility of 1 is an assumption, not an
AI confidence estimate. Use `dao propose PROBLEM.json PATCH.json --expected-head
HEAD` to supply an uncertainty model yourself. Model numerical claims require
external calibration and review.

## Decision engine

Score = expected net utility − weighted expected downside − irreversibility
penalty − expected rollback burden. A hard worst-loss limit can exclude an
action. Waiting uses a user-supplied, exhaustive signal likelihood model to
compute posterior choices and expected value of sample information. Waiting
scores the actual thresholded posterior recommendations, then subtracts evidence
and delay cost. Abstention has score 0; ties preserve options and are measured
against a fixed maximum. Exact internal probability support prevents tiny
possible losses from disappearing from the modeled loss cap.

The [decision model](docs/decision-model.md) specifies the formula, validation,
tie rules, and one-step limitations. In [waiting.json](examples/waiting.json),
acting now scores 12, information raises expected value to 38.4, EVSI is 26.4,
and waiting scores 35.4 after cost 3. Wolfram independently verified this example;
the calculation and official integration sources are in [provenance](docs/provenance.md).
Utility units and USD accounting are separate.

The [Telos-inspired experience layer](docs/telos.md) describes how durable
observations, branchable relationship beliefs, and later outcome reviews can
inform decisions without changing the authority of adjudication. Neutrality,
uncertainty, and choosing to wait have distinct meanings. A separate bounded
temporal planner can simulate how actions change relationships over several
decisions. Its first selected `act` can be proposed for an audited local memory
patch; the planner's output alone grants no execution authority.

## Accounting

Each attempt reserves budget before calling a provider. Settlements store
provider token counts, model, branch, price metadata, and estimated USD cost.
Configured finite-decimal token costs are computed exactly, independently of
the caller's decimal precision. Amounts are rounded upward to integer microUSD.
A timeout retains an `unknown`
reservation until trusted reconciliation. Actual cost overruns are recorded and
block new reservations rather than discarding spend. No branch can reset the
global ledger. The initial per-store budget is $10; change it explicitly with
`dao budget AMOUNT_USD`.

Reservations are admission estimates, not a provider billing guarantee. Accurate
prices and sufficient reservations are the operator's responsibility. Use
`dao resolve-usage REQUEST_ID --input-tokens N --output-tokens N --cost-usd COST
--model MODEL` after verifying an uncertain request with the provider. Ledger
entry IDs are retry-safe; orchestration assigns a new ID only for a new attempt.

## MCP

The [official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/v1/) is pinned
to the v1 maintenance line (`mcp>=1.28,<2`) for explicit protocol compatibility.
This release serves **local stdio**, with no network listener.

```json
{
  "mcpServers": {
    "dao": {
      "command": "/absolute/path/to/.venv/bin/dao-mcp",
      "args": ["--db", "/absolute/path/to/dao-state.db"]
    }
  }
}
```

On Windows use the absolute `.venv\\Scripts\\dao-mcp.exe` path. Tools cover
conversation, branches, history, diff/merge/revert, decisions, proposals,
execution, usage, and integrity. Resources include `dao://branches`,
`dao://state/{branch}`, `dao://projection/{branch}`, and `dao://audit`.
Update existing MCP host configurations to the new command and resource URIs.

Approval is absent from the default MCP tool set. A trusted operator can
adjudicate through the CLI. `dao-mcp --allow-adjudication` deliberately grants
that capability to the connected host; use it only for a trusted operator host.
`--provider openai` enables AI conversation with the same explicit configuration.
Add `--runtime langgraph --checkpoint-db /absolute/path/to/checkpoints.db` to the
MCP server arguments to schedule conversation through the same graph runtime.
This does not grant the host adjudication authority.

Telos tools let an MCP host register relationships, evaluate explicit
three-state beliefs, inspect scoped coherence with `relationship_coherence`,
simulate bounded plans with `plan_relationship`, propose
relationship actions or evidence collection, record observations, and update
branch beliefs. Observation submission requires
`--allow-observation`; adjudication and retrospective review require
`--allow-adjudication`. See the
[experience guide](docs/telos.md#use-the-local-mcp-tools) for tool order,
source-trust boundary, and modeling limits.

## Boundaries and validation

This is a local, single-operator foundation. Built-in effects include memory
patches and an explicitly installed, confined filesystem artifact executor.
The external executor contract binds reviewed arguments, preconditions, budget,
and a durable dispatch key. Other service actions still need dedicated
executors, identities, permissions, idempotency, reconciliation, and compensation.
The state database is not encrypted; protect it with OS permissions. Hashes and
append-only triggers provide tamper evidence within the local trust boundary,
not a signature or protection against an administrator rewriting the database.

See [architecture](docs/architecture.md) for invariants and extension points.
The [validation record](docs/validation.md) lists the release checks and limits.
Tests cover Bayesian calculations, branch conflicts, stale approvals, atomic
rollback, usage races, unknown costs, model tool boundaries, and a real MCP stdio
client/server round trip. The LangGraph checks cover persistent interrupts,
independent rulings, receipt replay, and crash recovery boundaries. Live OpenAI
calls require credentials and are not part of offline CI.

```sh
python -m pytest -q
python -m ruff check .
python -m build
```
