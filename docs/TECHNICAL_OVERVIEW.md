# 기술 개요 — Route·Compose·Abstain (감염병 도메인)

> ⚠️ **이 문서는 v0.1 완성 시의 목표 상태(target state)를 기술한다.** 면접·발표에서 플랫폼을 설명하기 위한 기준 문서다.
> 현재 실제 구현 상태(무엇이 동작하고 무엇이 stub인가)는 [IMPLEMENTATION.md](IMPLEMENTATION.md)가 사실대로 기록한다. 이 문서를 "이미 다 됐다"로 읽지 말 것.

---

## 0. 한 줄 정의

> 이질적인 공공데이터(정형 통계 · 법령 문서 · 지식 그래프)에 흩어진 지식을, 질의별로 필요한 정보원만 선택·조합하고, 근거가 부족하면 답하지 않는 다중 소스 질의응답 플랫폼.

도메인은 **감염병**이다: 질병관리청 발생통계(SQL) + 감염병예방법·시행령(문서) + 급수·신고체계 지식그래프. 도메인은 데이터 어댑터일 뿐이고, 아키텍처 자체는 도메인 독립이다 — 같은 코드에 보험·산재 데이터를 꽂으면 그 도메인이 된다.

**핵심 주장 한 문장**: 단일 검색 경로나 모든 도구를 항상 호출하는 방식은, 정형·비정형·관계형 데이터가 섞인 환경에서 비효율적이고 신뢰하기 어렵다. 질의마다 경로를 고르고, 경로별로 근거를 검증하고, 못 하면 거절하는 편이 낫다.

---

## 1. 전체 흐름 (질의 → 응답)

```
사용자 질의
  │
  ▼
Query Analyzer      의도·시간범위·감염병명·개인정보 요구 여부 추출
  │
  ▼
Router (5-way)      SQL │ DOCUMENT │ GRAPH │ COMPOSITE │ ABSTAIN  + confidence
  │
  ▼
Orchestrator        COMPOSITE면 하위질의 분해, 도구 호출 순서 결정, 예산 관리
  │
  ▼
MCP Tool Layer      query_structured_data │ retrieve_documents │ query_knowledge_graph
  │                 (각 도구는 실행 전후로 trace JSONL 1줄을 남긴다)
  ▼
Verifier            SQL 실행 성공? 문서 근거 충분? 그래프 경로 유효? 소스 간 충돌?
  │
  ▼
Response Policy     ANSWER │ CLARIFY │ ABSTAIN(+사유코드)
  │
  ▼
응답 = 답변 + 인용 근거 + 사용 경로 + 비용 요약 + 출처 표기
```

핵심 설계 원칙 5개:

| 원칙 | 의미 |
|---|---|
| 계측 우선 | 모든 도구 호출이 trace JSONL 1줄을 남긴다. 모든 정량 지표는 이 로그에서만 나온다 |
| 거절은 실패가 아니다 | ABSTAIN/CLARIFY는 정상 종료 상태. 근거 없는 답변보다 우선 |
| 단일 백본 | 모든 실험 조건이 동일 LLM·온도·시드. 차이는 오케스트레이션뿐 |
| 조건은 config | 베이스라인 비교를 `if system==...` 코드 분기가 아니라 설정으로 |
| 임베디드 우선 | 외부 서버 없이 `pip install` 후 바로 실행. 재현성 = 리뷰어가 30분에 돌리는가 |

---

## 2. 데이터 계층 — 세 스토어가 하나의 엔티티를 공유한다

이 플랫폼이 성립하는 유일한 이유: **세 스토어가 조인 허브를 공유**한다. 공유 안 하면 COMPOSITE가 불가능하고 그냥 도구 3개 나열이 된다.

조인 허브 = **`disease_type` (법정감염병 종류)**. 감염병예방법 제2조가 급수별로 감염병을 열거하고(1급 17종·2급 21종·3급 27종·4급 23종, 규칙 파싱으로 추출), 통계도 같은 질병명으로 집계된다.

| 경로 | 저장소 | 소스 | disease_type이 나타나는 곳 |
|---|---|---|---|
| 정형 | SQLite (임베디드) | 질병청 지역별 감염병 발생통계 (data.go.kr 15053802) | 질병명 컬럼 (연도×시도×질병×발생) |
| 비정형 | Qdrant local + BM25 | 감염병예방법 + 시행령 + 시행규칙 (법제처 API) | 조문 제목·본문 |
| 그래프 | Ladybug (임베디드 Cypher) | 위 법령의 조문 구조 + 급수 분류 | 노드 |

