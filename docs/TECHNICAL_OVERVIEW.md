# Technical Overview — Route·Compose·Abstain (Infectious Disease Domain)

> ⚠️ **This document describes the target state at v0.1 completion.** It is the reference document for explaining the platform in interviews and talks.
> The current actual implementation status (what works and what is a stub) is recorded truthfully in [IMPLEMENTATION.md](IMPLEMENTATION.md). Do not read this document as "it is all already done."

---

## 0. One-line definition

> A multi-source question answering platform that, for knowledge scattered across heterogeneous public data (structured statistics · legal documents · knowledge graph), selects and combines only the information sources each query needs, and declines to answer when evidence is insufficient.

The domain is **infectious disease**: KDCA incidence statistics (SQL) + 감염병예방법 (Infectious Disease Control and Prevention Act) and its 시행령 (Enforcement Decree) (documents) + a knowledge graph of disease grades and the notification system. The domain is only a data adapter; the architecture itself is domain-independent — plug insurance or industrial-accident data into the same code and it becomes that domain.

**The core claim in one sentence**: a single retrieval path, or always calling every tool, is inefficient and hard to trust in an environment where structured, unstructured, and relational data are mixed. It is better to pick a path per query, verify evidence per path, and abstain when you cannot.

---

## 1. End-to-end flow (query → response)

```
User query
  │
  ▼
Query Analyzer      Extract intent, time range, disease name, whether personal data is requested
  │
  ▼
Router (5-way)      SQL │ DOCUMENT │ GRAPH │ COMPOSITE │ ABSTAIN  + confidence
  │
  ▼
Orchestrator        If COMPOSITE, decompose into sub-queries, decide tool call order, manage budget
  │
  ▼
MCP Tool Layer      query_structured_data │ retrieve_documents │ query_knowledge_graph
  │                 (each tool writes 1 trace JSONL line before and after execution)
  ▼
Verifier            SQL executed OK? Document evidence sufficient? Graph path valid? Cross-source conflict?
  │
  ▼
Response Policy     ANSWER │ CLARIFY │ ABSTAIN(+reason code)
  │
  ▼
Response = answer + cited evidence + path used + cost summary + source attribution
```

The 5 core design principles:

| Principle | Meaning |
|---|---|
| Instrumentation first | Every tool call writes 1 trace JSONL line. Every quantitative metric comes only from this log |
| Abstention is not failure | ABSTAIN/CLARIFY are normal terminal states. Preferred over an answer without evidence |
| Single backbone | Every experimental condition uses the same LLM, temperature, and seed. The only difference is orchestration |
| Conditions are config | Baseline comparisons go through configuration, not `if system==...` code branches |
| Embedded first | Runs immediately after `pip install`, with no external server. Reproducibility = can a reviewer run it in 30 minutes |

---

## 2. Data layer — three stores share one entity

The only reason this platform holds together: **the three stores share a join hub**. Without sharing, COMPOSITE is impossible and it degenerates into three tools listed side by side.

The join hub is **`disease_type` (법정감염병 종류 — statutory infectious disease category)**. 감염병예방법 제2조 (Article 2) enumerates the diseases by grade (1급 17종 · 2급 21종 · 3급 27종 · 4급 23종 — 17 Grade-1, 21 Grade-2, 27 Grade-3, 23 Grade-4 diseases; extracted by rule-based parsing), and the statistics are aggregated under the same disease names.

| Path | Store | Source | Where disease_type appears |
|---|---|---|---|
| Structured | SQLite (embedded) | KDCA 지역별 감염병 발생통계 (region-level infectious disease incidence statistics) (data.go.kr 15053802) | 질병명 (disease name) column (연도×시도×질병×발생 — year × province × disease × cases) |
| Unstructured | Qdrant local + BM25 | 감염병예방법 + 시행령 + 시행규칙 (Enforcement Rule) (법제처 API — Ministry of Government Legislation) | Article titles and bodies |
| Graph | Ladybug (embedded Cypher) | Article structure of the statutes above + grade classification | Nodes |

