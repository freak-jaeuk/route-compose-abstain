# Evaluation results

60 items in the infectious disease domain (44 answerable + 16 unanswerable, 6 contrastive twins),
across 6 conditions that vary only the abstention policy under the **same backbone, same retrieval, same router**.

Reproduce:
```bash
python eval/run_eval.py                # 6 conditions (or --only <condition>)
python eval/analyze_eval.py            # headline metrics
python eval/bootstrap_ci.py            # confidence intervals
python eval/coverage_matched.py        # coverage-matched comparison + tie-break range
python eval/trigger_ablation.py        # log-based counterfactual estimate
python eval/loo_ablation.py            # measured leave-one-trigger-out
python eval/shapley.py                 # exact Shapley over all 16 trigger subsets
python eval/confidence_sensitivity.py  # do the results survive other refusal constants?
python eval/refusal_causes.py          # root-cause classification of false refusals
```

## 1. Abstention ON/OFF

| Metric | Abstention ON [95% CI] | Abstention OFF [95% CI] | Difference [95% CI] |
|---|---|---|---|
| coverage | 0.567 [0.43, 0.68] | 1.000 | −0.433 [−0.567, −0.317] * |
| **risk** | **0.118** [0.03, 0.24] | 0.267 [0.15, 0.38] | **−0.149 [−0.262, −0.048]** * |
| **AURC↓** | 0.149 [0.07, 0.26] | **0.118** [0.05, 0.21] | **+0.031 [+0.001, +0.072]** * |
| **AUROC↑** | 0.747 [0.61, 0.87] | **0.866** [0.77, 0.95] | **−0.119 [−0.213, −0.041]** * |
| abstention precision | 0.462 [0.27, 0.67] | — | — |
| abstention recall | 0.750 [0.52, 0.94] | — | — |
| route macro-F1 | 0.736 | 0.655 | — |
| selective accuracy | 0.778→0.824 | 0.667 | — |

`*` = the 95% paired-bootstrap CI of the difference (B=10,000) does not include 0.
`risk` = answerability risk (the fraction of unanswerable items among those answered), independent of routing.

**It wins at the operating point and loses at ranking.** Abstention lowers risk from 0.267→0.118, but
AURC gets worse (+0.031) and so does AUROC (−0.119). The two metrics point the same way independently.
Of the two we treat AUROC as primary: the AURC interval reaches +0.001 at its lower end and
would not survive correction for the 7 comparisons reported here (we apply none, and report
intervals descriptively).

### This is not an artefact of the hand-picked refusal constants (`confidence_sensitivity.py`)

26 of the 60 confidence values are constants we chose (privacy 0.02, execution gate 0.10,
router 0.25), so the obvious objection is that the ordering result was manufactured by those
three numbers. Re-scoring the same traces says otherwise:

| Perturbation | AURC worse than OFF | AUROC worse than OFF |
|---|---|---|
| all 6 reassignments of the 3 constants | **6/6** | **6/6** |
| 1000 draws, each constant ~ U[0, 0.9] | **100%** | **100%** |

Every answered query scores at least 0.6, so as long as all three refusal constants stay below that floor,
only their **order** reaches either metric — the 6 permutations above already enumerate that space, and
drawing from `[0, 0.6)` would be the same experiment twice. The draws therefore go up to 0.9, which lets a
refusal outscore an answer; that is the case the permutations cannot reach.

Ranges over the random draws: AURC [0.144, 0.387] vs 0.118 for OFF; AUROC [0.293, 0.770] vs 0.866.
**No assignment of refusal constants recovers the OFF condition's ordering.** The result comes from
*which* queries get refused, not from what score they are given.

## 2. Once coverage is matched — it loses to a scalar threshold

Comparison with coverage held fixed at 0.567 (34/60):

| How the answered set is chosen | risk |
|---|---|
| abstention policy (8 reason codes) | 0.118 (4/34) |
| **most confident 34 with abstention OFF** | **0.080** [0.029, 0.118] |
| random rejection (B=2000) | 0.267 [0.176, 0.353] |