**조인 허브 CSV 8열이 아니라 88행**: `(sql_disease_name, law_grade, article_no, surveillance_type)`. 이 파일 하나가 SQL 컬럼값 ↔ 법 조문 ↔ 그래프 노드를 잇는다. 손으로 만들고 전수 검증한다.

흥미로운 구조 하나: **4급은 전수감시 통계에 없다** (표본감시 별도). "왜 결핵은 전수신고인데 감기는 표본감시인가"는 법 조문으로만 답이 나오는 질문이고, 통계·법·그래프를 동시에 요구하는 좋은 COMPOSITE 예시가 된다.

---

## 3. 라우터 — 파인튜닝 없이 4종 비교

질의를 5개 경로로 분류하고 confidence를 낸다. 파인튜닝하지 않고, 동일 프롬프트·예시 조건에서 네 변형을 비교한다.

| 변형 | 구현 | 비용 | 역할 |
|---|---|---|---|
| `rules` | 정규식 + 스키마 어휘 매칭 | ~0 | 하한선 |
| `encoder` | bge-m3 임베딩 → logistic regression | ~0 | 비용 대비 성능 후보 |
| `llm` | few-shot + structured output (JSON schema strict) | 높음 | 통상적 접근 |
| `oracle` | gold 라벨 주입 | — | 상한선 |

라우팅 판단 기준(충돌 시 순서 적용):
1. 답이 **통계 값의 집계**로만 나오면 SQL ("2023년 시도별 결핵 발생 건수")
2. 답이 **조문의 서술**로만 나오면 DOCUMENT ("결핵의 신고 기한은?")
3. 답이 **두 엔티티 사이 관계 경로**를 요구하면 GRAPH ("장티푸스 신고의무를 위임한 하위 규정은?")
4. 위 중 둘 이상이 모두 필요하면 COMPOSITE
5. 근거가 어디에도 없으면 ABSTAIN

confidence가 임계값(dev에서 보정) 미만이면 `LOW_ROUTER_CONFIDENCE`로 거절한다.

---

## 4. SQL 경로 — 보안이 축소 불가 영역

자연어를 바로 SQL로 만들지 않는다. 중간 표현(QuerySpec)을 거친다.

```
자연어 질의
  → QuerySpec 생성 (테이블·집계함수·필터·기간을 구조화)
  → 스키마·컬럼 화이트리스트 검증
  → 파라미터 바인딩 SQL 생성 (SELECT 전용)
  → 읽기전용 실행
  → 결과 행·값 범위 검증
```

보안 통제(전부 필수, 하나도 못 뺀다):

| 통제 | 구현 |
|---|---|
| 읽기 전용 | SQLite `file:db?mode=ro` |
| SELECT 전용 | QuerySpec→SQL 생성기가 SELECT만 만든다. 자유 SQL 실행 경로 없음 |
| 다중 문장 차단 | 세미콜론 포함 시 거부 |
| 화이트리스트 | 스키마 레지스트리에 없는 테이블·컬럼은 `OUT_OF_SCHEMA` |
| 파라미터 바인딩 | 값은 100% 바인딩. 문자열 연결 금지 |
| PII 차단 | 개인 식별 질의 패턴 + 컬럼 blocklist → `PRIVACY_RESTRICTED` |
| 자원 상한 | statement timeout, LIMIT 강제, 결과 행수 cap |

감염병 공개통계는 집계라 개인정보 컬럼이 0건이다. 그래서 PII 통제는 **질의 차단으로 시연**한다 — "환자 홍길동의 진단 이력" 같은 개인 식별 질의는 스키마에 개인 테이블이 없으므로 실행 전에 차단한다. README에 "데이터에 개인정보 없음, 개인 식별 질의는 실행 전 차단"으로 정직하게 명시한다. 이게 오히려 더 나은 설계다.

---

## 5. Document 경로 — Hybrid Retrieval

법령은 조·항·호·목이 API 필드로 구조화되어 오므로, 청킹이 파싱 문제가 아니라 필드 매핑 문제다.