**The join hub CSV is 88 rows, not 8 columns**: `(sql_disease_name, law_grade, article_no, surveillance_type)`. This one file links SQL column values ↔ statute articles ↔ graph nodes. It is built by hand and verified exhaustively.

One interesting piece of structure: **4급 (Grade 4) diseases do not appear in the 전수감시 (universal case-based surveillance) statistics** (they are covered separately under 표본감시, sentinel surveillance). "왜 결핵은 전수신고인데 감기는 표본감시인가" (why is 결핵 — tuberculosis — subject to full mandatory notification while 감기 — the common cold — is under sentinel surveillance) is a question answerable only from the statute text, and it makes a good COMPOSITE example because it requires statistics, statute, and graph at once.

---

## 3. Router — four variants compared without fine-tuning

The router classifies a query into 5 paths and emits a confidence. There is no fine-tuning; four variants are compared under identical prompt and exemplar conditions.

| Variant | Implementation | Cost | Role |
|---|---|---|---|
| `rules` | regex + schema vocabulary matching | ~0 | lower bound |
| `encoder` | bge-m3 embedding → logistic regression | ~0 | cost-effectiveness candidate |
| `llm` | few-shot + structured output (JSON schema strict) | high | conventional approach |
| `oracle` | gold labels injected | — | upper bound |

Routing decision criteria (applied in order when they conflict):
1. If the answer comes only from an **aggregation of statistical values**, SQL ("2023년 시도별 결핵 발생 건수" — 2023 tuberculosis case counts by province)
2. If the answer comes only from **the wording of an article**, DOCUMENT ("결핵의 신고 기한은?" — what is the notification deadline for tuberculosis?)
3. If the answer requires a **relation path between two entities**, GRAPH ("장티푸스 신고의무를 위임한 하위 규정은?" — which subordinate regulation was delegated the notification duty for 장티푸스, typhoid fever?)
4. If two or more of the above are all needed, COMPOSITE
5. If the evidence exists nowhere, ABSTAIN

If confidence falls below the threshold (calibrated on dev), the system abstains with `LOW_ROUTER_CONFIDENCE`.

---

## 4. SQL path — security is the one area that cannot be trimmed

Natural language is never turned directly into SQL. It goes through an intermediate representation (QuerySpec).

```
Natural language query
  → Build QuerySpec (structure the table, aggregate function, filters, period)
  → Validate against the schema and column whitelist
  → Generate parameter-bound SQL (SELECT only)
  → Read-only execution
  → Validate result rows and value ranges
```

Security controls (all mandatory; not one can be dropped):

| Control | Implementation |
|---|---|
| Read-only | SQLite `file:db?mode=ro` |
| SELECT only | The QuerySpec→SQL generator emits SELECT only. There is no free-form SQL execution path |
| Multi-statement blocking | Rejected if it contains a semicolon |
| Whitelist | Tables and columns absent from the schema registry get `OUT_OF_SCHEMA` |
| Parameter binding | Values are 100% bound. String concatenation is forbidden |
| PII blocking | Person-identifying query patterns + column blocklist → `PRIVACY_RESTRICTED` |
| Resource caps | statement timeout, forced LIMIT, result row-count cap |

The public infectious disease statistics are aggregates, so there are 0 personal-data columns. PII control is therefore **demonstrated through query blocking** — a person-identifying query such as "환자 홍길동의 진단 이력" (the diagnosis history of patient 홍길동, a placeholder personal name) is blocked before execution because the schema contains no person-level table. The README states this honestly: "no personal data in the dataset; person-identifying queries are blocked before execution." This is in fact the better design.

---

## 5. Document path — hybrid retrieval

Statutes arrive with 조·항·호·목 (article, paragraph, subparagraph, item) already structured as API fields, so chunking is a field-mapping problem rather than a parsing problem.

