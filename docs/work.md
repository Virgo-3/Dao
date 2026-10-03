# One advancing work lifecycle

Dao accepts an objective and carries it forward until it needs approval,
evidence, operator input, or verified completion. `Coordinator` connects Agent,
Audit, Store, Ledger, and WorkflowJournal through one work identity and durable
next transition. The core coordinator uses only the standard library.

## Start and review an objective

For model-driven work, configure the OpenAI provider as in the README. Create
`success.json` containing `{"preference":"concise"}`, then:

```sh
dao run "Remember that I prefer concise answers" --success success.json --provider openai
dao work inspect WORK_ID
dao work adjudicate WORK_ID approved --actor operator --reason "Reviewed the exact memory change"
dao work history WORK_ID
dao verify
```

`run` starts bounded progress until it reaches a stable boundary. `--json`
returns the full record. `--work-id ID` makes creation retry-safe; reusing an ID
for different inputs is rejected. `--no-advance` creates work without calling a
provider. Offline conversation is a scripted demonstration, not a planner;
use an explicit plan or `python examples/work_demo.py` for offline progress.

`work adjudicate` combines trusted review and the permitted next transition,
without copying a proposal base into an execution command. Existing
`dao adjudicate` remains supported: `work advance` reads that independent ruling.
Missing or deferred approval does nothing; rejection requests input. Neither
the provider nor default MCP tools can adjudicate.

`dao work advance WORK_ID --until-blocked --provider openai` continues work
across several steps. Plain `advance` performs at most one provider turn or
approved effect. Polling review, waiting, completion, or an uncertain dispatch
does not call the provider. Work records a provider-turn limit (default 20);
each turn retains Agent's four-call limit and global budget admission. Explicit
operator plans do not consume that allowance. No daemon or network listener is
started: CLI invocations, SDK callers, or an MCP host drive progress.

## Decisions, evidence, and waiting

An operator can supply a calibrated decision problem and exact memory patch:

```sh
dao run "Review test results before proceeding" --no-advance --work-id review-tests
dao work plan review-tests examples/waiting.json patch.json --wait-for wait.json
dao work observe review-tests ci-run-123 test-result CI result.json --actor operator
dao work advance review-tests --until-blocked --provider openai
```

For a wait recommendation, `wait.json` must contain `source` and `topic`, for
example `{"source":"CI","topic":"test-result"}`, and may contain a timezone-aware
ISO-8601 `deadline`. A matching trusted observation wakes reassessment; it never
approves or executes the waiting patch. A deadline makes work ready on the next
invocation without fabricating a signal. Active evidence collection needs its
own reviewed external effect; a waiting condition grants no collection authority.

Observation IDs are scoped to work and retry-safe; changed evidence under an
existing ID is rejected. Observations remain source claims. New evidence also
advances the branch, invalidating previously reviewed bases. Numerical assumptions
remain explicit: coordination does not calibrate the decision model or turn
provider estimates into facts. Provider-created memory proposals retain the
explicit-request preference assumption described in the README.

## Forward history and success

Transitions append numbered work revisions and matching events. Work history,
evidence, approvals, execution receipts, and spending live outside branch
snapshots. Snapshot restoration cannot erase or duplicate this operational
history. Branches remain deliberation contexts; each work record selects the
branch on which its next plan is based.

Mutations accept `expected_revision`; plans also accept `expected_head`.
Transactions reject stale revisions and commit a local effect together with
its work outcome. Concurrent processes cannot claim the same provider turn
twice. The work view links input, proposal, operation, and usage IDs, plus
recorded and current heads. `dao project` includes bounded, sourced objective
summaries from the same database read cut.

Memory success predicates compare canonical JSON exactly, distinguishing `true`
from `1`. Completion is certified at a recorded head. Later restoration can
make `success_currently_satisfied` false while preserving historical completion.
A model saying "done" cannot complete work. Without machine predicates, a
trusted operator must certify the outcome:

```sh
dao work complete WORK_ID evidence.json --actor operator --reason "Verified the outcome"
```

## Provider recovery

Work advancement and a durable operation claim commit together before calling
Agent outside the SQLite transaction. Saved replies can be adopted on restart.
Unfinished and failed claims are never automatically redispatched. Inspect the
receipt, branch, proposals, and attributed usage; reconcile pending or unknown
usage with the existing ledger API before a new attempt.

