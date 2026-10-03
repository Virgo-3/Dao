# Working on Dao

Use codebase-memory-mcp graph tools first for structural code discovery:
`search_graph`, `trace_path`, `get_code_snippet`, `check_index_coverage`,
`query_graph`, and `get_architecture`. At session start, confirm the nearest
project and graph generation with `list_projects` or `index_status`; index this
repository if no project matches. Default to Tier 2 task-directed verification.

Check coverage for every file relied upon. Read exact source for stale, skipped,
excluded, partial, pending, or unknown coverage. A clean report is best-effort
evidence, not proof of completeness. Paginate relevant results. Use `rg` for
literal strings, configuration, non-code files, and insufficient graph results.

Before delegating, query graph and coverage in the parent and pass project,
generation, tier, scope, symbols, paths, pagination, traces, coverage gaps, source
fallback, and unresolved questions. A child without MCP access must use supplied
evidence and exact source without claiming graph access.

Keep state writes atomic and guarded by expected heads. Preserve global usage
and audit history across branch operations. Never give provider tools authority
to adjudicate their own actions. Numeric decision assumptions and USD price
estimates must remain explicit and inspectable. Do not commit credentials or
runtime databases.

Validation: `python -m pytest -q`, `python -m ruff check .`,
`python examples/demo.py`, and `python -m build` after installing `.[dev,mcp,openai]`.