```
Statute JSON (법제처 API)
  → Chunk at article granularity (split by paragraph if the article is long)
     meta: {law_id, article_no, article_title, effective_date}
  → Index
     ├── BM25 (bm25s + kiwipiepy morphological tokenizer)   ← without morphology, BM25 is meaningless for Korean
     └── Dense (BGE-m3-ko embeddings, Qdrant local)
  → At query time run both paths in parallel → RRF fusion
  → Cross-Encoder rerank (bge-reranker-v2-m3, top-50 → top-5)
  → Return the evidence span
```

What actually trips up parsing (low risk, but must be handled):
- **Branch article numbers (조문가지번호)** — so that 제11조 and 제11조의2 (Article 11 and Article 11-2) do not collide on the same key
- **Missing required 항 (paragraph) field** — definition articles sometimes have only 호 (subparagraphs) and no paragraph number
- Fallback for articles with no 항

top-k and RRF weight tuning is done only on the dev split, and that record becomes the supporting evidence for "retriever optimization."

---

## 6. Graph path — Cypher templates + a rule-extracted graph

**We do not build the graph with an LLM.** The reason is that the relations between statute articles are extractable by rule — this is the heart of reproducibility, and it heads off the reviewer objection "an inaccurate LLM-generated graph" at the source.

```
Nodes: Law · Article · DiseaseType · Grade · SurveillanceType · Region · Year
Edges:
  Law -[:CONTAINS]-> Article
  Article -[:DELEGATES_TO]-> Article      (delegation pattern "법 제N조" in 시행령 body text, regex)
  DiseaseType -[:CLASSIFIED_AS]-> Grade    (enumerated in 법 제2조, the join hub)
  DiseaseType -[:REPORTED_VIA]-> SurveillanceType  (전수/표본)
  DiseaseType -[:DEFINED_IN]-> Article
```

Delegation edges are extracted from 시행령 article bodies with the regex `법\s*제(\d+)조(?:의(\d+))?` and matched against the set of statute articles. Instead of free-form Cypher generation there are **3 templates**, one per relation type, and the LLM only selects a template and fills in parameters. Write clauses (CREATE/MERGE/DELETE) are blocked.

A question that only the graph can answer:
> "제3급감염병의 신고 의무를 규정한 조문이 시행령에 위임한 세부 사항은?" (what details did the article prescribing the notification duty for Grade-3 infectious diseases delegate to the Enforcement Decree?)
> → a 2-hop `Article -[:DELEGATES_TO]-> Article`. Document retrieval would find the two documents separately and leave a human to connect them, whereas the graph returns the hierarchy directly.

**Backend caveat**: we use openCypher, but Ladybug and Neo4j queries are not directly compatible (schema DDL is mandatory, shortest-path syntax differs, walk/trail semantics differ). We fix the backend to Ladybug alone and mark a Neo4j migration as "requires rewriting the Graph Agent." (This judgment was initially written down incorrectly as "queries are shareable as strings because it is openCypher," then corrected against measurement; that correction is itself an instance of identifying a technical risk.)

---

## 7. Composite — genuinely multi-source queries

Questions requiring two or more sources are decomposed into sub-queries, the results are integrated, and consistency across sources is checked.

Example:
> "결핵의 법정감염병 등급과 신고 기한을 설명하고, 최근 3년 시도별 발생 추이를 보여줘" (explain the statutory disease grade and notification deadline for tuberculosis, and show the per-province incidence trend over the last 3 years)

```
Decomposition:
  q1(DOCUMENT) → articles on 결핵 grade and notification rules in 감염병예방법
  q2(SQL)      → aggregate 결핵 2021~2023 by province from 15053802
  (the join hub guarantees that "결핵" in q1 and the disease name in q2 are the same entity)
Integration → the Verifier cross-checks the statutory evidence against the statistical values
Response → narrative (article citations) + numbers (aggregates) + sources
```

