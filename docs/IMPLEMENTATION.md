# 구현 현황과 기술 스택

이 문서는 **지금 저장소에서 실제로 동작하는 것**만 다룬다. 설계 의도는 [ARCHITECTURE_v1.md](../ARCHITECTURE_v1.md), 기술 개요는 [TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md).

- Python **약 2,550줄** (`src/rca` + `scripts` + `eval`)
- 도메인: **감염병** (질병관리청 발생통계 + 감염병예방법 + 규칙추출 지식그래프)
- 세 스토어·세 Agent·오케스트레이터·LLM·FastAPI 전부 실제 데이터로 동작

---

## 1. 무엇이 동작하는가

stub이 아니다. 전부 실제 공개 데이터·실제 검색·실제 LLM으로 end-to-end 관통한다.

| 계층 | 상태 | 근거 |
|---|---|---|
| 데이터 수집 (질병청 API) | ✅ | 14,226행 (68질병 × 2015~2026 × 18지역), 전국=지역합 정합성 PASS |
| 조인 허브 | ✅ | 법 88종 ↔ 통계 68종, 99% 매칭 |
| SQLite 정형 스토어 | ✅ | 이중집계·조인 무결성 실쿼리 검증 |
| 문서 스토어 (202청크) | ✅ | 감염병예방법·시행령·시행규칙 조문 청킹 |
| 지식그래프 (213노드·125엣지) | ✅ | 규칙추출 위임엣지, ladybug 2-hop 순회 |
| SQL Agent | ✅ | NL→QuerySpec→검증→파라미터SQL→읽기전용. 보안 자체점검 통과 |
| Document Agent | ✅ | BM25(kiwi)+bge-m3+reranker 하이브리드, RRF |
| Graph Agent | ✅ | Cypher 템플릿 3종, 읽기전용 |
| 라우터 | ✅ | LLM few-shot + 규칙 폴백, 스모크 4/4 |
| 오케스트레이터 | ✅ | 라우팅→분해→실행→근거게이트→정책, e2e |
| 거절 정책 (사유 8종) | ✅ | PRIVACY_RESTRICTED·OUT_OF_SCHEMA·GRAPH_PATH_NOT_FOUND 등 관측 |
| LLM 백본 | ✅ | vLLM gpt-oss-20b, OpenAI 호환 |
| trace 계측 | ✅ | 도구 호출 1줄 + `_run` 종결줄, kind(llm/tool) 구분 |
| FastAPI 데모 | ✅ | POST /query 응답 URL + 단일페이지 UI |
| 평가 하네스 | ✅ | 60문항 × 거절 ON/OFF, trace→지표 |

미구현(v0.2): MCP 서버(도구 스키마는 계약 형태로 설계), encoder 라우터, verifier 전체, ReAct 베이스라인.

---

## 2. 코드 지도

```
src/rca/
├── state.py         RunState·Budget·ToolCall + 사유코드, validate_assignment
├── trace.py         Tracer (JSONL, 질의 스코프) · read_traces
├── llm.py           vLLM 클라이언트 (urllib, OpenAI 호환)
├── router.py        5-way 라우터 (LLM few-shot + 규칙 폴백)
├── orchestrator.py  파이프라인 — 라우팅·실행·근거게이트·정책·답변생성
├── api.py           FastAPI 데모
└── tools/
    ├── sql.py       SQL Agent (보안 경계)
    ├── docs.py      Document Agent (하이브리드 검색)
    └── graph.py     Graph Agent (Cypher 템플릿)
scripts/
├── fetch_kdca.py            발생통계 수집
├── fetch_law.py             법령 3종 수집
├── build_disease_hub.py     조인 허브
├── build_sqlite.py          정형 스토어
├── build_document_chunks.py 문서 청킹
└── build_graph.py           지식그래프
eval/
├── qa/gold.jsonl   60문항 (답변가능 44 + 답변불가 16, twin 6)
├── run_eval.py     2조건 실행
└── analyze_eval.py trace → 지표
```

각 `src/rca/*.py`·`tools/*.py`는 `__main__` 자체 점검을 갖는다. `scripts/*.py`는 실행 시 검증 수치를 출력한다.

---

## 3. 핵심 구현 셋

### 3.1 계측 우선 (`trace.py`)

도구 호출 1회 = JSONL 1줄, 질의 1건 = `_run` 종결줄. 모든 지표가 이 로그에서만 나온다.
`ok`(도구 실행 성공)와 근거 충분을 구분하고, `cited`(실제 인용)를 검색된 전체 근거와 분리한다.
`answer_confidence` 연속 점수가 있어야 risk–coverage 곡선(AURC)을 적분할 수 있다.

### 3.2 SQL 보안 (`tools/sql.py`)