```
법령 JSON (법제처 API)
  → 조문 단위 청킹 (조가 길면 항 단위 분할)
     메타: {law_id, article_no, article_title, effective_date}
  → 색인
     ├── BM25 (bm25s + kiwipiepy 형태소 토크나이저)   ← 한국어는 형태소 없이 BM25 무의미
     └── Dense (BGE-m3-ko 임베딩, Qdrant local)
  → 질의 시 두 경로 병렬 → RRF 융합
  → Cross-Encoder rerank (bge-reranker-v2-m3, top-50 → top-5)
  → 근거 구간(span) 반환
```

파싱에서 실제로 걸리는 것(리스크 낮지만 처리 필요):
- **조문가지번호** — 제11조와 제11조의2가 같은 키로 충돌하지 않게
- **항 필수필드 결측** — 정의 조문은 항번호 없이 호만 있는 경우
- 항 없는 조문 폴백

top-k·RRF 가중치 튜닝은 dev 분할에서만 하고 그 기록이 "Retriever 최적화" 근거자료가 된다.

---

## 6. Graph 경로 — Cypher 템플릿 + 규칙 추출 그래프

**그래프를 LLM으로 만들지 않는다.** 법령의 조문 간 관계가 규칙으로 추출 가능하기 때문이다 — 이게 재현성의 핵심이고 "LLM이 만든 부정확한 그래프"라는 리뷰 반박을 원천 차단한다.

```
노드: Law · Article · DiseaseType · Grade · SurveillanceType · Region · Year
엣지:
  Law -[:CONTAINS]-> Article
  Article -[:DELEGATES_TO]-> Article      (시행령 본문의 "법 제N조" 위임 패턴, 정규식)
  DiseaseType -[:CLASSIFIED_AS]-> Grade    (법 제2조 열거, 조인 허브)
  DiseaseType -[:REPORTED_VIA]-> SurveillanceType  (전수/표본)
  DiseaseType -[:DEFINED_IN]-> Article
```

위임 엣지는 시행령 조문 본문에서 `법\s*제(\d+)조(?:의(\d+))?` 정규식으로 뽑아 법 조문 집합과 대조한다. 자유 Cypher 생성 대신 관계 유형별 **템플릿 3종**을 두고, LLM은 템플릿 선택 + 파라미터만 채운다. 쓰기 절(CREATE/MERGE/DELETE)은 차단한다.

그래프여야만 답이 나오는 질문:
> "제3급감염병의 신고 의무를 규정한 조문이 시행령에 위임한 세부 사항은?"
> → `Article -[:DELEGATES_TO]-> Article` 2-hop. 문서검색은 두 문서를 따로 찾아 사람이 이어야 하지만 그래프는 위계를 직접 반환한다.

**백엔드 주의**: openCypher를 쓰지만 Ladybug와 Neo4j의 쿼리가 그대로 호환되지는 않는다(스키마 DDL 필수, 최단경로 문법 상이, walk/trail 시맨틱 차이). 백엔드를 Ladybug 하나로 고정하고, Neo4j 전환은 "Graph Agent 재작성 필요"로 표기한다. (이 판단은 초기에 "openCypher라 쿼리 문자열 공유"로 잘못 적었다가 실측으로 정정한 것이고, 그 정정 자체가 기술 리스크 식별 사례다.)

---

## 7. Composite — 진짜 다중 소스 질의

두 개 이상 소스가 필요한 질문을 하위질의로 분해하고, 결과를 통합하고, 소스 간 정합을 검사한다.

예시:
> "결핵의 법정감염병 등급과 신고 기한을 설명하고, 최근 3년 시도별 발생 추이를 보여줘"

```
분해:
  q1(DOCUMENT) → 감염병예방법에서 결핵 등급·신고규정 조문
  q2(SQL)      → 15053802에서 결핵 2021~2023 시도별 집계
  (조인 허브가 q1의 "결핵"과 q2의 질병명이 같은 엔티티임을 보장)
통합 → Verifier가 조문 근거와 통계 값을 대조
응답 → 서술(조문 인용) + 수치(집계) + 출처
```

조인 허브가 없으면 q1의 "결핵"과 q2의 "결핵"이 같은 것인지 시스템이 모른다. 그래서 88행 CSV가 플랫폼의 존재 이유 전부다.

---

## 8. 응답 정책 — 3분기 + 사유 코드