Without the join hub, the system has no way to know that "결핵" in q1 and "결핵" in q2 are the same thing. That is why the 88-row CSV is the platform's entire reason for existing.

---

## 8. Response policy — 3-way branch + reason codes

| Verdict | Condition |
|---|---|
| `ANSWER` | Verified evidence is sufficient and consistent across sources |
| `CLARIFY` | A qualifier such as year, region, or disease is missing |
| `ABSTAIN` | One of the reason codes below |

8 abstention reasons: `OUT_OF_SCHEMA` · `INSUFFICIENT_EVIDENCE` · `LOW_ROUTER_CONFIDENCE` · `SQL_EXECUTION_FAILURE` · `GRAPH_PATH_NOT_FOUND` · `SOURCE_CONFLICT` · `PRIVACY_RESTRICTED` · `BUDGET_EXCEEDED`
4 clarification reasons: `MISSING_TIME_RANGE` · `MISSING_REGION` · `AMBIGUOUS_ENTITY` · `MULTIPLE_INTERPRETATIONS`

If the reason is not recorded as a code, "what was missing" cannot be reconstructed after the fact — the whole error analysis table simply disappears.

**Evidence gate**: an answer must include at least 1 cited source_id, and if the LLM's self-judgment ("is this answer supported by the evidence?") is no, the system abstains. Only with this gate does the "retrieval succeeded but evidence was insufficient" class of abstention become demonstrable.

---

## 9. Orchestration — budgets and failure recovery

Budgets are imposed so that the agent cannot loop forever.

```
step limit exceeded / token or time limit exceeded → ABSTAIN(BUDGET_EXCEEDED)
tool error → 1 retry → 1 fallback path → if it still fails, record the reason and stop
router confidence < τ → ABSTAIN(LOW_ROUTER_CONFIDENCE)
numeric mismatch across sources → ABSTAIN(SOURCE_CONFLICT)
```

The budget decision lives in exactly one function (`Budget.exhausted`) and means "can the next call be started?" Every limit stops the run the moment it is reached.

Honestly: this structure is planned orchestration plus a failure-recovery loop, not an autonomous multi-agent system in which the agent freely chooses its own tools. ReAct-style free tool selection is compared as a **baseline**.

---

## 10. Observability — the trace JSONL is the core of the architecture

1 tool call = 1 JSONL line, 1 query = 1 `_run` terminating line. Every number in the paper and in the résumé comes from here.

```json
{"qid":"q_0001","system":"proposed","backbone":"gpt-oss-20b@...","seed":0,
 "step":2,"tool":"query_structured_data","route_pred":"COMPOSITE","route_conf":0.81,
 "sql":"SELECT region, SUM(cases) ...","output":{"row_count":17},
 "tokens_in":320,"tokens_out":60,"latency_ms":430,"ok":true,"kind":"tool"}
{"qid":"q_0001","step":4,"tool":"_run","elapsed_ms":2180,
 "verdict":"ANSWER","answer_confidence":0.72,"cited":["감염병예방법 제2조","15053802"]}
```

Metrics derived from this log:

| Metric | Derivation |
|---|---|
| Router Macro-F1, calibration | `route_pred`/`route_conf` vs gold |
| SQL invalid query rate | `ok`, `error` (whether the tool executed — distinct from evidence sufficiency) |
| Recall@k, nDCG, citation precision | `cited` vs gold evidence |
| **AURC** (primary metric) | `answer_confidence` threshold sweep → integral of the risk-coverage curve |
| Abstention P/R, selective accuracy | `verdict` vs gold `answerable` |
| p95 latency | `_run.elapsed_ms` (not the sum of tool latencies — includes router and verifier) |
| Calls, tokens, and cost per query | aggregate over `kind=="tool"` rows |