⚠️ **"top 34 by confidence" is not a single set.** Confidence takes only 6 values, so 31 of the
60 queries tie at the boundary (0.6) and 21 of the 34 slots must be filled out of that tie.
Risk ranges from **0.000 to 0.118** across tie-breaks, averaging 0.080; in **19%** of tie-breaks
the threshold does no better than the policy. One deterministic tie-break (sort by qid) gives
0.059 — the 39th percentile of that distribution, i.e. an artefact of an arbitrary choice.
We report the distribution instead.

The policy beats random, so it uses signal. But at matched coverage the 8 reason codes buy
**no risk advantage** over a threshold on a single scalar. Consistent with §1.

## 3. Trigger contribution — three methods, three answers

All columns give the effect on risk of **enabling** the trigger; negative = lowers risk.
All three are computed from the same traces (`eval/runs`) — mixing pre- and post-repair
traces here silently compares two different systems, so `loo_ablation.py` now refuses to run
when the estimate came from a different trace directory.

| Trigger | n_T | u_T | Estimated | Leave-one-out | **Shapley** |
|---|---|---|---|---|---|
| evidence gate | 7 | 1 | −0.004 | +0.018 | −0.020 |
| privacy screen | 6 | 4 | −0.082 | 0.000 | 0.000 |
| schema-range check | 3 | 3 | −0.072 | 0.000 | **−0.040** |
| graph path check | 2 | 0 | +0.007 | 0.000 | +0.003 |
| | | | | | Σ = −0.056 |

**All three methods disagree on every row.** The log-based estimate errs on all 4 and reverses
sign on the evidence gate. Leave-one-out then reports exactly 0.000 for three triggers — not
because they do nothing, but because the triggers are not independent:

| Trigger removed | What happens to those queries |
|---|---|
| `PRIVACY_RESTRICTED`, 6 items | **all** abstained under `LOW_ROUTER_CONFIDENCE` |
| `OUT_OF_SCHEMA`, 3 items | **all** abstained under `INSUFFICIENT_EVIDENCE` |
| `GRAPH_PATH_NOT_FOUND`, 2 items | **all** abstained under `INSUFFICIENT_EVIDENCE` |

Turning a trigger off does not stop the query from being abstained — it only changes the reason. The counterfactual assumes that
"turning the trigger off makes the query answered", but what actually happens is a **cascade**.

`evidence gate` is the sole exception: removing it turns 6 of the 7 items into an actual ANSWER (the remaining 1 becomes `OUT_OF_SCHEMA`).
That is why only this trigger has a non-zero leave-one-out effect — **leave-one-out measures a contribution only when nothing downstream catches the query.**

### Shapley recovers what leave-one-out erased (`shapley.py`)

Only 4 triggers, so the full lattice is 2⁴ = 16 configurations = 960 runs (~5s each). Exact, no approximation.
Efficiency axiom holds: Σφ = v(N) − v(∅) = 0.118 − 0.174 = −0.056.

The schema-range check carries **71% of the total risk reduction** while leave-one-out scores it 0.000.
Its marginal effect depends entirely on the coalition:

| Coalition it is added to | risk before → after | Δ |
|---|---|---|
| nothing | 0.174 → 0.095 | **−0.079** |
| evidence gate only | 0.118 → 0.118 | 0.000 |
| the other three | 0.118 → 0.118 | 0.000 |

Leave-one-out only ever sees the last row. The privacy screen is 0.000 in *every* coalition
(genuinely inert, not masked); the graph path check is slightly harmful. Leave-one-out reports
all three situations identically as 0.000.

### The shipped configuration is dominated

| Configuration | coverage | risk | answered | errors |
|---|---|---|---|---|
| all 4 triggers (shipped) | 0.567 | 0.118 | 34 | 4 |
| **schema-range check alone** | **0.700** | **0.095** | **42** | **4** |

Better on both axes — 8 more answers, same number of errors. Three of the four triggers buy
nothing here and cost coverage. One-at-a-time ablation cannot find this: the comparison that
reveals it is between two configurations leave-one-out never visits.

## 4. A reason code tells you only which gate fired

