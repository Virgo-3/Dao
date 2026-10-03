# Decision model: dao-decision/2

Dao evaluates a supplied finite model and returns a recommendation plus every
input, score, and posterior needed to reproduce it. Evaluation has no side
effects. A recommendation does not execute an action or purchase evidence.
Utilities and costs use the model's `utility_unit`, never the usage ledger's USD.

For scenario s and action a, let `p(s)` be the prior probability and
`n(a,s) = utilities[a][s] - cost[a]`. The score is:

```
expected_net       = sum_s p(s) * n(a,s)
expected_downside  = sum_s p(s) * max(0, -n(a,s))
irreversible      = 1 - reversibility[a]
score[a]          = expected_net
                    - downside_weight * expected_downside
                    - irreversibility_weight * irreversible
                    - rollback_cost[a] * irreversible
```

`reversibility` is a user-provided number in [0,1], not a guarantee that rollback
works. The rollback term models residual exposure; it is not a charge collected
on every actual rollback. Set its estimate deliberately to avoid double-counting
losses already included in utilities.

An optional `max_loss` makes an action inadmissible when its largest negative net
utility among positive-probability scenarios exceeds that cap. This bound applies
to net utility, before score penalties. Impossible scenarios do not count against
the cap. A posterior assigning exactly zero probability to a scenario therefore
can make an action admissible. A tiny nonzero probability still counts fully.

Abstention is always available at score 0. Optimization chooses the best
admissible action or abstention. Every selected action must strictly improve on
abstention, even if `min_score` is negative. The tie set is measured against one
fixed maximum score; successive near ties cannot drift away from that maximum.
Ties prefer higher reversibility, then the lexicographically smaller action ID.
`min_score` gates recommendations but does not remove candidates from the
potential-value optimization. Recommendation tie selection considers only
actions meeting the threshold, so a nearby subthreshold action cannot hide an
eligible choice.

## Waiting and the value of evidence

`signals` is one complete partition of possible outcomes of one evidence
acquisition. `likelihoods[s]` means P(signal | scenario s); for each scenario the
likelihoods over all signals must sum to 1. These are likelihoods, not posterior
probabilities or independent observations to multiply together.

```
P(z)              = sum_s p(s) * P(z | s)
P(s | z)          = p(s) * P(z | s) / P(z)
best_now          = best admissible action or zero abstention
optimal_score_now = max(0, admissible scores before tie preferences)
recommendation_now = best admissible action meeting min_score, or abstention
value_after_signals = sum_z P(z) * optimal_score_under_posterior(z)
EVSI              = value_after_signals - optimal_score_now
value_after_recommendations = sum_z P(z) * recommended_score_under_posterior(z)
wait_score        = value_after_recommendations - wait_cost
```

Each posterior uses the same score weights, reversal estimates, and loss policy.
`signal_evaluations` exposes both its optimal `best` candidate and its thresholded
`recommendation`, plus the true `optimal_score` before tie preferences.
`optimal_score_now`, `value_after_signals` and EVSI retain potential decision value
before the `min_score` gate. `value_after_recommendations` values the actual
thresholded continuation; waiting uses this second quantity. For example, with
equally likely low/high states, action A paying (99,101), action B paying
(-100,105), perfect information costing 1, and `min_score=100`, potential value
after signals is 102. The thresholded continuation abstains in the low state
and chooses B in the high state, giving value 52.5 before cost and wait score
51.5. Dao recommends A now at score 100. Potential EVSI remains 2; it is not
misreported as the thresholded policy's realized improvement.

`wait_cost` includes evidence acquisition and delay costs in the same utility
units. Waiting is recommended only when a signal model exists, its net score
strictly improves on `recommendation_now.score`, and it meets `min_score`.
Equality keeps the current recommendation. `improves_recommendation_now` reports
this comparison; `improves_best_now` separately compares to the potential
optimizer. The engine adds no time or optionality bonus without a modeled
signal. An impossible outcome contributes nothing and has a null posterior.
After evidence arrives, update the model and evaluate again.

## Worked example

