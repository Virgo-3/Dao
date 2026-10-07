# Decision and adjudication model

Dao makes its decision calculations inspectable. It compares immediate actions,
abstaining, and waiting for one explicitly modeled observation. The engine is
deterministic and uses caller-supplied scenarios, probabilities, payoffs, and
signal likelihoods. A result evaluates those assumptions; it does not establish
that the assumptions are true.

## Input and units

`dao.decision.evaluate(problem)` accepts a JSON-compatible object:

```json
{
  "scenarios": [
    {"name": "Demand holds", "probability": 0.5},
    {"name": "Demand fades", "probability": 0.5}
  ],
  "actions": [
    {
      "name": "Full launch", "payoffs": [120, -100], "cost": 5,
      "reversibility": 0.1, "rollback_cost": 10
    },
    {
      "name": "Reversible pilot", "payoffs": [35, -10], "cost": 5,
      "reversibility": 0.95, "rollback_cost": 2
    }
  ],
  "signals": [
    {"name": "Strong study", "likelihoods": [0.9, 0.1]},
    {"name": "Weak study", "likelihoods": [0.1, 0.9]}
  ],
  "waiting_cost": 3,
  "discount": 0.95,
  "irreversibility_penalty": 8,
  "risk_aversion": 0.001,
  "confidence_margin": 1
}
```

Arrays of payoffs and likelihoods follow scenario order. At least one scenario
and one action are required. Names must be nonempty and unique within each
array. All numeric fields must be finite numbers; booleans are rejected.
Probabilities and likelihoods are between zero and one. Scenario probabilities
must sum to one, and the likelihoods across all signals must sum to one for
each scenario. Totals within `1e-9` of one are normalized to remove rounding
error; other totals are rejected. Unknown fields are rejected to expose typos
and unmodeled assumptions.

Each array has at most 64 entries, and names have at most 200 characters before
trimming. Before scoring, the engine rejects problems where
`actions * scenarios * (signals + 1)` exceeds 250,000. These shared limits apply
to direct evaluation, HTTP requests, and model tool calls, bounding computation
and repeated names in posterior results.

Payoffs may be negative. Costs, rollback costs, waiting cost, the irreversibility
coefficient, risk aversion, and confidence margin must be nonnegative.
Reversibility and discount are between zero and one. Defaults are:

| Field | Default | Interpretation |
| --- | ---: | --- |
| `cost` | 0 | Action cost in every scenario |
| `rollback_cost` | 0 | Additional cost in losing scenarios |
| `reversibility` | 0 | Unknown recovery gets no reversibility credit |
| `signals` | `[]` | No modeled observation |
| `waiting_cost` | 0 | Other waiting costs, or combined cost when separate costs are omitted |
| `observation_cost` | 0 | Additional cost of obtaining the observation |
| `delay_cost` | 0 | Additional cost of delay and opportunities lost |
| `reconsider_when` | derived from signals | Operator's review trigger or deadline, at most 200 characters |
| `discount` | 1 | Multiplier for utility after the observation |
| `irreversibility_penalty` | 0 | No additional irreversibility charge |
| `risk_aversion` | 0 | Risk-neutral evaluation |
| `confidence_margin` | 0 | No utility buffer |

Use a common utility unit for every payoff and cost. Variance has squared
utility units, so `risk_aversion` has inverse-utility units. Multiplying all
payoffs and costs by a factor requires dividing risk aversion by that factor
to preserve the preference model. Reversibility is an assumed recoverability
score; the engine applies a linear policy charge, not an empirically estimated
recovery process.

## Immediate utility

Let `p(s)` be a scenario probability, `g(a,s)` its gross payoff for action `a`,
`c(a)` its action cost, `b(a)` its rollback cost, `r(a)` its reversibility,
`k` the irreversibility coefficient, and `lambda` the risk-aversion coefficient.
The modeled outcome is:

