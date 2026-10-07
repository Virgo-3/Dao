# Relationships, uncertainty, and conflicts

Dao keeps a directed relationship graph in the active branch's immutable state. Nodes identify people, goals, claims, or actions. Relations distinguish effects, evidential support, compatibility, and resource competition. This is an operational model supplied by the operator; it makes no claim that every object is self-aware or that a numerical balance establishes a person's feelings.

The graph separates a relation's definition, a belief about its sign, reported transitions, and the permission to resolve an adverse case. Evidence and observations are user data, including text imported from documents. They do not instruct Dao to act.

## Use the browser or terminal

Open the **Relationships** tab. Its summary shows conditional coherence, assessed coverage, unknown weight, and severe unresolved conflicts. Expand a conflict to inspect its provenance and action scope. The graph and transition estimates are available beneath the register.

Example buttons load one operation at a time. Review each operation and select **Save operation**. They form a sequence: create the action and goal, define their relationship, then assess it. Example sources are illustrative; replace them with actual evidence. The editor applies operations to the selected branch and current checkpoint. Saving creates a revision, refreshes the summary, and invalidates prior artifact authorization for a different relationship state.

In the terminal, `/relate PATH` imports the same JSON operation. `/relationships` displays the summary of the viewed checkpoint, and `/conflicts` displays its unresolved register. `/head` refreshes the checkpoint after another view changes the branch. Imports cannot override the branch or expected head, and are bounded to 128 KiB.

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

Node importance expresses declared salience; relation weight expresses the importance of the modeled effect. These are distinct from evidence reliability and token usage. Node and relation definitions are immutable within a branch. Changes in scope remain visible through revisions and explicit new identities.

Action scope contains **exact decision action names**, such as `Full launch` in the built-in decision example. An empty action array is global. The operator determines applicability; Dao does not infer aliases or whether two differently named actions have equivalent effects.

## Assess without erasing a concern

An assessed relation has a distribution over positive, neutral, and negative signs:

```json
{"operation":"assess","relation_id":"launch-trust","belief":{"positive":0.1,"neutral":0.1,"negative":0.8},"source":"Reliability review","content":"The unresolved reliability issue may undermine customer trust."}
```

The three probabilities must sum to one. Any positive negative probability opens or reopens a conflict. A severe conflict excludes its scoped actions from the decision calculation, including choices after a future signal; if every action is excluded, Dao abstains. Decisions remain advisory calculations. They do not authorize external effects.

Unknown is represented separately from a neutral belief:

```json
{"operation":"assess","relation_id":"launch-trust","belief":null,"source":"Uncertainty review","content":"The effect is presently unknown; the concern has not been resolved."}
```

Setting a belief to unknown decreases assessed coverage and leaves the conflict open. A neutral assessment also leaves the conflict open until resolution is adjudicated. An unresolved severe conflict continues withholding the affected action, even when displayed coherence rises. Unscoped artifact authorization considers all unresolved severe conflicts.

For fixed relation weights, positive, negative, and neutral masses sum weighted assessed probabilities; unknown mass is the sum of weights without a current assessment. Conditional coherence is `positive / (positive + negative)` and is **Unassessed** when that denominator is zero. Coverage is assessed weight divided by total relation weight, with zero coverage for an empty scope. The summary reports the masses and fractions so changing a classification cannot conceal uncertainty in a single score. Coherence does not select actions on its own or override a severe-conflict constraint.

## Record outcomes and adjudicate resolution

Reported observations retain the action, context, before/after signs, and source:

```json
{"operation":"observe","relation_id":"launch-trust","action":"Reversible pilot","context":"Small controlled pilot","before":"negative","after":"positive","source":"Pilot report","content":"The reviewed pilot outcome was positive after the reliability correction."}
```

Observations produce action- and context-specific transition counts and posterior probability estimates with uncertainty. These are empirical associations conditional on supplied reports. They do not identify causal effects, establish calibration, guarantee improving coherence, or change a belief automatically. A reported negative outcome opens or reopens the conflict.

When new evidence supports a nonadverse belief, record that reassessment explicitly:

```json
{"operation":"assess","relation_id":"launch-trust","belief":{"positive":0.9,"neutral":0.1,"negative":0},"source":"Follow-up review","content":"The reviewed correction and outcome support a nonadverse assessment."}
```

Then submit a separate resolution request:

```json
{"operation":"resolve","relation_id":"launch-trust","evidence":[{"source":"Operator review","content":"The original reliability concern is resolved.","stance":"support","reliability":0.9},{"source":"Outcome report","content":"The reviewed corrected pilot supports resolving the concern.","stance":"support","reliability":0.9}]}
```

Resolution requires an assessed belief with zero negative probability, no latest negative observation, and an allowed evidence adjudication bound to the current relationship state. Source labels and reliability remain operator-supplied assumptions. A favorable assessment or observation alone does not close the case. The supported verdict is a local policy decision, not factual proof; later adverse evidence reopens the case.

The HTTP mutation endpoint is `POST /api/relationships`. It accepts one of the operations above plus selected `branch` and its `expected_head`; the browser also supplies the mutation CSRF header. Successful mutations return new `head` and `result`. `/api/state` and exports include a top-level relationship summary and the raw graph in `head.state.relationships`. Earlier states without a graph are treated as empty without rewriting history.

Branching and restoration apply to the graph just as they apply to memory. Restore appends a revision; prior cases, journal events, model expenditure, and external consequences are not undone. Audit permissions bind to their relationship state, so a stale approval cannot authorize a changed graph.