[`examples/waiting.json`](../examples/waiting.json) models a 60% chance of being
ready and a 40% chance of not being ready. Shipping yields 100 or -120 points.
Idle and abstention yield 0. A positive signal has likelihoods 0.8 and 0.2; a
negative signal has likelihoods 0.2 and 0.8. Waiting costs 3 points.

| Quantity | Value |
| --- | ---: |
| Best score now: ship | 12 |
| Positive signal probability | 0.56 |
| Ready probability after positive signal | 6/7 |
| Best score after positive signal: ship | 480/7 |
| Negative signal probability | 0.44 |
| Ready probability after negative signal | 3/11 |
| Best score after negative signal: abstain | 0 |
| Value after signals | 38.4 |
| EVSI | 26.4 |
| Net wait score | 35.4 |

The recommendation is `wait`. The posterior plan ships after a positive signal
and abstains after a negative signal. The exact outcomes remain uncertain.

## Input and reproducibility contract

Call `dao.decision.evaluate(problem)` with this JSON shape:

```json
{
  "utility_unit": "modeled utility points",
  "scenarios": [{"id": "scenario", "probability": 1}],
  "actions": [{
    "id": "action",
    "utilities": {"scenario": 10},
    "cost": 0,
    "reversibility": 1,
    "rollback_cost": 0
  }],
  "policy": {
    "downside_weight": 0,
    "irreversibility_weight": 0,
    "max_loss": null,
    "min_score": 0
  },
  "signals": [],
  "wait_cost": 0
}
```

All numeric inputs must be finite numbers, excluding booleans. Costs and score
weights are nonnegative; utility and `min_score` can be negative. Scenario
probabilities and signal likelihoods lie in [0,1]. Scenario, action, and signal
IDs must each be unique in their respective lists. Action IDs `wait` and
`abstain` are reserved. Utility and likelihood maps must contain exactly all
scenario IDs. Unknown fields are rejected so misspelled policy settings cannot
silently change the model. Scenarios must be nonempty; actions can be empty.
Optional `max_loss: null` means no cap.

Probability sums must be within 1e-9 of 1 and are normalized, with any rounding
residual assigned to the largest probability to stabilize exact replay. The returned
`inputs` are sorted, include defaults, and can be passed to `evaluate` again.
Input objects are not mutated. Output identifies `model_version` and contains
action evaluations, posterior evaluations, waiting evaluation, and a
`recommendation` with `kind` (act, wait, or abstain), `action_id` (null except for
act), `score`, and reasons. No timestamps or random draws enter evaluation.

Numerical ties use relative and absolute tolerances of 1e-12, also returned in
the output. Strict improvements must exceed that tolerance. A negative EVSI
within rounding tolerance is set to zero; a materially negative EVSI or any
arithmetic overflow rejects the model. Validated float priors and likelihood
partitions are normalized as exact fractions internally. Joint probabilities,
posterior support, and net-loss cap comparisons do not underflow. Scores and
JSON probability displays remain floats. A positive signal may display
`probability: 0` while `possible: true`; `posterior_support` lists every state
with positive exact posterior mass, even when its displayed probability is zero.
Only `possible: false` means an impossible signal. Do not use rounded display
zeros to relax a loss constraint or treat these display values as a lossless
posterior representation. Persistent relationship updates reject a positive
posterior outside the float storage range before changing branch state.

Version 2 changes waiting to the thresholded continuation policy and corrects
numerical support and tie selection. Existing immutable version-1 decision
records keep their original interpretation; reevaluate inputs for a new decision
using the current version. Historical records are not rewritten.

## Scope and assumptions

This is a one-step decision aid. The inputs supply mutually exclusive scenarios,
calibrated priors, utilities, and a valid likelihood model; Dao cannot establish
their truth. It does not infer correlations, learn probabilities, model strategic
opponents, simulate an indefinite sequence of observations, or solve a full
partially observable decision process. Waiting's benefit assumes the modeled
signal will be available and useful; time evolution and irreversible deadlines
belong in scenarios, utilities, and wait cost.

The loss cap constrains modeled scenarios, not unknown hazards. Reversal scores
do not replace a real compensation procedure. Recommendations and their evidence
should pass through Dao's adjudication process before any external action.
