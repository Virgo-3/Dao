# Talking with Dao

Dao opens on the conversation. You can state a decision, consider another option, correct an assumption, ask for the original reasoning, and explore another direction without commands or forms. Routine semantic notes keep the original message passages attached.

## Try it

Start the browser with `python -m dao`, or the terminal with `python -m dao --terminal`. Send these messages one at a time:

```text
We chose SQLite because this must work offline.
Maybe Postgres.
Actually, multiple people need to write at once.
Why did we decide that?
Let's explore another direction
Go back to main
```

The first message records a decision and its reason, joined by a dependency. “Maybe Postgres” records an option under consideration. The correction adds an updated assumption, preserves the original reason, and points out the related decision to revisit. It does not prove that the requirements are incompatible or silently replace the choice. Recall returns the recorded SQLite decision and its offline reason.

“Let's explore another direction” creates and switches to `alternative`, or `alternative-2` and so on if needed. “Go back to main” continues main's own saved context. You can keep thinking in either branch independently.

These common phrases work in the offline demo without a model request. The offline fallback for open-ended messages is still a short canned response; it cannot provide general AI reasoning. Enable live AI as described in the [README](../README.md) for model-backed conversation. Provider integration tests use synthetic streams; live inference and billing are not verified by offline tests.

## Useful phrases

| Say | What Dao does locally |
| --- | --- |
| “We chose X because Y” or “We're using X because Y” | Records an affirmative decision and its reason. |
| “Maybe X” or “I'm considering X” | Records a tentative option without treating it as a decision. |
| “Remember that…” | Saves a cited semantic note, preserving uncertainty in the statement. |
| “Actually, …” | Records an updated assumption and preserves earlier reasoning. |
| “Why did we decide that?” | Recalls the latest recorded settled decision and its dependency reasons. |
| “What do you remember?” | Lists recent semantic notes with their recorded stances, plus saved memory notes. |
| “Let's explore another direction” | Creates a safely named alternative and switches to it. |
| “Create a branch called experiment” | Creates and switches to that new branch, if its name is available. |
| “Switch to experiment” or “Go back to main” | Continues an existing branch's own saved context. |
| “Keep that” or “Reject that suggestion” | Reviews a pending advanced suggestion when exactly one is unambiguous. |

Local recognition covers these patterns, rather than every paraphrase. Quoted or hypothetical text does not trigger local actions. Live AI can handle broader conversation and capture useful routine context through its bounded memory tool. When several advanced suggestions are pending, Dao lists them by number; say “Keep suggestion 2” or “Reject suggestion 2” to select one. The optional proposal details and terminal commands can also review a specific ID. A suggestion based on an older graph needs to be rejected and staged again.

## What is remembered

Semantic notes distinguish `mentioned`, `believed`, `considering`, and `decided`. A passing mention or tentative option is not a commitment. Routine capture records new nodes and connections cited only to the latest user message. It does not set belief probabilities, record reported observations, change action scopes or severe conflicts, resolve conflicts, or perform external actions.

Reasons and corrections remain inspectable. A `depends_on` link says its source depends on its target. A correction can identify earlier dependent decisions worth reconsidering, but does not prove a contradiction or invalidate them automatically. A replacement link requires explicit support, such as wording that names the prior assumption being replaced. Recorded conditions are notes to revisit, not automatic monitors.

Routine memory and recall activities are saved with completed assistant replies. Their source links let you inspect the original passages. Failed turns keep a labeled partial reply and usage evidence, while their graph and proposal changes are rolled back; no successful activity is announced for an uncommitted change. Explicit `/remember key=value` memory remains available separately for users who want it.

## History and details

The browser keeps **History** and **Details** closed initially. History exposes branches and revisions. Details exposes source passages and optional numeric decisions, relationship review, audits, usage, export, and integrity checks. Advanced JSON editors and relationship diagnostics stay collapsed until needed. The terminal offers the same conversation behavior plus [optional commands](terminal.md).

A new alternative begins from the completed source turn. A switch to an existing branch keeps that branch's graph, memory, and other semantic state; only the control message and reply are appended there. Finalizing the source reply and destination navigation is atomic. This prevents a source branch's notes from overwriting the destination. Each branch remains available in History, and restoring a reachable checkpoint creates a new revision while retaining history and usage.

Advanced changes can still use explicit [structure proposals](structure.md), [relationship operations](relationships.md), and [evidence review](../SECURITY.md). Those tools are available when you need their precise controls; ordinary remembering and recalling do not require them.
