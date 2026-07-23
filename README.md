# Route–Compose–Abstain (RCA)

> 정형 DB · 비정형 문서 · 지식 그래프에 흩어진 지식을 **선택적으로 라우팅·조합**하고,
> 근거가 부족하면 **답변을 거부**하는 다중 소스 질의응답 플랫폼.

**상태: 구축 중 (Step 0 — 설계 확정)**

## 왜

일반적인 RAG는 모든 질의를 같은 검색 경로에 태운다. 수치·집계 질의, 관계형 질의, 데이터에 답이 없는 질의에서
이 방식은 불필요한 연산과 그럴듯한 오답을 만든다. RCA는 질의별로 필요한 정보원만 호출하고,
경로별로 근거를 검증한 뒤 `ANSWER` / `CLARIFY` / `ABSTAIN` 중 하나를 낸다.

## 구성

| 경로 | 백엔드 | 담당 질의 |
|---|---|---|
| SQL | SQLite / PostgreSQL | 수치·집계·필터 |
| Document | Qdrant + BM25 + cross-encoder | 정의·서술 |
| Graph | Kùzu / Neo4j (openCypher) | 관계·다단계 |
| Composite | 위 조합 | 복합 질의 |
| Abstain | — | 답할 근거가 없는 질의 |

## 문서

| 문서 | 내용 |
|---|---|
| [ARCHITECTURE_v1.md](ARCHITECTURE_v1.md) | 시스템 설계 — 컴포넌트·MCP 도구 계약·trace 스키마·보안 경계 |
| [docs/RESEARCH_PLAN_v2.md](docs/RESEARCH_PLAN_v2.md) | 연구 설계 — 평가셋·라벨링 프로토콜·베이스라인·지표·일정 |
| [eval/RESULTS.md](eval/RESULTS.md) | 측정 결과 (현재는 배선 점검 수치) |

## 실행

지금 동작하는 것은 **stub 도구 기반 배선 점검**이다. 추가 의존성 없이 (pydantic만) 돌아간다.

```bash
PYTHONPATH=src python -m rca.demo    # 8문항 × 2조건 실행 → eval/runs/*.jsonl
python eval/analyze.py               # trace → 지표 표
```

```
| system   | route F1 | coverage | sel.acc | abst.P | abst.R | AURC↓ | calls/q | tok/q |
|----------|----------|----------|---------|--------|--------|-------|---------|-------|
| doc_only |    0.080 |    1.000 |   0.250 |      — |  0.000 | 0.594 |    1.00 |   570 |
| proposed |    0.800 |    0.625 |   1.000 |  1.000 |  1.000 | 0.118 |    1.00 |   348 |
```

수치 자체는 stub이라 의미 없다. 확인하려는 것은 **모든 지표가 trace 로그만으로 유도되는가**이다.
실제 도구가 붙어도 `rca/demo.py`의 stub 함수만 교체되고 오케스트레이터·정책·계측은 그대로다.

향후: `python -m rca.run --system proposed --split test`.
외부 서버 없이 임베디드 스토어로 동작한다. 서버 모드는 `configs/base.yaml`의 `backend:` 로 전환.

## 로드맵

- [x] Step 0 — 아키텍처 확정 · trace 스키마 · 주지표(AURC) 확정 · 배선 점검
- [ ] Step 0.5 — route 라벨링 프로토콜(κ) · contrastive unanswerable 규칙 확정
- [ ] Step 1 — 30문항 end-to-end 관통 (실제 SQL · Document · Graph · Verifier)
- [ ] Step 2 — 200문항 확장, 데모, 예비 결과 공개
- [ ] Step 3 — 베이스라인 8종 · ablation 5종 · arXiv v1

## 고지

본 저장소의 출력은 보험·금융·법률 **자문이 아니다**. 공개 데이터만 사용하며 개인 계약 정보는 다루지 않는다.

## 라이선스

MIT (코드) / 데이터는 각 출처 라이선스를 따른다.
