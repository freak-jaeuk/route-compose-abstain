# 구현 현황과 기술 스택

이 문서는 **지금 저장소에 실제로 들어 있는 코드**만 다룬다. 설계 의도는 [ARCHITECTURE_v1.md](../ARCHITECTURE_v1.md), 평가 계획은 [RESEARCH_PLAN_v2.md](RESEARCH_PLAN_v2.md) 에 있다.

- Python **642줄** (`src/rca` 481 + `eval/analyze.py` 161)
- 런타임 의존성 **pydantic 하나**. 나머지는 표준 라이브러리
- 커밋 6개, `python -m rca.demo && python eval/analyze.py` 로 끝까지 실행됨

---

## 1. 지금 동작하는 것 / 아직 아닌 것

정직하게 나눈다. **데이터 계층은 전부 stub이다.**

| 계층 | 상태 | 근거 |
|---|---|---|
| 실행 상태 모델 (`RunState`·`Budget`·`ToolCall`) | ✅ 동작 | `python src/rca/state.py` 자체 점검 통과 |
| trace JSONL 계측 | ✅ 동작 | `python src/rca/trace.py` 자체 점검 통과 |
| 오케스트레이션 루프 (라우팅→계획→실행→정책) | ✅ 동작 | 8문항 × 2조건 실행 |
| 규칙 기반 라우터 (5-way + confidence) | ✅ 동작 | route F1 0.800 |
| 거절 정책 (사유 코드 8종) | ✅ 동작 | `OUT_OF_SCHEMA` 2건, `PRIVACY_RESTRICTED` 1건 관측 |
| 예산 관리 (step·토큰·시간·도구별) | ✅ 동작 | `Budget.exhausted` 경계 5종 검증 |
| trace → 지표 분석기 (9종) | ✅ 동작 | `python eval/analyze.py` |
| SQL Agent (실제 SQLite) | ⛔ stub | 상수 dict + 스키마 위반 예외만 흉내 |
| Document Agent (BM25 + bge-m3 + rerank) | ⛔ stub | 키워드 매칭 3건 |
| Graph Agent (Kùzu/Cypher) | ⛔ stub | 경로 문자열 1건 |
| MCP Tool Server | ⛔ 미구현 | 도구 입출력 스키마만 문서에 확정 |
| LLM 백본 | ⛔ 미연결 | 토큰 수는 상수로 주입 |

**stub 수치는 연구 결과가 아니다.** 지금 검증한 것은 하나다 — 논문에 필요한 지표 전부가 실행 로그만으로 계산되는가. 성립했으므로, 실제 도구가 붙어도 계측·정책·오케스트레이터는 다시 만들지 않는다.

---

## 2. 코드 지도

```
src/rca/
├── state.py    112줄   RunState · Budget · ToolCall · Evidence + 사유 코드 타입
├── trace.py    152줄   Tracer (JSONL writer) · read_traces
└── demo.py     217줄   stub 도구 5종 + 규칙 라우터 + 오케스트레이션 루프
eval/
├── analyze.py  161줄   trace → 지표 9종 + 지표 함수 자체 점검
└── qa/demo_gold.jsonl  8문항 (contrastive twin 2쌍 포함)
```

의존 방향은 단방향이다. `state ← trace ← demo`, `analyze → trace`.
`demo.py`의 stub 함수만 교체하면 실제 시스템이 된다 — 나머지 파일은 손대지 않는다.

---

## 3. 핵심 구현 셋

### 3.1 계측 우선 (`trace.py`)

이 저장소의 중심 결정이다. **도구 호출 1회 = JSONL 1줄, 질의 1건 = `_run` 종결 줄 1개.**

```python
with tr.query(qid) as summary:                    # step 번호를 1부터 재시작
    with tr.step("query_structured_data", spec) as rec:
        rec["output"] = tool_query_structured_data(spec)
        rec["tokens_in"], rec["tokens_out"] = 320, 60
    summary.update(verdict="ANSWER", answer_confidence=0.72, cited=[...])
```

`step()`은 컨텍스트 매니저라 지연시간·예외가 자동으로 잡힌다. 예외는 `ok=False` 로 남기고 그대로 전파한다 — **실패도 실험 데이터다.**

세 필드를 따로 두는 이유가 각각 명확하다:

| 필드 | 없으면 못 하는 것 |
|---|---|
| `_run.elapsed_ms` | 도구 지연시간 합에는 라우터·검증기·정책이 빠진다. p95를 과소평가한다 |
| `_run.answer_confidence` | `verdict`만으로는 risk–coverage 곡선에 점이 하나뿐이다. AURC를 적분할 수 없다 |
| `_run.cited` | 검색된 근거와 실제 인용된 근거가 구분되지 않으면 citation precision을 정의할 수 없다 |

