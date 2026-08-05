# Implementation Status and Technology Stack

This document covers **only what actually runs in the repository today**. For design intent see [ARCHITECTURE_v1.md](../ARCHITECTURE_v1.md); for a technical overview see [TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md).

- **about 2,550 lines** of Python (`src/rca` + `scripts` + `eval`)
- Domain: **infectious disease** (KDCA incidence statistics + 감염병예방법 (the Infectious Disease Control and Prevention Act) + a rule-extracted knowledge graph)
- Three stores, three agents, the orchestrator, the LLM, and FastAPI all run on real data

---

## 1. What Works

None of this is a stub. Everything runs end-to-end on real public data, real retrieval, and a real LLM.

| Layer | Status | Evidence |
|---|---|---|
| Data collection (KDCA API) | ✅ | 14,226 rows (68 diseases × 2015~2026 × 18 regions), nationwide = sum-of-regions consistency PASS |
| Join hub | ✅ | 67 matched — 98.5% of the 68 statistics types, 76.1% of the statute's 88 |
| SQLite structured store | ✅ | Double counting and join integrity verified with real queries |
| Document store (202 chunks) | ✅ | Article-level chunking of 감염병예방법·시행령·시행규칙 (the Act, its Enforcement Decree and its Enforcement Rules) |
| Knowledge graph (285 nodes, 260 edges — 125 delegation) | ✅ | Rule-extracted delegation edges, 2-hop traversal on ladybug |
| SQL Agent | ✅ | NL→QuerySpec→validation→parameterized SQL→read-only. Passes its own security self-check |
| Document Agent | ✅ | BM25(kiwi)+bge-m3+reranker hybrid, RRF |
| Graph Agent | ✅ | 3 Cypher templates, read-only |
| Router | ✅ | LLM few-shot + rule fallback, smoke test 4/4 |
| Orchestrator | ✅ | routing→decomposition→execution→evidence gate→policy, e2e |
| Abstention policy (8 reason codes) | ✅ | PRIVACY_RESTRICTED, OUT_OF_SCHEMA, GRAPH_PATH_NOT_FOUND, etc. observed |
| LLM backbone | ✅ | vLLM gpt-oss-20b, OpenAI-compatible |
| trace instrumentation | ✅ | 1 line per tool call + a `_run` closing line, kind(llm/tool) distinction |
| FastAPI demo | ✅ | POST /query response URL + single-page UI |
| Evaluation harness | ✅ | 60 items × abstention ON/OFF, trace→metrics |

Not implemented (v0.2): MCP server (the tool schemas are designed in contract form), encoder router, the full verifier, ReAct baseline.

---

## 2. Code Map

```
src/rca/
├── state.py         RunState·Budget·ToolCall + reason codes, validate_assignment
├── trace.py         Tracer (JSONL, query-scoped) · read_traces
├── llm.py           vLLM client (urllib, OpenAI-compatible)
├── router.py        5-way router (LLM few-shot + rule fallback)
├── orchestrator.py  pipeline — routing, execution, evidence gate, policy, answer generation
├── api.py           FastAPI demo
└── tools/
    ├── sql.py       SQL Agent (security boundary)
    ├── docs.py      Document Agent (hybrid retrieval)
    └── graph.py     Graph Agent (Cypher templates)
scripts/
├── fetch_kdca.py            collect incidence statistics
├── fetch_law.py             collect the 3 statutes
├── build_disease_hub.py     join hub
├── build_sqlite.py          structured store
├── build_document_chunks.py document chunking
└── build_graph.py           knowledge graph
eval/
├── qa/gold.jsonl   60 items (44 answerable + 16 unanswerable, 6 twins)
├── run_eval.py     runs the 2 conditions
└── analyze_eval.py trace → metrics
```

Every `src/rca/*.py` and `tools/*.py` carries a `__main__` self-check. Every `scripts/*.py` prints verification figures when run.

---

## 3. Core Implementation Set

### 3.1 Instrumentation First (`trace.py`)

1 tool call = 1 JSONL line, 1 query = 1 `_run` closing line. Every metric comes from this log and nowhere else.
We separate `ok` (the tool executed successfully) from evidence sufficiency, and separate `cited` (actually cited) from the full set of retrieved evidence.
A continuous `answer_confidence` score has to exist before the risk–coverage curve (AURC) can be integrated.

### 3.2 SQL Security (`tools/sql.py`)