Once the earlier worker has stopped and its outcome has been inspected:

```sh
dao work recover WORK_ID recovery.json --actor operator --reason "Inspected state and reconciled billing"
```

`recovery.json` must include `"dispatch_quiescent":true` and supporting evidence.
This is a trusted operator assertion that no earlier worker is still dispatching.
Recovery preserves the old claim and permits a new operation identity on a
later advance. A reply lost before its durable receipt requires inspection;
the system does not promise exactly-once provider calls.

## External executors

An external proposal binds an executor name, its stable configuration, exact arguments, preconditions,
USD reservation, and reviewed branch base. Dispatch authorization and budget
reservation commit atomically. Execution then runs outside the transaction.
After dispatch is claimed, a ruling cannot revoke an action that may already
have happened; its physical outcome must be reconciled. Changes to logical
state after dispatch do not erase that outcome.

Operators declare a non-secret executor `binding` containing its version,
endpoint, account, and behavior-relevant configuration identifiers, and install
`Executor.execute(effect, key)` and
`Executor.reconcile(effect, key)`. Executors enforce service preconditions
atomically where supported. The stable key is recorded before dispatch.
Reconciliation inspects the original attempt without sending it again.
Planning includes the installed binding in the reviewed proposal. Dispatch and
reconciliation reject a different binding; changing an artifact directory after
approval cannot redirect the reviewed effect.
Receipts contain exactly `status`, nonempty `evidence`, and `cost_usd`.
Statuses are `succeeded`, `not_executed`, and `unknown`. Known costs settle
against the global ledger, including overruns. Unknown outcomes retain their
liability. Confirmed non-execution requires a new plan and approval for any retry.

The bundled `ArtifactExecutor` proves a real zero-cost effect: exclusively create
one key-named JSON file in an operator-selected directory. It cannot overwrite
an artifact. A partial or conflicting file remains unknown. SDK example:

```python
from dao.agent import memory_problem
from dao.executors import ArtifactExecutor
from dao.store import Store
from dao.work import Coordinator

with Store(".dao/state.db") as store:
    work = Coordinator(store, executors={"artifact": ArtifactExecutor("artifacts")})
    item = work.create("Produce an inspected report artifact")
    item = work.plan(item["id"], memory_problem("explicit artifact request"), effect={
        "executor": "artifact", "arguments": {"content": {"report": "reviewed content"}},
        "preconditions": {"absent": True}, "reservation_usd": "0",
    })
    item = work.adjudicate(item["id"], "approved", "operator", "Reviewed exact artifact")
```

The example's one-scenario utility is an explicit preference assumption. For
CLI use, `work plan WORK_ID PROBLEM.json --effect EFFECT.json`, `work advance`,
and `work adjudicate` accept `--artifact-dir DIRECTORY`. Otherwise CLI/MCP
install no external executor. The provider receives no arbitrary shell or
network-action tool. Domain executors still need actual service identities,
permissions, idempotency, reconciliation, and compensation contracts.

After a crash or ambiguous outcome, normal advancement stops at `executing` or
`reconciling`. Once dispatch is quiescent, use SDK `reconcile` or:

```sh
dao work reconcile WORK_ID recovery.json --artifact-dir artifacts --actor operator --reason "Stopped worker; inspect original dispatch"
```

Reconciliation never calls `execute` again. Compensation is a new reviewed
effect. External success does not certify an objective without success predicates.

## MCP, scheduling, and demonstrations

Default tools expose `start_work`, `read_work`, `list_work`, `work_history`,
`plan_work`, `advance_work`, and `run_work`; the resource is
`dao://work/{work_id}`. `--allow-adjudication` adds `adjudicate_work`,
`complete_work`, and `recover_work`. `--allow-observation` adds `observe_work`.
These are trusted local host capabilities, not authenticated remote identities.

`python examples/work_demo.py` demonstrates local review, restart, restoration,
and a crash after a real artifact effect followed by reconciliation. Existing
direct and LangGraph conversation interfaces remain supported. A scheduler or
another agent can drive the same Coordinator methods; its checkpoint or resume
data has no approval authority. Work remains a single-operator local foundation.