`ok`의 의미도 못박았다. **도구 실행 성공 여부이며 근거 충분 여부가 아니다.** 검색 결과가 비어 거절된 경우는 `ok=True` + `_run.abstain_reason=INSUFFICIENT_EVIDENCE` 로 남는다. 섞으면 SQL invalid query rate가 "쿼리는 멀쩡했는데 근거가 없던" 사례로 오염된다.

동시성은 계약으로 처리했다 — **Tracer 1개 = 파일 1개 = 프로세스 1개.** 락도 fsync도 두지 않는 대신 조건별로 파일을 분리하고, 분석 단계에서 `qid` 수가 gold 문항 수와 맞는지 확인해 누락 실행을 잡는다.

`read_traces()`는 깨진 줄을 조용히 버리지 않고 `(rows, corrupt)` 로 함께 돌려준다. 프로세스가 죽어 잘린 마지막 줄 하나 때문에 전체 분석이 막히지 않는다.

### 3.2 상태와 예산 (`state.py`)

컴포넌트 사이에 전역 변수나 암묵 공유가 없다. `RunState` 하나를 들고 다닌다.

예산 판정은 한 함수에만 있다.

```python
def exhausted(self, steps, elapsed_ms, next_tool=None) -> bool:
    """다음 호출을 시작할 수 있는가. 모든 한도가 도달 즉시(>=) 정지한다."""
```

`exceeded`에서 `exhausted`로 이름을 바꾼 이유가 있다. 원래 step·토큰·시간은 `>=`인데 도구별 호출 한도만 `>`였다. 경계에서 정책이 일관되지 않으면 `BUDGET_EXCEEDED` 발생 조건을 논문에 쓸 수 없다. 자체 점검이 경계 5종을 전부 확인하며, 도구별 한도는 step 한도에 가려지지 않도록 넉넉한 예산에서 따로 검증한다.

응답은 3분기이고 사유 코드가 붙는다.

```
ABSTAIN  8종  OUT_OF_SCHEMA · INSUFFICIENT_EVIDENCE · LOW_ROUTER_CONFIDENCE
              SQL_EXECUTION_FAILURE · GRAPH_PATH_NOT_FOUND · SOURCE_CONFLICT
              PRIVACY_RESTRICTED · BUDGET_EXCEEDED
CLARIFY  4종  MISSING_TIME_RANGE · MISSING_REGION · AMBIGUOUS_ENTITY
              MULTIPLE_INTERPRETATIONS
```

`CLARIFY`에 사유가 없으면 "무엇이 부족했는지"를 사후에 복원할 수 없다. 오류 분석 표가 그대로 하나 사라진다.

### 3.3 조건은 config, 코드 분기가 아니다 (`demo.py`)

실험 조건마다 `if system == "react"` 를 넣으면 조건 수만큼 코드 경로가 갈라지고, 비교 자체가 무의미해진다. 가변 축을 넷으로 고정했다.

```python
SYSTEMS = [
    {"name": "proposed", "router": "rules",  "threshold": 0.50, "abstention": True},
    {"name": "doc_only", "router": "fixed", "route": "DOCUMENT", "abstention": False},
]
```

도구 호출도 세 갈래로 복사돼 있던 것을 테이블 + 헬퍼 하나로 합쳤다.

```python
LEGS = {
    "SQL":      ("query_structured_data", parse_spec, tool_query_structured_data, (320, 60)),
    "DOCUMENT": ("retrieve_documents",   ...,        tool_retrieve_documents,     (480, 90)),
    "GRAPH":    ("query_knowledge_graph", ...,       tool_query_knowledge_graph,  (260, 55)),
}

for leg in plan:
    tool, build, fn, tokens = LEGS[leg]
    if st.budget.exhausted(st.steps, elapsed(), next_tool=tool):
        ...ABSTAIN(BUDGET_EXCEEDED)
    out = call_tool(tr, st, tool, build(st.question), fn, tokens)
    st.cited += collect_cited(leg, out, cfg["abstention"])
```

`call_tool`은 trace 기록과 `RunState.steps` 반영을 한곳에서 한다. 이게 갈라져 있으면 예산이 영원히 발동하지 않는다 (§5에서 실제로 그랬다).

### 3.4 지표 산출 (`analyze.py`)

입력은 trace 로그뿐이다. **시스템을 다시 실행하지 않는다.**