| 판정 | 조건 |
|---|---|
| `ANSWER` | 검증된 근거가 충분하고 소스 간 일관 |
| `CLARIFY` | 연도·지역·질병 등 한정조건 부족 |
| `ABSTAIN` | 아래 사유 코드 중 하나 |

거절 사유 8종: `OUT_OF_SCHEMA` · `INSUFFICIENT_EVIDENCE` · `LOW_ROUTER_CONFIDENCE` · `SQL_EXECUTION_FAILURE` · `GRAPH_PATH_NOT_FOUND` · `SOURCE_CONFLICT` · `PRIVACY_RESTRICTED` · `BUDGET_EXCEEDED`
명확화 사유 4종: `MISSING_TIME_RANGE` · `MISSING_REGION` · `AMBIGUOUS_ENTITY` · `MULTIPLE_INTERPRETATIONS`

사유를 코드로 남기지 않으면 "무엇이 부족했는지"를 사후에 복원할 수 없다 — 오류 분석 표가 그대로 사라진다.

**근거 게이트**: 답변은 최소 1개의 인용 source_id를 포함해야 하고, LLM 자가 판정("이 답이 근거에 의해 지지되는가")이 no면 ABSTAIN한다. 이게 있어야 "검색은 됐으나 근거 불충분" 유형의 거절이 시연 가능해진다.

---

## 9. 오케스트레이션 — 예산과 실패 복구

에이전트가 무한 루프하지 않도록 예산을 건다.

```
step 한도 초과 / 토큰·시간 한도 초과 → ABSTAIN(BUDGET_EXCEEDED)
도구 오류 → 1회 재시도 → 대체 경로 1회 → 그래도 실패면 사유 기록 후 중단
라우터 confidence < τ → ABSTAIN(LOW_ROUTER_CONFIDENCE)
소스 간 수치 불일치 → ABSTAIN(SOURCE_CONFLICT)
```

예산 판정은 한 함수(`Budget.exhausted`)에만 있고 "다음 호출을 시작할 수 있는가"를 뜻한다. 모든 한도가 도달 즉시 정지한다.

정직하게: 이 구조는 계획형(planned) 오케스트레이션 + 실패 복구 루프이지, 에이전트가 스스로 도구를 자유 선택하는 자율 multi-agent는 아니다. ReAct 자유 도구선택은 **베이스라인**으로 비교한다.

---

## 10. 관측성 — trace JSONL이 아키텍처의 핵심

도구 호출 1회 = JSONL 1줄, 질의 1건 = `_run` 종결 줄 1개. 논문·이력서의 모든 수치가 여기서 나온다.

```json
{"qid":"q_0001","system":"proposed","backbone":"gpt-oss-20b@...","seed":0,
 "step":2,"tool":"query_structured_data","route_pred":"COMPOSITE","route_conf":0.81,
 "sql":"SELECT region, SUM(cases) ...","output":{"row_count":17},
 "tokens_in":320,"tokens_out":60,"latency_ms":430,"ok":true,"kind":"tool"}
{"qid":"q_0001","step":4,"tool":"_run","elapsed_ms":2180,
 "verdict":"ANSWER","answer_confidence":0.72,"cited":["감염병예방법 제2조","15053802"]}
```

이 로그에서 유도되는 지표:

| 지표 | 유도 |
|---|---|
| Router Macro-F1, calibration | `route_pred`/`route_conf` vs gold |
| SQL invalid query rate | `ok`, `error` (도구 실행 성공 여부 — 근거 충분과 구분) |
| Recall@k, nDCG, citation precision | `cited` vs gold 근거 |
| **AURC** (주지표) | `answer_confidence` 임계값 스윕 → risk-coverage 곡선 적분 |
| Abstention P/R, selective accuracy | `verdict` vs gold `answerable` |
| p95 지연 | `_run.elapsed_ms` (도구 지연 합이 아님 — 라우터·검증기 포함) |
| 질의당 호출수·토큰·비용 | `kind=="tool"` 행 집계 |

두 필드가 왜 따로 있는지가 이 설계의 요점이다: `answer_confidence` 없이는 risk-coverage 곡선에 점이 하나뿐이라 AURC를 적분할 수 없고, `cited`(실제 인용)를 `evidence`(검색된 전체)와 구분하지 않으면 citation precision을 정의할 수 없다.

---

## 11. 실험 하네스 — 조건은 config다

8개 실험 조건이 같은 런타임을 공유한다. 가변 축은 넷:

