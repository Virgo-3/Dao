# Memory search

Dao searches relevant memories throughout the entire saved conversation tree. It no longer relies on a fixed 30-message lookback.

## Use it

Start the terminal with `python -m dao` or `dao` after installation. Search works locally in both demo and AI modes:

```text
/search supplier rollback deadline
```

Matches include their source checkpoint, JSON Pointer path, the branches whose history contains that source, and whether the record is present in the current snapshot. Inspect a source without restoring it:

```text
/state FULL_CHECKPOINT_ID
```

For open-ended AI conversation, configure `OPENAI_API_KEY` and `DAO_MODEL`, then run `python -m dao --ai`. Every open-ended AI reply automatically searches the tree using the current question. Explicit `/search` commands never call the provider or change branch state.

## Scope and provenance

The local index covers messages, notes, decisions, recorded choices, observations, and other saved state in **every checkpoint** in the selected database. This includes sibling branches, both parents of merges, and content no longer present after a restore or `/forget`. There is no age, message-count, or history-page cutoff for the search corpus.

Shared records inherited by many checkpoints are indexed once. A source identifies a verifiable saved copy of the record; it is not a claim that the record is current on every listed branch. Results explicitly distinguish records in the current state from historical or alternate state. Search does not merge branches, update assumptions, or create a checkpoint.

## Ranking and context limits

Ranking uses local BM25-style word relevance with Unicode/case normalization and common English stop words removed. Visually identical composed and decomposed accents match, as do compatibility forms such as fullwidth letters and ligatures. Accents are preserved: `café` and `cafe` remain distinct words. Excerpts keep the original text and source positions. It uses ordinary SQLite tables and Python's standard library. No embeddings, SQLite extensions, search service, or extra account are needed. Matching is lexical: try different keywords for synonyms or concepts expressed differently.

The corpus is unrestricted by age, but the returned context is bounded:

- Up to 8 ranked matches by default.
- Up to 1,200 characters per excerpt, centered near matching terms.
- Up to 16,000 characters of serialized result data.
- Up to 64 distinct non-stop words per query.
- Up to 8 source branch names per match, plus the total branch count.

Truncation is reported. Narrow a query for more specific results. The model also receives up to 6,000 serialized characters of immediately preceding dialogue for continuity, with the current question kept intact. This short dialogue supplement does not limit searchable history.

## Existing databases

Existing databases are indexed automatically on their first search. When search tokenization changes, the next search rebuilds older indexes automatically from validated checkpoints. Rebuilding is transactional: a failure preserves the previous cache for a retry. Later searches add only previously unindexed checkpoints, including commits written by another Dao process. The first search or a rebuild on a large history may take longer.

The index is a derived cache in the same SQLite database. Immutable snapshots and their IDs stay unchanged, and selected search results are checked against their source snapshots before use. Keep the database out of source control and stop Dao before copying it for backup.

## AI context and privacy

Search and ranking run locally. In AI mode, Dao sends the current question, a small amount of preceding dialogue, current decision/notes/analysis, and selected historical excerpts to the OpenAI Responses API. Retrieved excerpts can come from **any branch or historical snapshot in this database**.

Branches represent alternatives within one trusted workspace, not privacy boundaries. Restoring or forgetting a value does not erase its historical copies or remove them from search. Use separate databases for workspaces that should not share memory.

Retrieved text is untrusted historical data. It cannot execute commands, authorize actions, or automatically update the active branch. Current decision assumptions and notes remain authoritative. The provider is instructed to identify source checkpoints when using retrieved memories.
