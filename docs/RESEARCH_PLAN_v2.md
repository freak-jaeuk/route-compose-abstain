# Research Plan v2

This document covers what changed from v1 and **the two protocols that must be fixed before data construction**.
The system design lives in [ARCHITECTURE_v1.md](../ARCHITECTURE_v1.md) and is not repeated here.

---

## 1. Title

**Route, Compose, or Abstain: Calibrated Multi-Source Orchestration over SQL, Hybrid Document Retrieval, and Knowledge Graphs**

Korean title: SQL·비정형 문서·지식 그래프를 통합한 선택적 질의응답 — 다중 소스 오케스트레이션과 보정된 응답 거절

---

## 2. What changed from v1

| # | v1 | v2 | Reason |
|---|---|---|---|
| C1 | Write questions deliberately per route, then evaluate the router against those labels | Write from the raw sources without fixing a route → **post-hoc labeling by 2 annotators + report κ**, at least 20% naturally occurring queries | Circular reasoning: the authors' system solves problems the authors wrote. The RQ1 result is inflated wholesale |
| C2 | 100 unanswerable items (composition undecided) | **contrastive twin** required — a minimal perturbation of an answerable question | Synthetic unanswerables are filtered out by surface cues, so abstention performance is overestimated |
| C3 | 3 weeks | Step 0.5→3, **6–7 weeks** (the 3-week reduced version is in §7) | gold annotation alone is on the order of 100 hours |
| C4 | train 60 / valid 20 / test 20 | **dev 20 / test 80** | no fine-tuning, so a train split is wasted |
| M1 | 10 baselines (no ReAct) | **add ReAct with free tool choice**, 8 total | "Why not just give the LLM all the tools?" is the first reviewer question and the real-world alternative in practice |
| M2 | backbone unspecified | **same backbone, temperature, and fixed seed across all conditions** | with different LLMs the cost and accuracy comparison is meaningless |
| M3 | no statistical testing | pre-declared primary metric + bootstrap CI + Holm (family 3) | 30 metrics × 8 conditions × 5 ablations = multiple comparisons |
| M4 | 30+ metrics | 3 tables in the main text, the rest in the appendix | many metrics means no story |
| M5 | 13 ablations | **5** (Graph · Composite · Verifier · Abstention · router) | intra-retrieval comparisons (BM25/dense/rerank/chunking/α) are already covered by prior work and orthogonal to our claim |
| M6 | RQ2 "is graph good on graph-only questions" | **full query × store performance matrix** | The original is tautological. A single `always_all` run yields both the matrix and the oracle upper bound at no extra cost |
| M7 | KG schema only | specify the construction method + report **audit accuracy on a 100-triple sample** | a reproducibility hole and the #1 reviewer question |
| M8 | CLARIFY scoring undecided | counted as no answer in the primary metric, **precision reported separately** + 4 reason codes | we were measuring a 3-way policy with a 2-way metric |
| M9 | faithfulness judge unspecified | fixed judge model and prompt + **human agreement rate on n≥100** | without it, the metric is unverifiable |
| M10 | 3 routers | **add an encoder classifier** (bge-m3 + logistic regression) | 100× cost difference. If it wins, a practical contribution; if it loses, a negative result |
| — | primary metric AURCC | **AURC** (area under risk–coverage curve) | the standard name |

---

## 3. To be finalized ① route labeling protocol (O1)

**This decision cannot be undone.** It is fixed before any question is written.

### 3.1 Authoring stage (without labels)

1. Lay out the raw sources first (statistical table schemas / statute articles / graph relations).
2. Looking at each source, write **"a question a real user would plausibly ask with this source as evidence."**
3. At this point the author **does not record** whether it is SQL/DOCUMENT/GRAPH.
4. Mix in **at least 20%** naturally occurring queries (excerpted from public-agency FAQs and published citizen inquiries, with personal information removed).

### 3.2 Labeling stage (2 separate annotators)

- Label set: `SQL` · `DOCUMENT` · `GRAPH` · `COMPOSITE` · `UNANSWERABLE` · `AMBIGUOUS`
- Decision criteria (apply in the order above on conflict):
  1. If the answer follows from **an aggregation of table values** alone, `SQL`
  2. If the answer follows from **statements in a document** alone, `DOCUMENT`
  3. If the answer requires **a relation path between two entities**, `GRAPH`
  4. If **two or more** of the above are needed for a complete answer, `COMPOSITE`
  5. If there is no evidence in the schema, the documents, or the graph, `UNANSWERABLE`
  6. If the answer diverges because a qualifier (year, region, product) is missing, `AMBIGUOUS`
- **Report Cohen's κ.** If κ < 0.70, revise the criteria and relabel everything.
- Disagreements are adjudicated by a 3rd annotator, but **the pre-adjudication κ goes in the paper.**

### 3.3 What goes in the paper

κ, the disagreement rate, and the label pair that disagrees most often (expected: `GRAPH` ↔ `COMPOSITE`).
We also report how close router performance comes to the human-agreement ceiling.

---

## 4. To be finalized ② contrastive unanswerable rules (O2)

