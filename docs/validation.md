# Release validation

## Objective lifecycle validation, 2026-10-02

Validated locally on Windows with Python 3.14.6. The complete suite passed:
195 tests and 78 subtests. The 24 new work and interface cases cover durable
objective revisions, independent review, exact completion predicates, waiting
for named evidence, stale approvals, concurrent provider claims, attributed
usage, uncertain charges, saved replies, operator recovery, external dispatch
and reconciliation, restart after an artifact was written, and rejection of a
changed executor target after approval. Real CLI and stdio MCP round trips
exercise objective creation, advancement, review, resources, and integrity
verification. Default MCP tools cannot adjudicate or submit observations.

`python -m ruff check .`, `python -m pip check`, and all three demonstrations
(`examples/demo.py`, `examples/langgraph_demo.py`, and `examples/work_demo.py`)
passed. The work demonstration also passed with `python -S` and `PYTHONPATH=src`,
without optional dependencies. Isolated source distribution and wheel builds
passed and include the coordinator and executor modules; the source distribution
also includes the work guide, demonstration, and tests. No live OpenAI request
was made. CI now runs the work demonstration in its existing Windows/Linux and
Python 3.11/3.14 matrix; these local results do not claim remote CI completion.

## Previous release validation, 2026-10-01

Validated locally on Windows with Python 3.14 on 2026-10-01.

- `python -m pytest -q`: 171 tests and 78 subtests passed, covering state DAG operations, conflict
  rejection, atomic rollback, immutable history and ref journal replay,
  accounting concurrency, uncertain charges, Bayesian calculations, stale
  approvals, altered/reordered audit projections, provider tool boundaries,
  branchable relationship beliefs, retry-safe observation records, scoped
  coherence, bounded temporal planning, prospective/retrospective review, and
  plain-text terminal chat with opt-in structured output. Projection tests cover
  branch-specific cited claims, bounded omissions, global accounting and audit
  status, deterministic reads on an existing store, SQLite `query_only` mode,
  and a `/projection` chat turn without a provider call or usage charge.
- A real stdio MCP subprocess and client completed initialization, tool discovery,
  state read, projection tool and resource reads, branch creation, resource
  discovery, evidence authorization, observation replay, temporal local action
  execution, and integrity verification. CLI and MCP projection checks compared
  the branch head and event journal before and after projection reads.
  The default tool set excluded adjudication and observation submission.
- `python -m ruff check .` passed. `python examples/demo.py` passed and verified
  branch isolation, adjudication, merge, restoration, and event-chain integrity.
- Source distribution and wheel builds succeeded with `python -m build`,
  using isolated build environments with setuptools 84.0.0. Both artifacts
  include the exact probability helper. The
  sdist includes examples, documentation, and tests. `requirements-tested.txt` records the tested optional
  dependency environment; platform-specific dependencies may differ on Linux.
- The OpenAI adapter was verified with mocked Responses output, including cached
  token costs, storage settings, tools, timeout accounting, and context races.
  Live OpenAI requests were not made.

The LangGraph integration was validated with LangGraph 1.2.12 and
`langgraph-checkpoint-sqlite` 3.1.1. Its 42 new cases cover native graph
invocation and streaming, fresh-input and JSON schemas, separate trusted
adjudication, forged resume/checkpoint display data, wait/abstain/rejection,
stale heads, immutable receipts, rollback, concurrent operation claims,
incomplete provider work, persistent checkpoint resume in a new subprocess,
and retry-safe CLI and real MCP stdio conversation. Recovery errors expose the
operation ID. A provider failure keeps unknown usage liability; a checkpoint
failure after a completed chat can recover its saved reply without billing again.
The restart demonstration and native async chat/action smoke checks passed.
`python -m pip check` reported no broken requirements, and a `python -S` offline
chat confirmed that the direct runtime still works with only the standard library.
The package build includes the new graph, runtime, and journal modules plus the
guide and demonstration in the source distribution. Minimum dependency versions
have not been independently exercised locally.

The mathematical corrections for `dao-decision/2` and `dao-temporal/2` add
regressions for underflowing probability products, loss and resource cap debits,
fixed-maximum tie preferences, thresholded waiting continuations, potential EVSI,
atomic rejection of beliefs that cannot preserve positive support in stored
floats, and exact decimal token costs under altered precision, exponent limits,
and traps. Temporal memoization is checked for independent mutable branches.
Independent reference checks passed for 2,000 static decision models, 800
temporal models (6,513 policy nodes), 2,910 relationship assertions, and 3,000
provider pricing cases. Existing immutable audit records retain their original
model versions; new evaluations use version 2.

CI is configured for Windows and Linux, Python 3.11 and 3.14. The local results
above do not assert that remote CI or other platforms have completed.