Classifying the **root cause** of each false refusal from the traces (`refusal_causes.py`), the logged reason differs from the actual cause:

| Logged reason | Actual root cause |
|---|---|
| `INSUFFICIENT_EVIDENCE` | retrieval_miss 5 · zero_count 1 |
| `LOW_ROUTER_CONFIDENCE` | router_no_match 4 |
| `GRAPH_PATH_NOT_FOUND` | router_wrong_path 2 |
| `PRIVACY_RESTRICTED` | policy_overmatch 2 |

**For 9 of the 14 the code names the cause; for 5 it names only the gate.** Accurate: the 5 `retrieval_miss`
items (the retriever ran and returned nothing above threshold) and the 4 `router_no_match` items (never
reached a retriever). Not accurate: the 2 `GRAPH_PATH_NOT_FOUND` items are literally true — no such path
existed — but the cause is that the router sent a document query to the graph; the 2 `PRIVACY_RESTRICTED`
items matched a name-like token inside an aggregate query; and the 1 `zero_count` item ran its SQL
successfully and summed to 0 over rows that exist — zero *is* the answer, which is why refusing it counts as
a false refusal, and it is a different operational problem from a retriever that missed. A reason code tells you "which gate fired", not "why the query reached that gate".

### The first version of this classifier inverted the result

An earlier `refusal_causes.py` sent any refusal on a COMPOSITE query to `composite_partial` whenever a tool
had been called, without looking at what the tool returned. All 6 COMPOSITE refusals made exactly one call,
to the document retriever, which returned `{"hits": 0}` — so they were recorded as "first leg succeeded,
second leg stalled", and the `retrieval_miss` branch became **unreachable for COMPOSITE queries by
construction**. On that classification this section reported **0 retrieval-recall failures**; 5 of the 14 are
retrieval failures. The traces held the correction the whole time — the branch order, not the data, produced
the old number. This is the same "the label records which branch fired, not what happened" defect the section
is about, one level up in our own measurement code.

## 5. Even so: the diagnosis did fix the system

The analysis above found 2 defects, which we fixed, and the re-run results improved.

| Metric | Before fix | After fix |
|---|---|---|
| risk | 0.167 | **0.118** |
| abstention recall | 0.625 | **0.750** |
| abstention precision | 0.417 | **0.462** |
| AURC | 0.175 | **0.149** |
| selective accuracy | 0.778 | **0.824** |

The defects fixed:
1. **Entity-resolution failures silently fell through to a full aggregate.** `"2023년 시도별 화성인 감염병 발생 건수"`
   (2023 per-province case counts for "Martian disease" — a disease that does not exist in the schema) was answered with 16 rows at confidence 0.6. When a query names a disease that is not in the schema, it is now sent to `OUT_OF_SCHEMA`.
2. **The router's no-match fallback was logged as `INSUFFICIENT_EVIDENCE`.** In 8 of the 15 items the retriever was
   never even called. These are now split out as `LOW_ROUTER_CONFIDENCE`.

Neither defect would have been visible if only a scalar confidence had been logged.

## Honest limitations

1. **The sample is small.** 60 items, a single domain, a single author, no IAA.
2. **Answer content is not graded.** Correctness is defined as routing match or appropriateness of abstention;
   the factuality of the generated text enters neither. The evaluation was run with `generate=False`, `use_llm=False`.
3. **confidence is not a model output.** When answering it is `min(0.9, 0.5+0.1·|evidence|)`; when abstaining it is a per-reason constant.
   Across the 60 items there are only 6 distinct values, so AURC is sensitive to the relative order of these constants.
4. **Abstention OFF is an experimental control that turns off even the policy abstentions**, not a deployment setting.
5. **The router is rule-based** and was fixed in advance from the schema (not tuned to the evaluation items).

## Reproducibility

Retrieval and reranking select CPU/GPU via `RCA_DEVICE` (inference is deterministic, results identical) ·
temperature 0 · seed 0 · rule-based routing. The 6 trace JSONL files are included in the repository, and
every metric is derived from these logs alone. The analysis scripts refuse to run if the item count is not 60.