Why those two fields are kept separate is the point of the design: without `answer_confidence` the risk-coverage curve has only one point and AURC cannot be integrated, and without distinguishing `cited` (actually cited) from `evidence` (everything retrieved), citation precision cannot be defined.

---

## 11. Experiment harness — conditions are config

8 experimental conditions share the same runtime. There are four axes of variation:

| Axis | Values |
|---|---|
| orchestrator | `planned` · `react` |
| router | `fixed`·`rules`·`encoder`·`llm`·`oracle`·`none` |
| verifier | `none`·`per_path` |
| abstention | `none`·`calibrated`·`oracle` |

Conditions: `doc_only`·`sql_only`·`graph_only`·`always_all`·`react`·`proposed`·`oracle_route`·`oracle_abstain`.

One side effect: a single execution trace of `always_all` (running every query through all 3 stores) yields the **query × store performance matrix** and the **oracle upper bound** at no additional cost.

---

## 12. Tech stack (with the role of each piece)

| Layer | Technology | Why |
|---|---|---|
| Language·API | Python 3.11, FastAPI, Pydantic v2 | Wrong values in the state model are caught immediately by Literal |
| Orchestration | Custom state machine (not LangGraph) | Explicit control over budget, retry, verification, and abstention. We do not list frameworks we did not use in the stack |
| Structured | SQLite (read-only) → PostgreSQL | Start embedded; moving to a server is one config line |
| Vector | Qdrant local mode | Works without a server. FastAPI runs workers=1 (local mode holds a single-process lock) |
| Graph | Ladybug (embedded openCypher) | Kùzu upstream archived (2025-10) → successor fork. No server required |
| Embedding | BGE-m3-ko | Korean dense retrieval |
| Lexical search | bm25s + kiwipiepy | A Korean morphological tokenizer is mandatory |
| Reranker | bge-reranker-v2-m3 | Korean cross-encoder |
| LLM | vLLM gpt-oss-20b (OpenAI-compatible) | Local serving. The code is agnostic to local vs API |
| Deployment | HuggingFace Spaces (Docker SDK, remote build) | A public URL without a container runtime locally |
| Instrumentation | JSONL + pandas | Append-only, git-diffable. No DB needed at the 8 × 400-item scale |

---

## 13. How this differs from ordinary RAG (interview prep)

Three answers to "so what makes this different from ordinary RAG?":

1. **Path selection** — ordinary RAG runs every query through vector search. Here, numeric queries route to SQL and relational queries to the graph. And the effect of that choice on accuracy, cost, and error rate is **shown as numbers**, contrasted against `always_all` (call everything).
2. **Quantifying abstention** — plenty of RAG systems emit "I don't know," but measuring abstention quality with a risk-coverage curve (AURC) is rare. We present, as a curve, how much the error rate drops as coverage is lowered.
3. **Tool execution success ≠ sufficient evidence** — we do not answer just because retrieval returned results. ANSWER requires passing the evidence gate, and if it does not, the reason for abstaining is recorded as a code. From an operations standpoint, this distinction is the core of hallucination prevention.

One line for the résumé:
> Designed and implemented a multi-source orchestration platform that selects and combines heterogeneous public data (structured statistics, legal documents, knowledge graph) per query and refuses to answer when evidence is insufficient. Instrumented tool calls, citations, latency, and tokens as JSONL and evaluated with AURC and selective accuracy.

---

## 14. Limitations (honestly)

- It is planned orchestration, not an autonomous multi-agent system (ReAct appears only as a baseline comparison).
- The graph centers on delegation edges; internal cross-references between articles were excluded because false positives were too high.
- Answer scoring supports EM/F1 only on the SQL path; narrative and composite answers are manually scored by a single rater against a rubric (stated in the table).
- Domain independence is "claimed" from a single domain (infectious disease); empirical validation on a second domain is v0.2.
- Metrics from the stage before the LLM and answer scorer are attached are marked in the table as approximations of the routing metrics.