```text
losing(a,s) = 1 when g(a,s) - c(a) < 0; otherwise 0
z(a,s) = g(a,s) - c(a) - b(a)*losing(a,s) - k*(1-r(a))
mean(a,p) = sum_s p(s)*z(a,s)
variance(a,p) = sum_s p(s)*(z(a,s)-mean(a,p))^2
utility(a,p) = mean(a,p) - lambda*variance(a,p)
baseline = max(0, max_a utility(a,p))
```

Rollback is triggered by the sign of payoff minus action cost **before** either
policy penalty. The expected rollback charge is its cost times the probability
of that trigger. The irreversibility charge applies in every scenario. Scores
return the mean gross payoff, all charges, mean net payoff, variance, risk
penalty, and resulting utility. Abstention has zero utility.

The engine rejects numerical overflow instead of returning infinity or NaN.
Even finite inputs can overflow when computing squared deviations, so extremely
large utility scales should be rescaled.

## Bayesian observation and the value of waiting

Each signal `y` supplies `L(y,s) = P(y | s)`. The signal outcomes are mutually
exclusive and exhaustive. For each signal with positive probability:

```text
P(y) = sum_s p(s)*L(y,s)
p(s | y) = p(s)*L(y,s)/P(y)
best_after(y) = max(0, max_a utility(a, p(. | y)))
after_signal = sum_y P(y)*best_after(y)
EVSI = max(0, after_signal - baseline)
wait_utility = discount*after_signal - waiting_cost - observation_cost - delay_cost
```

Variance and the risk penalty are recomputed under each posterior. An impossible
signal has probability zero and a `null` posterior; it contributes zero to the
expectation. EVSI denotes the expected value of sample information under this
mean-variance model, before discount and waiting cost. With zero risk aversion
it reduces to the risk-neutral expected value of sample information. The law
of total variance and posterior optimization make its theoretical value
nonnegative; the zero clamp removes floating-point drift.

With no signals, `after_signal = baseline` and `EVSI = 0`. Waiting then only
discounts the same option and pays the waiting cost. The engine never assumes
that uncertainty decreases because time passes.

Recommendation rules use confidence margin `m` as a **utility buffer**, not a
statistical confidence interval:

1. Recommend `wait` when `wait_utility > baseline + m`.
2. Otherwise recommend `act` when the best immediate action has utility above `m`.
3. Otherwise recommend `abstain`.

Equal wait and immediate utilities retain the immediate choice. Action utilities
within numerical tie tolerance (`1e-12` relative or absolute) prefer higher
reversibility, then caller input order. `selected_action` is `null` for waiting
and abstention; `best_immediate_action` remains available for comparison. The
posterior table uses the zero-valued abstention rule to show an information
contingent policy; confidence margin applies to the initial recommendation.

## Demo calculation and independent check

The example above is returned by `demo_payload()`. Its net outcome vectors are
`[107.8, -122.2]` for launch and `[29.6, -17.4]` for the pilot.

| Probability distribution | Full launch utility | Pilot utility | Best option |
| --- | ---: | ---: | --- |
| Prior `[0.5, 0.5]` | -20.425 | 5.54775 | Pilot |
| Strong study posterior `[0.9, 0.1]` | 80.039 | 24.70119 | Launch |
| Weak study posterior `[0.1, 0.9]` | -103.961 | -12.89881 | Abstain |

Each study result occurs with probability `0.5`, giving:

```text
after_signal = 0.5*80.039 + 0.5*0 = 40.0195
EVSI = 40.0195 - 5.54775 = 34.47175
wait_utility = 0.95*40.0195 - 3 = 35.018525
```

The waiting score exceeds `5.54775 + 1`, so Dao recommends waiting. These numbers
were independently checked on October 6, 2026 using the Wolfram Language
evaluator with exact rational inputs before conversion to decimals. The same
calculation can be reproduced without Python:

