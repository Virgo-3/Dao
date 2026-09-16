# Security policy and boundaries

Dao is a local, single-user terminal decision workspace. It has no browser interface, HTTP server, or inbound network listener. Multi-user hosting, remote exposure, authentication between different OS users, and sandboxing local processes are outside this release's scope.

## Properties to preserve

- Never expose API keys or provider response bodies on failure. Keep the provider endpoint fixed, use HTTPS, and never forward authorization through redirects. Conversation content is displayed only in the user's terminal, not background logs.
- Model output is untrusted plain text. It has no tool execution capability and cannot authorize state changes or external actions. Escape terminal control characters before displaying replies, history, errors, and other untrusted text. Only user input enters the terminal command dispatcher.
- Retrieved memories are untrusted historical data, with source checkpoints and paths. They may describe alternate or superseded state; current decision assumptions and notes remain authoritative. Search must not mutate branches or replay commands from indexed text. Bind all search terms as SQL parameters and verify returned content against immutable source snapshots.
- Import decision assumptions only from explicitly named local JSON files, bound the input size, and validate before committing. Snapshot exports create new files exclusively; they never overwrite an existing file or state database. Never execute file content or launch a shell to process commands.
- All branch writes use an expected head checked in the same SQLite transaction as the commit. Merge conflicts and validation failures leave branches unchanged.
- Validate finite numbers, probabilities, schema, sizes, and branch names at their boundaries. Never interpret branch names as paths, SQL fragments, or shell commands.
- State restore adds a commit; it does not delete history or undo external effects. Database hashes are integrity checks, not signatures. The database and local machine are trusted assets.
- No telemetry is sent by Dao. Search and ranking run locally. In AI mode, current branch context and relevant excerpts from any branch or historical snapshot in the database are sent to OpenAI, as described in [Memory search](docs/memory-search.md). Branches are not privacy boundaries; forgotten or restored-away content remains searchable. Research plugins are development tools only.

Conversation history may contain sensitive data. The SQLite database is plaintext and subject to the operating system's access permissions. Exclude it from source control. Stop Dao before copying the database for backup; do not assume exporting the current snapshot includes branch history.

## Reporting

Use the repository's private vulnerability reporting feature if enabled. Avoid posting credentials or private conversation content in public issues. If a private channel is unavailable, open an issue requesting contact without exploit details or sensitive data.
