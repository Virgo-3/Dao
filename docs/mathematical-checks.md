# Mathematical verification

These synthetic examples were evaluated with the Wolfram plugin while implementing the relationship model. They are exact algebraic checks, not empirical evidence about agents or stakeholders.

| Check | Exact result |
| --- | --- |
| Nine unit positive weights and one negative weight 20 | Conditional weighted coherence = 9/29 |
| Reclassify that negative relationship as unknown, keeping reference scope | Coherence = 1, assessed coverage = 9/29; conflict remains open |
| Dirichlet prior (1,1,1), observed transition counts (5,1,0) | Posterior means = (2/3,2/9,1/9) |
| Marginal posterior variances for those probabilities | (1/45,7/405,4/405) |
| Equal-probability outcomes; Launch payoffs (8,-6), Pilot payoffs (2,1), no penalties | Best immediate utility = 3/2 |
| Perfect modeled signal, observation/delay cost 1 | Expected utility after signal = 9/2; wait utility = 7/2 |
| Exclude Launch with a severe conflict, including posterior choices | Expected utility after signal = 3/2; information value = 0 |

For transition row counts `n_j`, define `a_j = n_j + 1` and `A = sum(a_j)`. The posterior mean is `a_j/A`; its marginal variance is `a_j*(A-a_j)/(A*A*(A+1))`. Every unobserved transition retains positive probability. Neutral can persist. Counts are grouped by relation, action, and context; observational grouping does not establish causation.

The original Telos transitions leave active-state persistence unspecified. Completing them with sign persistence probability `1-delta` and reset-to-neutral probability `delta`, for constant parameters strictly between zero and one, gives stationary probabilities `(alpha/(1+delta), delta/(1+delta), (1-alpha)/(1+delta))`. Thus the stationary positive share among active relations equals `alpha`; these dynamics alone do not guarantee increasing coherence. Dao uses an explicit constrained decision policy instead of adopting that optimization assertion.