```wolfram
Module[{prior = {1/2, 1/2}, outcomes = {{539/5, -611/5}, {148/5, -87/5}},
        likelihoods = {{9/10, 1/10}, {1/10, 9/10}}, utility, signalProbabilities,
        posteriors, priorUtilities, posteriorUtilities, baseline, after},
  utility[p_] := Map[Function[z, With[{mean = p.z},
    mean - (p.((z - mean)^2))/1000]], outcomes];
  signalProbabilities = Map[prior.# &, likelihoods];
  posteriors = MapThread[(prior #1)/#2 &, {likelihoods, signalProbabilities}];
  priorUtilities = utility[prior];
  posteriorUtilities = Map[utility, posteriors];
  baseline = Max[Join[{0}, priorUtilities]];
  after = signalProbabilities.Map[Max[Join[{0}, #]] &, posteriorUtilities];
  N[{priorUtilities, posteriorUtilities, baseline, after,
     after - baseline, (19/20)*after - 3}, 12]
]
```

## Audit as adjudication

`dao.audit.adjudicate(payload)` evaluates a submitted claim and evidence using
an explicit policy. It accepts `claim`, `evidence`, `threshold` (default `0.75`),
and `require_sources` (default `2`). Evidence entries require a nonempty source
and content, a stance of `support`, `contradict`, or `neutral`, and finite
reliability between zero and one. The source requirement is a positive integer.
Unknown fields are rejected.

Sources count once after trimming and casefolding their labels. For each distinct
supporting source, take its **lowest** submitted supporting reliability; average
those values to compute `support_score`. Duplicate submissions cannot inflate
support or source count. Source labels cannot establish actual independence.

| Condition | Verdict | `allowed` |
| --- | --- | --- |
| Any submitted contradiction, even reliability zero | `contested` | `false` |
| No contradiction, enough distinct supporting sources, score at or above threshold | `supported` | `true` |
| Otherwise | `insufficient` | `false` |

Neutral evidence supplies no support. Empty evidence is insufficient even with
threshold zero, because the positive source requirement still applies.
Reasons and metrics expose the rule's outcome. Reliability and stance are
caller supplied; the engine neither reads a source nor uses an AI-generated
assertion to certify the evidence. A supported verdict satisfies a local gate;
it is not factual proof or permission for an external action.

The verdict identifier is SHA-256 of the returned `normalized_payload`, encoded
as UTF-8 JSON with sorted object keys, compact separators, `ensure_ascii=False`,
and `allow_nan=False`. Text fields are trimmed, numerical defaults are explicit,
and reliability and threshold values normalize to floats. Evidence array order
and source-label case remain part of the digest. Reordering evidence or changing
its content therefore changes the identifier even when the verdict is identical.
This identity binds an adjudication to what was submitted, not to the world's
truth.

## Boundaries

This is a finite-scenario, single-observation model. It does not estimate
probabilities from conversation, infer signal quality, calibrate evidence,
model repeated learning, or solve a continuous-time real-options problem.
The mean-variance preference model can have dynamic consistency limits; Dao
exposes an information-contingent recommendation rather than claiming a general
optimal control policy. Consequences absent from the scenarios remain absent
from the calculation. The decision tool only returns data; the runtime owns
state changes and permitted actions.

## Waiting plans and relationship constraints

`observation_cost` and `delay_cost` are optional additional nonnegative utility costs. Keep them out of `waiting_cost` when using the separate fields to avoid double-counting. `reconsider_when` can name an event or deadline; it is a review instruction recorded in the result, not a scheduled task. The returned `waiting_plan` exposes modeled signals, each cost, total waiting cost, and a reconsideration condition. Re-evaluate the current graph and assumptions when the signal arrives.

The runtime excludes exact action names affected by unresolved severe relationship conflicts before both prior and posterior optimization. The complete submitted problem is still validated and included in the computation limit. When every action is excluded, abstention remains available. Action applicability is operator-declared and decision results remain advisory; no external action authorization follows from a recommendation. Weighted coherence and empirical transition summaries accompany results as diagnostics and do not replace utilities or automatically supply calibrated scenario probabilities.

The pure evidence adjudicator continues hashing normalized evidence. Runtime permission identifiers additionally bind the action scope and current graph digest. Artifact saving requires an unscoped current permission and no severe unresolved conflicts. Graph edits invalidate earlier artifact permission; conflict-resolution evidence is retained among the versioned audits.