From a single answerable question, build a pair in which the answer disappears at **minimum edit distance**.
If the pair can be filtered out from surface cues alone (a year number, a word like "전망" (forecast)), it has failed.

| Perturbation type | Original (answerable) | Pair (unanswerable) | Where it is caught |
|---|---|---|---|
| Aggregation-unit violation | …**지역별** 보험금 지급액 (insurance payouts by region) | …**읍면동별** 보험금 지급액 (by eup/myeon/dong, the smallest administrative unit) | schema granularity |
| Time-range violation | **2021~2023년** 지급 추이 (payout trend) | **2030년** 지급 전망 (payout forecast) | coverage_years |
| Missing column | 지역별 **지급액** (payout amount by region) | 지역별 **평균 처리일수** (average processing days by region) | column whitelist |
| Missing relation | 특약이 수정하는 **보장** (the coverage a rider modifies) | 특약이 수정하는 **세율** (the tax rate a rider modifies) | graph relation type |
| Personal information | **가입 통계** (enrollment statistics) | **가입자 홍길동의 내역** (the records of subscriber 홍길동, a placeholder name) | PII blocking |
| Document-scope violation | **실손보험** 면책사항 (indemnity health insurance exclusions) | **해외 여행자보험** 면책사항 (overseas travel insurance exclusions — not collected) | corpus scope |

Rules:
- The edit distance of a pair is **at most 2 eojeol** (whitespace-delimited Korean word units).
- A pair goes into the same split (dev or test) **together**. If they are separated, the contrast breaks.
- Record the original qid in the `twin_of` field so that pair-level analysis is possible.
- Target share: **at least 60%** of unanswerables are twins. The rest are naturally occurring unanswerable queries.

8-item example for wiring checks: [`eval/qa/demo_gold.jsonl`](../eval/qa/demo_gold.jsonl) (`qa_001` ↔ `qa_004`, `qa_007`)

---

## 5. Evaluation set

| Type | Count |
|---|---:|
| SQL | 80 |
| Document | 80 |
| Graph | 60 |
| Composite | 80 |
| Ambiguous | 30 |
| Unanswerable | 70 (including 40 twins) |
| **Total** | **400** |

Split: **dev 80 / test 320.** Thresholds, weights, prompts, and chunking are tuned on dev only; test is run 1 time.

The gold schema is the same as v1, with `twin_of`, `route_label_a`, `route_label_b` (the raw labels from the 2 annotators), and `gold_abstain_reason` added.

---

## 6. Experiments

### 8 conditions
`doc_only` · `sql_only` · `graph_only` · `always_all` · `react` · `proposed` · `oracle_route` · `oracle_abstain`

All use the same runtime and differ only in config ([ARCHITECTURE §12](../ARCHITECTURE_v1.md#12-experiment-harness--conditions-are-config)).

### 5 ablations
Remove Graph · Remove Composite · Remove Verifier · Remove Abstention · compare the 4 routers

### Metrics
- **Primary metric: AURC** (pre-declared, 1 metric)
- 2 secondary: tokens per query, p95 latency
- Holm family = {AURC, tokens, p95}, 3 members. The remaining 27 metrics are in the appendix and are not tested.
- Bootstrap 95% CI for every metric (B=1000).

### Expected conclusion (negative allowed)
"Orchestration pays off only on multi-hop and composite queries. On single-hop it costs 2× for the same accuracy."
Even if the proposed method fails to beat the baselines, we report the per-route failure causes and the risk–coverage analysis as they are.

---

## 7. Schedule

| Stage | Duration | Output |
|---|---|---|
| Step 0 | done | architecture, trace schema, and primary metric fixed; wiring check |
| Step 0.5 | 3 days | **finalize §3 and §4 of this document** — irreversible afterwards |
| Step 1 | 2 weeks | 30 items end-to-end (real SQL · Doc · Graph · Verifier) |
| Step 2 | 1 week | expand to 400 items, demo, public repo |
| Step 3 | 3 weeks | 8 conditions · 5 ablations · error analysis · arXiv v1 |

If only 3 weeks are available, the reduced version: 200 items (including 40 twins) / 4 conditions (`doc_only` · `react` · `always_all` · `proposed`) / 3 ablations (Graph · Verifier · Abstention) / 1 primary metric, AURC → post arXiv v1, then update with the expansion as v2.

---

## 8. Honesty principles (v1 §19 retained + additions)

- We do not claim that using MCP, Neo4j, or a vector DB is in itself a research novelty.
- We do not claim that Graph is superior on every question.
- We leave open the possibility that routing contributes more to **cost, latency, and wrong-answer reduction** than to accuracy.
- Even if the proposed method is worse than the baselines, we report the per-route failure causes and the risk–coverage analysis.
- We use public data only; no personal contract information and no non-public corporate data.
- **Added**: we report κ before label adjudication. We do not report only the post-adjudication number.
- **Added**: both the demo and the paper state explicitly that system outputs are not insurance, financial, or legal advice.

---

## 9. Open items

O3 KG construction method · O4 judge agreement rate · O5 abstention-threshold calibration method · O6 Korean reranker selection
— the list and when each is handled are in [ARCHITECTURE §16](../ARCHITECTURE_v1.md#16-open-issues).