| 축 | 값 |
|---|---|
| orchestrator | `planned` · `react` |
| router | `fixed`·`rules`·`encoder`·`llm`·`oracle`·`none` |
| verifier | `none`·`per_path` |
| abstention | `none`·`calibrated`·`oracle` |

조건: `doc_only`·`sql_only`·`graph_only`·`always_all`·`react`·`proposed`·`oracle_route`·`oracle_abstain`.

부수효과 하나: `always_all`(모든 질의를 3스토어에 전부 태움) 실행 trace 하나로 **질의×스토어 성능 행렬**과 **oracle 상한**이 추가 비용 없이 나온다.

---

## 12. 기술 스택 (역할과 함께)

| 계층 | 기술 | 왜 |
|---|---|---|
| 언어·API | Python 3.11, FastAPI, Pydantic v2 | 상태 모델의 잘못된 값은 Literal에서 즉시 걸림 |
| 오케스트레이션 | Custom state machine (LangGraph 아님) | 예산·재시도·검증·거절을 명시적으로 제어. 안 쓴 프레임워크를 스택에 넣지 않는다 |
| 정형 | SQLite (읽기전용) → PostgreSQL | 임베디드로 시작, 서버는 config 한 줄 |
| 벡터 | Qdrant local mode | 서버 없이 동작. FastAPI는 workers=1 (local mode 단일 프로세스 락) |
| 그래프 | Ladybug (임베디드 openCypher) | Kùzu 상류 아카이브(2025-10) → 후속 포크. 서버 불필요 |
| 임베딩 | BGE-m3-ko | 한국어 dense 검색 |
| 어휘 검색 | bm25s + kiwipiepy | 한국어 형태소 토크나이저 필수 |
| 리랭커 | bge-reranker-v2-m3 | 한국어 cross-encoder |
| LLM | vLLM gpt-oss-20b (OpenAI 호환) | 로컬 서빙. 코드가 로컬/API 무관 |
| 배포 | HuggingFace Spaces (Docker SDK, 원격 빌드) | 로컬에 컨테이너 런타임 없이 공개 URL 확보 |
| 계측 | JSONL + pandas | append-only, git diff 가능. 8·400문항 규모에 DB 불필요 |

---

## 13. 이게 일반 RAG와 다른 점 (면접 대비)

"그래서 일반 RAG랑 뭐가 다른데?"에 대한 답 세 가지:

1. **경로 선택** — 일반 RAG는 모든 질의를 벡터 검색에 태운다. 여기선 수치 질의는 SQL, 관계 질의는 그래프로 라우팅한다. 그리고 그 선택이 정확도·비용·오답률에 미치는 영향을 `always_all`(전부 호출)과 대조해 **숫자로 보인다**.
2. **거절의 정량화** — "모른다"를 출력하는 RAG는 많지만, 거절 품질을 risk-coverage 곡선(AURC)으로 측정하는 경우는 드물다. coverage를 낮추면 오답률이 얼마나 떨어지는가를 곡선으로 제시한다.
3. **도구 실행 성공 ≠ 근거 충분** — 검색이 결과를 반환했다고 답하지 않는다. 근거 게이트를 통과해야 ANSWER고, 못 하면 어떤 사유로 거절했는지 코드로 남는다. 운영 관점에서 이 구분이 환각 방지의 핵심이다.

이력서 한 줄:
> 이질적 공공데이터(정형 통계·법령 문서·지식 그래프)를 질의별로 선택·조합하고 근거 부족 시 답변을 거절하는 다중 소스 오케스트레이션 플랫폼을 설계·구현. 도구 호출·인용·지연·토큰을 JSONL로 계측하고 AURC·selective accuracy로 평가.

---

## 14. 한계 (정직하게)

- 계획형 오케스트레이션이지 자율 multi-agent가 아니다 (ReAct는 베이스라인 비교로만).
- 그래프는 위임 엣지 위주이고 조문 간 내부 인용은 오탐이 커 제외했다.
- 답변 채점은 SQL 경로만 EM/F1이 가능하고, 서술·복합 답변은 단일 채점자 루브릭 수동 채점이다 (표에 명기).
- 도메인 하나(감염병)로 도메인 독립성을 "주장"하지만, 두 번째 도메인 실증은 v0.2다.
- LLM·답변 채점기가 붙기 전 단계의 지표는 라우팅 지표의 근사임을 표에 명시한다.
