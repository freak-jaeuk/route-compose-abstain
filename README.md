# Route–Compose–Abstain (RCA)

> A multi-source question-answering platform that **selectively routes and composes**, per query,
> knowledge scattered across structured statistics · legal documents · a knowledge graph, and **abstains** when evidence is insufficient.

The domain is **infectious disease** (KDCA incidence statistics + 감염병예방법 (the Infectious Disease Control and Prevention Act) + a disease-grade / reporting-hierarchy graph). The architecture is domain-independent.

## Why

Ordinary RAG sends every query through the same vector search. On numeric/aggregation queries, relational queries, and queries the data has no answer to,
this produces wasted computation and plausible-sounding wrong answers. RCA calls only the sources a given query needs,
verifies the evidence per route, and then emits one of `ANSWER` / `CLARIFY` / `ABSTAIN`.

## Components

| Route | Backend | Queries handled | Example |
|---|---|---|---|
| SQL | SQLite (read-only) | numbers · aggregation · trends | "2023년 시도별 수두 발생 건수" (2023 chickenpox case counts by province) |
| Document | Qdrant + BM25(kiwi) + reranker | definitions · descriptions | "제2급감염병 신고 기한은?" (what is the reporting deadline for a Class 2 infectious disease?) |
| Graph | Ladybug (openCypher) | relations · hierarchy | "제8조의2가 시행령에 위임한 규정" (the provisions Article 8-2 delegates to the Enforcement Decree) |
| Composite | combination of the above | compound | "수두 신고기준 설명 + 최근 3년 추이" (explain the chickenpox reporting criteria + the last 3 years' trend) |
| Abstain | — | no evidence | "김철수 확진 여부" (whether a named individual is a confirmed case — PII) · "2030년 전망" (2030 outlook — out of range) |

## Data (all public)

| Store | Scale | Source |
|---|---|---|
| Structured | 14,226 rows (68 diseases × 2015~2026 × 18 regions) | [KDCA Infectious Disease Portal](https://dportal.kdca.go.kr) |
| Document | 202 chunks / 감염병예방법·시행령·시행규칙 (the Act, its Enforcement Decree and Enforcement Rules) | [Korean Law Information Center](https://www.law.go.kr) |
| Graph | 285 nodes (213 articles · 68 diseases · 4 grades) · 260 edges, of which 125 are delegation | rule-based extraction from the statutes above (no LLM) |
| Join hub | 67 matched: 98.5% of the 68 statistical types, 76.1% of the statute's 88 enumerated types | `data/disease_hub.csv` |

## Running

```bash
pip install -r requirements.txt

# 1) Build the data (collect from public APIs → build the stores)
python scripts/fetch_kdca.py            # collect incidence statistics (sequential, ~20 min)
python scripts/fetch_law.py             # 3 statutes
python scripts/build_disease_hub.py     # join hub
python scripts/build_sqlite.py          # structured store
python scripts/build_document_chunks.py # document chunking
python scripts/build_graph.py           # knowledge graph

# 2) Demo server (the LLM is an OpenAI-compatible endpoint, default localhost:30070)
PYTHONPATH=src uvicorn rca.api:app --port 8000 --workers 1
#   → http://localhost:8000  (single-page UI)

# 3) Evaluation (60 items × abstention ON/OFF)
PYTHONPATH=src python eval/run_eval.py
python eval/analyze_eval.py
```

The LLM backbone switches between local and cloud through the `RCA_LLM_BASE` and `RCA_LLM_MODEL` environment variables. Retrieval and embedding are pinned to CPU.

## Evaluation — does abstention prevent wrong answers?

Same backbone, same retrieval, toggling only the abstention policy (60 items). Details: [eval/RESULTS.md](eval/RESULTS.md).

| Condition | coverage | risk | selective acc | abstention R | AURC↓ |
|---|---|---|---|---|---|
| **abstention ON** | 0.600 | **0.167** | **0.778** | 0.625 | 0.175 |
| abstention OFF | 1.000 | 0.267 | 0.667 | — | **0.142** |

Abstention lowers operating-point risk from **0.267 → 0.167** (difference −0.100, paired bootstrap 95% CI [−0.204, −0.004]).
**But AURC actually gets worse** (0.142 → 0.175) — because the abstention triggers push 14 answerable queries into the low-confidence region.
Reporting accuracy alone hides this half of the picture. False refusals are 14/24, mostly a limit of retrieval recall ([limitations](eval/RESULTS.md#honest-limitations)).

## Documentation

| Document | Contents |
|---|---|
| [docs/TECHNICAL_OVERVIEW.md](docs/TECHNICAL_OVERVIEW.md) | Technical overview — flow · components · stack · "how this differs from ordinary RAG" |
| [ARCHITECTURE_v1.md](ARCHITECTURE_v1.md) | System design — trace schema · security boundary · ADRs |
| [docs/RESEARCH_PLAN_v2.md](docs/RESEARCH_PLAN_v2.md) | Research design — evaluation set · labeling · metrics (for the paper extension) |
| [eval/RESULTS.md](eval/RESULTS.md) | Measured results |

## Design points

- **Instrumentation first** — 1 tool call = 1 JSONL line. Every metric comes from this log and nowhere else.
- **A successful tool call ≠ sufficient evidence** — even when retrieval returns results, an answer is given only after the evidence gate is passed.
- **The graph is not built with an LLM** — the "법 제N조" ("Article N of the Act") delegations in the Enforcement Decree are extracted by regular expression (reproducibility).
- **Security is not negotiable** — SQL is read-only · SELECT-only · parameter-bound · whitelisted · PII-blocked.

## Notice

The output of this repository is **not medical or legal advice**; it is reference material grounded in public statistics and statutes. No personal data is handled.
Statute full texts are not redistributed but reproduced via `scripts/fetch_law.py` (source: 법제처 / Ministry of Government Legislation, KOGL).

## License

MIT (code) / data follows the license of each source.