| 지표 | 유도 |
|---|---|
| Router Macro-F1 | `route_pred` vs gold `route_label` |
| Coverage | `verdict == ANSWER` 비율 |
| Selective accuracy | 답한 것 중 정답 |
| Abstention P/R | `verdict == ABSTAIN` vs gold `answerable` |
| **AURC** (주지표) | `answer_confidence` 내림차순 위험 적분 |
| 질의당 호출수·토큰 | `tool != "_run"` 행 집계 |
| p95 지연 | `_run.elapsed_ms` nearest-rank |
| 거절 사유 분포 | `_run.abstain_reason` |

지표 함수 셋은 미묘해서 자체 점검을 붙였고 표를 뽑을 때마다 함께 돈다.

```python
assert aurc([(0.9, True), (0.1, False)]) < aurc([(0.9, False), (0.1, True)])
tied = [(0.5, True), (0.5, False)]
assert aurc(tied) == aurc(tied[::-1])            # 동점은 순서에 무관해야 한다
assert macro_f1([("SQL", "SQL"), ("SQL", "GRAPH")]) < 0.5   # 없는 클래스 예측도 벌점
assert pctl([1,2,3,4,5,6,7,8,9,10], 0.95) == 10  # nearest-rank
```

동점 처리가 특히 중요했다. 임계값 스윕은 같은 점수를 구분하지 못하므로 동점 구간을 한꺼번에 편입해야 한다. 안 하면 **로그 줄 순서만 바뀌어도 주지표 값이 변한다.**

라우팅 필드가 빠진 줄은 기본값으로 메우지 않는다. 경고를 찍고 제외한다 — 결측을 `ABSTAIN`으로 메우면 라우터가 내지 않은 예측에 점수를 주게 된다.

---

## 4. 기술 스택

### 지금 실제로 쓰는 것

| 기술 | 용도 |
|---|---|
| **Python 3.11** | `X \| None` 유니온, `match` 없이도 충분한 타입 표현 |
| **pydantic v2** | `RunState` 등 전 상태 모델. 잘못된 사유 코드는 `Literal` 에서 즉시 걸린다 |
| **JSONL** | 실험 로그. append-only, 스트리밍 가능, `git diff` 가능 |
| stdlib `contextlib` | `Tracer.step`/`query` 컨텍스트 매니저 |
| stdlib `time.perf_counter` | 단조 시계. `time.time()`은 시스템 시각 변경에 영향받는다 |

pandas·numpy 없이 지표를 계산한다. 8문항 규모에서 의존성을 늘릴 이유가 없고, 400문항에서도 마찬가지다.

### 붙일 예정 (`requirements.txt`에 선언, 아직 미사용)

| 역할 | 선택 | 왜 |
|---|---|---|
| 정형 | SQLite (읽기전용) → PostgreSQL | 임베디드로 시작, 서버는 config 한 줄 |
| 벡터 | **Qdrant local mode** | `path=` 로 서버 없이 동작. 서버 모드와 API 동일 |
| 그래프 | **Kùzu** → Neo4j | 둘 다 openCypher. **쿼리 문자열을 공유**하므로 백엔드 교체 비용이 커넥션 함수 2개뿐 |
| 임베딩 | bge-m3 (sentence-transformers) | 한국어 성능, 하이브리드 검색에 dense 축 |
| 어휘 검색 | rank-bm25 + kiwipiepy | 한국어 형태소 토크나이저 없이 BM25는 무의미 |
| API | FastAPI | 데모 + MCP 도구 노출 |

### 임베디드를 기본값으로 한 이유

개발 박스에 Docker가 없다. 리뷰어 환경도 보장 못 한다. `pip install` 후 바로 도는 것이 재현성의 실질적 조건이다.

