# Relationships, uncertainty, and conflicts

Dao saves a directed relationship graph in each branch's revision history. Nodes identify people, goals, claims, or actions. A relation connects two nodes and describes an effect, evidence support, compatibility, or resource competition. You supply this model and its assumptions.

The graph separates a relation's definition, its assessed belief, reported observations, and permission to resolve a conflict. A belief assigns probabilities to positive, neutral, and negative outcomes, or is explicitly unknown. An observation records a reported change. Evidence and observations, including text imported from documents, are data supplied for review.

## Use the browser or terminal

Open the **Relationships** tab. Its summary shows conditional coherence, assessed coverage, unknown weight, and unresolved severe conflicts. Expand a conflict to inspect its source, history, and action scope. The graph and transition estimates are available below the conflict list.

Example buttons load one operation at a time. Review each operation and select **Save operation**. They form a sequence: create the action and goal, define their relation, then assess it. Example sources are placeholders; replace them with your evidence. The editor applies operations to the current branch and checkpoint. Saving creates a revision, refreshes the summary, and requires a new artifact audit for the changed relationship state.

In the terminal, `/relate PATH` imports the same JSON operation. `/relationships` displays the viewed checkpoint's summary, and `/conflicts` displays its unresolved conflicts. `/head` refreshes the checkpoint after another view changes the branch. Imports cannot override the branch or expected head, and are limited to 128 KiB.

## Define the scope

Save these as three separate JSON files and apply them in order:

```json
{"operation":"node","node":{"id":"launch","label":"Full launch","kind":"action","importance":1}}
```

```json
{"operation":"node","node":{"id":"customer-trust","label":"Protect customer trust","kind":"goal","importance":20}}
```

```json
{"operation":"relation","relation":{"id":"launch-trust","source":"launch","target":"customer-trust","kind":"effect","weight":20,"severe":true,"actions":["Full launch"]}}
```

Node importance expresses how much a person, goal, claim, or action matters in your model. Relation weight expresses the importance of its modeled connection. These values are separate from evidence reliability and token usage. Node and relation definitions are fixed once saved. A new definition requires a new identifier and remains visible in revision history.

Action scope contains **exact decision action names**, such as `Full launch` in the built-in decision example. An empty action array is global. The operator determines applicability; Dao does not infer aliases or whether two differently named actions have equivalent effects.

## Assess without erasing a concern

An assessed relation has a distribution over positive, neutral, and negative signs:

```json
{"operation":"assess","relation_id":"launch-trust","belief":{"positive":0.1,"neutral":0.1,"negative":0.8},"source":"Reliability review","content":"The unresolved reliability issue may undermine customer trust."}
```

The three probabilities must sum to one. Any negative probability greater than zero opens or reopens a conflict. An unresolved severe conflict excludes the actions in its declared scope from both immediate choices and choices after a future signal. If every action is excluded, Dao abstains. Decisions are recommendations based on the supplied assumptions; they grant no permission for external actions.

Unknown is represented separately from a neutral belief:

```json
{"operation":"assess","relation_id":"launch-trust","belief":null,"source":"Uncertainty review","content":"The effect is presently unknown; the concern has not been resolved."}
```

Setting an assessed belief to unknown decreases assessed coverage and leaves its conflict open. A positive or neutral assessment also leaves the conflict open until an audit allows resolution. Unresolved severe conflicts continue excluding their declared actions, even when coherence rises. Artifact saving requires a verdict without an action scope and checks all unresolved severe conflicts.

For fixed relation weights, positive, negative, and neutral mass are the sums of weighted assessed probabilities. Unknown weight is the sum of weights without a current assessment. Conditional coherence is `positive / (positive + negative)` and displays **Not defined** when the denominator is zero. This includes empty, entirely unknown, and entirely neutral graphs. Assessed coverage is assessed weight divided by total relation weight, with zero coverage for an empty graph. Review these values together: a higher coherence value can coincide with less assessed coverage. Coherence does not select actions or override unresolved severe conflicts.

## Record observations and resolve conflicts

Reported observations retain the action, context, before/after signs, and source:

```json
{"operation":"observe","relation_id":"launch-trust","action":"Reversible pilot","context":"Small controlled pilot","before":"negative","after":"positive","source":"Pilot report","content":"The reviewed pilot outcome was positive after the reliability correction."}
```

Reported observations produce transition counts and probability estimates for each relation, action, and context. The estimates include uncertainty and describe associations in the supplied reports. They do not establish causation, calibration, or improving coherence, and they do not change a belief automatically. A negative before or after state opens or reopens the conflict.

When new evidence supports a belief with zero negative probability, record that reassessment explicitly:

```json
{"operation":"assess","relation_id":"launch-trust","belief":{"positive":0.9,"neutral":0.1,"negative":0},"source":"Follow-up review","content":"The reviewed correction and outcome support zero negative probability."}
```

Then submit a separate resolution request:

```json
{"operation":"resolve","relation_id":"launch-trust","evidence":[{"source":"Operator review","content":"The original reliability concern is resolved.","stance":"support","reliability":0.9},{"source":"Outcome report","content":"The reviewed corrected pilot supports resolving the concern.","stance":"support","reliability":0.9}]}
```

Resolution requires an assessed belief with zero negative probability, a latest reported outcome that is not negative if observations exist, and an allowed audit verdict bound to the current relationship state. You supply the source labels and reliability scores. A favorable assessment or observation alone leaves the conflict open. A supported verdict means the evidence meets the local policy; it does not establish factual truth. A later negative belief or observation reopens the conflict.

The HTTP mutation endpoint is `POST /api/relationships`. It accepts one of the operations above plus selected `branch` and its `expected_head`; the browser also supplies the mutation CSRF header. Successful mutations return new `head` and `result`. `/api/state` and exports include a top-level relationship summary and the raw graph in `head.state.relationships`. Earlier states without a graph are treated as empty without rewriting history.

Branching and restoration apply to the graph as they apply to saved memory. Restore creates a new revision from an earlier checkpoint. Previous conflicts remain in revision history, and journal events and recorded usage are preserved. Restore cannot undo external actions. Artifact permission binds to the current relationship state, so a changed graph requires a new audit.