The only point where user input reaches the DB. Nothing here can be cut:
read-only (mode=ro) · only SELECT is generated · 100% parameter binding of values · whitelists for tables/columns/diseases/regions/years ·
person-level determination queries blocked (aggregate queries pass) · the `전국`/`기타` (nationwide/other) aggregate rows cannot be mixed with provinces (prevents double counting).
4 items from the Codex security review were addressed; the injection, PII, schema-escape, and double-counting self-checks pass.

### 3.3 Rule-Extracted Graph (`build_graph.py`)

The graph is not built with an LLM. Delegation patterns matching `법\s*제(\d+)조` in the body of the Enforcement Decree are pulled out with a regex and
cross-checked against the statute articles (125 edges, 1 dangling excluded). This forecloses the "inaccurate LLM-generated graph" criticism and secures reproducibility.

### 3.4 Conditions Are config (`orchestrator`·`run_eval`)

Experimental conditions such as abstention ON/OFF share the same runtime. By parameter, with no `if system == ...` branching.

---

## 4. Technology Stack (with roles)

| Layer | Technology | Why |
|---|---|---|
| Language & API | Python 3.11 · FastAPI · Pydantic v2 | a wrong reason code fails immediately at Literal+validate_assignment |
| Orchestration | Custom state machine | Explicit control of budget, retries, and abstention. Frameworks we do not use (LangGraph) are not in the stack |
| Structured | SQLite (read-only) | Embedded; a server is one config line |
| Vector | Qdrant local mode | No server needed, FastAPI workers=1 |
| Graph | **Ladybug 0.18** (embedded openCypher) | Successor fork after Kùzu was archived upstream (2025-10) |
| Embedding | BGE-m3-ko | Korean dense (pinned to CPU) |
| Lexical retrieval | bm25s + kiwipiepy | a Korean morphological tokenizer is mandatory |
| Reranker | bge-reranker-v2-m3-ko | Korean cross-encoder (CPU) |
| LLM | vLLM gpt-oss-20b (OpenAI-compatible) | local serving; the code is agnostic to local vs. API |
| Instrumentation | JSONL + pure Python | append-only, git diff, no DB needed at this scale |

### Graph Backend Correction

Earlier documents stated that "Kùzu and Neo4j are both openCypher, so query strings are shared and the cost of swapping is two connection functions", but neither was true (Kùzu was archived → Ladybug, and the queries did not port because of DDL, shortest-path syntax, and walk/trail differences). We have settled on Ladybug alone; Neo4j is marked as unverified. See [ADR-1](../ARCHITECTURE_v1.md#7-data-layer).

---

## 5. Development Process — Write → Review → Fix

A separate reviewer (Codex) was run over each piece of code. Real defects caught in the SQL Agent security review:
whitelist bypass in run(), `regions` double counting, runaway execution, PII cases with no honorific — all addressed.
The instrumentation layer went through 3 review rounds, catching the "it runs, but the measurement is wrong" class of bug (no continuous score for AURC, step misattribution, tie handling).
We also left in place cases where review was not accepted mechanically (a claimed `ok=False` regression → replaced by making the definition explicit).

---

## 6. Running

```bash
pip install -r requirements.txt
# data build → demo → evaluation: see 'Running' in the README
PYTHONPATH=src uvicorn rca.api:app --port 8000 --workers 1
```

e2e check:
```
"2023 시도별 수두"  → SQL   → 충북7505·서울3135··· (varicella by province; real data + LLM answer)
"제2급 신고기한"    → DOC   → "24시간 이내"   (Class-2 reporting deadline → "within 24 hours")
"제8조의2 위임"     → GRAPH → 시행령 제1조의3/4/5 (delegation articles of the Enforcement Decree; 2-hop)
"김철수 확진 여부"  → PRIVACY_RESTRICTED   (is a named individual confirmed positive?)
"2030 수두 전망"    → ABSTAIN              (varicella forecast for a future year)
```

---

## 7. Limitations (honestly)

- This is planned orchestration, not autonomous multi-agent (ReAct is the v0.2 baseline).
- The graph is mostly delegation edges; in-article citations were excluded because false positives were too high.
- Evaluation centers on routing and abstention metrics. Scoring the answer content (EM/F1, manual) is separate.
- Domain independence is "claimed" from a single domain; empirical validation on a second domain is v0.2.
- LLM answer generation is slow on the shared GPU (~20 s/item). For demos, pre-caching plus a parallel video recording is recommended.