사용자 입력이 DB에 닿는 유일한 지점. 축소 불가:
읽기전용(mode=ro) · SELECT만 코드 생성 · 값 100% 파라미터 바인딩 · 테이블/컬럼/질병/지역/연도 화이트리스트 ·
개인 판정 질의 차단(집계 질의는 통과) · 전국/기타 집계행을 시도와 못 섞음(이중집계 방지).
Codex 보안 리뷰 4건 반영, injection·PII·schema-escape·이중집계 자체점검 통과.

### 3.3 규칙추출 그래프 (`build_graph.py`)

그래프를 LLM으로 만들지 않는다. 시행령 본문의 `법\s*제(\d+)조` 위임 패턴을 정규식으로 뽑아
법 조문과 대조(125엣지, dangling 1 제외). "LLM이 만든 부정확한 그래프" 비판을 차단하고 재현성을 확보한다.

### 3.4 조건은 config (`orchestrator`·`run_eval`)

거절 ON/OFF 등 실험 조건이 같은 런타임을 공유한다. `if system == ...` 분기 없이 파라미터로.

---

## 4. 기술 스택 (역할과 함께)

| 계층 | 기술 | 왜 |
|---|---|---|
| 언어·API | Python 3.11 · FastAPI · Pydantic v2 | 잘못된 사유코드는 Literal+validate_assignment에서 즉시 |
| 오케스트레이션 | Custom state machine | 예산·재시도·거절을 명시 제어. 안 쓴 프레임워크(LangGraph)는 스택에 없음 |
| 정형 | SQLite (읽기전용) | 임베디드, 서버는 config 한 줄 |
| 벡터 | Qdrant local mode | 서버 불필요, FastAPI workers=1 |
| 그래프 | **Ladybug 0.18** (임베디드 openCypher) | Kùzu 상류 아카이브(2025-10) 후속 포크 |
| 임베딩 | BGE-m3-ko | 한국어 dense (CPU 고정) |
| 어휘검색 | bm25s + kiwipiepy | 한국어 형태소 토크나이저 필수 |
| 리랭커 | bge-reranker-v2-m3-ko | 한국어 cross-encoder (CPU) |
| LLM | vLLM gpt-oss-20b (OpenAI 호환) | 로컬 서빙, 코드가 로컬/API 무관 |
| 계측 | JSONL + 순수 파이썬 | append-only, git diff, 소규모에 DB 불필요 |

### 그래프 백엔드 정정

초기 문서는 "Kùzu↔Neo4j openCypher라 쿼리 문자열 공유, 교체 비용 커넥션 함수 2개"라고 적었으나 둘 다 사실이 아니었다(Kùzu 아카이브 → Ladybug, 그리고 DDL·최단경로 문법·walk/trail 차이로 쿼리 미이식). Ladybug 하나로 고정, Neo4j는 미검증 표기. [ADR-1](../ARCHITECTURE_v1.md#7-데이터-계층) 참조.

---

## 5. 개발 방식 — 작성 → 리뷰 → 수정

코드마다 별도 리뷰어(Codex)를 돌렸다. SQL Agent 보안 리뷰에서 잡힌 실제 결함:
run() 화이트리스트 우회, regions 이중집계, 폭주, PII 호칭 없는 케이스 — 전부 반영.
계측 계층은 3라운드 리뷰로 "실행은 되는데 측정이 틀린" 부류(AURC 연속점수 부재, step 오귀속, 동점 처리)를 잡았다.
리뷰를 기계적으로 수용하지 않은 사례도 남겼다(`ok=False` 회귀 지적 → 정의 명시로 대체).

---

## 6. 실행

```bash
pip install -r requirements.txt
# 데이터 구성 → 데모 → 평가: README '실행' 참조
PYTHONPATH=src uvicorn rca.api:app --port 8000 --workers 1
```

e2e 확인:
```
"2023 시도별 수두"  → SQL   → 충북7505·서울3135··· (실데이터 + LLM 답변)
"제2급 신고기한"    → DOC   → "24시간 이내"
"제8조의2 위임"     → GRAPH → 시행령 제1조의3/4/5 (2-hop)
"김철수 확진 여부"  → PRIVACY_RESTRICTED
"2030 수두 전망"    → ABSTAIN
```

---

## 7. 한계 (정직하게)

- 계획형 오케스트레이션이지 자율 multi-agent가 아니다 (ReAct는 v0.2 베이스라인).
- 그래프는 위임 엣지 위주, 조문 내부 인용은 오탐 커서 제외.
- 평가는 라우팅·거절 지표 중심. 답변 내용 채점(EM/F1·수동)은 별도.
- 도메인 하나로 도메인 독립성을 "주장"하며, 두 번째 도메인 실증은 v0.2.
- LLM 답변생성이 공유 GPU에서 느림(~20초/건). 데모는 사전 캐시·영상 병행 권장.