Kùzu는 Neo4j 대비 생태계(APOC, GDS)가 얇다. 순회·최단경로만 쓰므로 영향 없다고 판단했고, 이 판단 근거를 [ARCHITECTURE §7 ADR-1](../ARCHITECTURE_v1.md#7-데이터-계층)에 남겼다.

---

## 5. 개발 방식 — 작성 → 리뷰 → 수정 → 재리뷰

코드를 쓸 때마다 별도 리뷰어(Codex)를 돌렸다. 3라운드에서 나온 것 중 실제 결함이었던 것:

| 라운드 | 지적 | 왜 심각했나 |
|---|---|---|
| 1 | AURC를 `verdict`로 계산 불가 | **주지표를 못 뽑을 뻔했다.** 연속 점수 필드 추가 |
| 1 | 인용 정밀도 계산 불가 | 검색된 근거와 인용된 근거 미구분 |
| 1 | p95 지연 과소평가 | 도구 지연 합에 라우터·검증기 누락 |
| 1 | step 번호 오귀속 | Tracer 하나가 여러 질의를 쓰면 번호가 계속 증가 |
| 1 | `Budget` 경계 불일치 | 도구 한도만 `>` |
| 2 | **도구 호출이 `steps`에 안 들어감** | `exhausted`가 항상 빈 목록을 봄 → 예산이 영원히 발동 안 함. **스모크가 거짓 통과하던 경로** |
| 2 | PII 차단 경로가 `route_pred` 미설정 | 분석 쪽 결측 보정이 라우터에 없는 공을 줌 |
| 2 | `aurc` 동점 미처리 | 로그 순서만 바뀌어도 주지표가 변함 (실제로 0.508→0.594 이동) |
| 2 | `macro_f1` 부풀림 · `pctl` 규약 오류 | 지표 계산 자체가 틀림 |

3라운드 마지막 지적 1건(`ok=False` 회귀)은 되돌리지 않고 **정의를 명시하는 쪽**을 택했다. 빈 검색 결과는 도구 실패가 아니라 검증 판정이며, `ok=False`로 찍으면 invalid query rate가 오염되기 때문이다. 리뷰 지적을 무조건 수용하지 않고 판단 근거를 문서에 남긴 사례다.

리뷰가 잡은 것 대부분이 **"실행은 되는데 측정이 틀린"** 부류였다. 계측이 시스템의 산출물인 프로젝트에서는 이쪽이 크래시보다 위험하다.

---

## 6. 실행

```bash
pip install pydantic                      # 현재 필요한 전부

PYTHONPATH=src python src/rca/state.py    # 상태·예산 자체 점검
PYTHONPATH=src python src/rca/trace.py    # 계측 자체 점검
PYTHONPATH=src python -m rca.demo         # 8문항 × 2조건 → eval/runs/*.jsonl
python eval/analyze.py                    # trace → 지표 표
```

실제 출력:

```
문항 8개 · 조건 2개 · trace 32줄

| system   | route F1 | coverage | sel.acc | abst.P | abst.R | AURC↓ | calls/q | tok/q |
|----------|----------|----------|---------|--------|--------|-------|---------|-------|
| doc_only |    0.080 |    1.000 |   0.250 |      — |  0.000 | 0.594 |    1.00 |   570 |
| proposed |    0.800 |    0.625 |   1.000 |  1.000 |  1.000 | 0.118 |    1.00 |   348 |

거절 사유 분포
  doc_only  없음
  proposed  {'OUT_OF_SCHEMA': 2, 'PRIVACY_RESTRICTED': 1}
```

`p95 ms`가 0인 것은 stub이 1ms 미만에 반환하기 때문이다. 실제 도구가 붙으면 채워진다.

### 배선이 실제로 동작하는지 보이는 예

```
qa_001  "…지역별 보험금 지급액 합계"    → SQL 라우팅 → 실행 성공 → ANSWER
qa_004  "…읍면동별 보험금 지급액 합계"  → SQL 라우팅 → OUT_OF_SCHEMA → ABSTAIN
```

두 질문은 어절 하나 차이이고 라우팅도 같다. **스키마에 실제로 닿아야만 갈린다.** 표면 단서로 걸러지지 않는 답변불가 질의를 만들 수 있다는 뜻이고, 이 성질이 거절 성능 측정의 전제다.

---

## 7. 다음 작업

| # | 작업 | 선행 조건 |
|---|---|---|
| 1 | route 라벨링 프로토콜 · contrastive 규칙 확정 | — (데이터 만들기 시작하면 되돌릴 수 없음) |
| 2 | SQL Agent 실구현 — QuerySpec → 검증 → 파라미터 SQL | 1 |
| 3 | Document Agent 실구현 — 파싱·청킹·BM25+dense·rerank | 1 |
| 4 | Graph Agent 실구현 — Kùzu 적재 + Cypher 템플릿 6종 | 1 |
| 5 | MCP Tool Server — 도구 5종 노출 | 2·3·4 |
| 6 | ReAct 베이스라인 — 같은 도구를 LLM이 자유 선택 | 5 |

보안 경계(읽기 전용 · SELECT 전용 · 파라미터 바인딩 · 화이트리스트 · PII 차단 · 자원 상한)는 [ARCHITECTURE §10](../ARCHITECTURE_v1.md#10-보안-경계-축소-금지-영역)에 명세돼 있고 2번에서 함께 구현한다. 축소 대상이 아니다.
