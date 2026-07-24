# Route–Compose–Abstain (RCA)

> 정형 통계 · 법령 문서 · 지식 그래프에 흩어진 지식을 질의별로 **선택적으로 라우팅·조합**하고,
> 근거가 부족하면 **답변을 거부**하는 다중 소스 질의응답 플랫폼.

도메인은 **감염병**(질병관리청 발생통계 + 감염병예방법 + 급수·신고체계 그래프). 아키텍처는 도메인 독립이다.

## 왜

일반적인 RAG는 모든 질의를 같은 벡터 검색에 태운다. 수치·집계 질의, 관계형 질의, 데이터에 답이 없는 질의에서
이 방식은 불필요한 연산과 그럴듯한 오답을 만든다. RCA는 질의별로 필요한 정보원만 호출하고,
경로별로 근거를 검증한 뒤 `ANSWER` / `CLARIFY` / `ABSTAIN` 중 하나를 낸다.

## 구성

| 경로 | 백엔드 | 담당 질의 | 예시 |
|---|---|---|---|
| SQL | SQLite (읽기전용) | 수치·집계·추이 | "2023년 시도별 수두 발생 건수" |
| Document | Qdrant + BM25(kiwi) + reranker | 정의·서술 | "제2급감염병 신고 기한은?" |
| Graph | Ladybug (openCypher) | 관계·위계 | "제8조의2가 시행령에 위임한 규정" |
| Composite | 위 조합 | 복합 | "수두 신고기준 설명 + 최근 3년 추이" |
| Abstain | — | 근거 없음 | "김철수 확진 여부"(PII) · "2030년 전망"(범위밖) |

## 데이터 (전부 공개)

| 스토어 | 규모 | 출처 |
|---|---|---|
| 정형 | 14,226행 (68질병 × 2015~2026 × 18지역) | [질병관리청 감염병포털](https://dportal.kdca.go.kr) |
| 문서 | 202청크 / 감염병예방법·시행령·시행규칙 | [법제처 국가법령정보](https://www.law.go.kr) |
| 그래프 | 213노드 · 125위임엣지 | 위 법령에서 규칙 추출(LLM 미사용) |
| 조인 허브 | 법 88종 ↔ 통계 68종, 99% 매칭 | `data/disease_hub.csv` |

## 실행

```bash
pip install -r requirements.txt

# 1) 데이터 구성 (공개 API에서 수집 → 스토어 빌드)
python scripts/fetch_kdca.py            # 발생통계 수집 (순차, ~20분)
python scripts/fetch_law.py             # 법령 3종
python scripts/build_disease_hub.py     # 조인 허브
python scripts/build_sqlite.py          # 정형 스토어
python scripts/build_document_chunks.py # 문서 청킹
python scripts/build_graph.py           # 지식 그래프

# 2) 데모 서버 (LLM은 OpenAI 호환 엔드포인트, 기본 localhost:30070)
PYTHONPATH=src uvicorn rca.api:app --port 8000 --workers 1
#   → http://localhost:8000  (단일 페이지 UI)

# 3) 평가 (60문항 × 거절 ON/OFF)
PYTHONPATH=src python eval/run_eval.py
python eval/analyze_eval.py
```

LLM 백본은 `RCA_LLM_BASE`·`RCA_LLM_MODEL` 환경변수로 로컬↔클라우드 전환. 검색·임베딩은 CPU 고정.

## 평가 — 거절이 오답을 막는가

같은 백본·같은 검색으로 거절 정책만 켜고 끈 비교(60문항). 상세: [eval/RESULTS.md](eval/RESULTS.md).

| 조건 | coverage | selective acc | **오답률** | 거절 R |
|---|---|---|---|---|
| **거절 ON** (proposed) | 0.600 | **0.778** | **0.222** | 0.625 |
| 거절 OFF (no_abstain) | 0.800 | 0.688 | 0.312 | 0.312 |

거절이 오답률을 **0.312 → 0.222(약 29%↓)** 낮춘다. 답변불가 20문항(범위 밖·PII·근거 없음)을 답 대신 거절로 전환한 결과.
대가는 coverage 하락 — 오거절도 있다(거절 정밀도 0.417, 주로 검색 리콜 한계). 지표는 라우팅·거절 중심이며 답변 내용 채점은 별도([한계](eval/RESULTS.md#정직한-한계)).

## 문서

| 문서 | 내용 |
|---|---|
| [docs/TECHNICAL_OVERVIEW.md](docs/TECHNICAL_OVERVIEW.md) | 기술 개요 — 흐름·컴포넌트·스택·"일반 RAG와 다른 점" |
| [ARCHITECTURE_v1.md](ARCHITECTURE_v1.md) | 시스템 설계 — trace 스키마·보안 경계·ADR |
| [docs/RESEARCH_PLAN_v2.md](docs/RESEARCH_PLAN_v2.md) | 연구 설계 — 평가셋·라벨링·지표 (논문 확장용) |
| [eval/RESULTS.md](eval/RESULTS.md) | 측정 결과 |

## 설계 요점

- **계측 우선** — 도구 호출 1회 = JSONL 1줄. 모든 지표가 이 로그에서만 나온다.
- **도구 실행 성공 ≠ 근거 충분** — 검색이 결과를 반환해도 근거 게이트를 통과해야 답한다.
- **그래프를 LLM으로 만들지 않는다** — 시행령의 "법 제N조" 위임을 정규식으로 추출(재현성).
- **보안 축소 불가** — SQL은 읽기전용·SELECT 전용·파라미터 바인딩·화이트리스트·PII 차단.

## 고지

본 저장소의 출력은 의료·법률 **자문이 아니며** 공개 통계·법령에 근거한 참고용이다. 개인정보는 다루지 않는다.
법령 원문은 재배포하지 않고 `scripts/fetch_law.py`로 재현한다(출처: 법제처, 공공누리).

## 라이선스

MIT (코드) / 데이터는 각 출처 라이선스를 따른다.
