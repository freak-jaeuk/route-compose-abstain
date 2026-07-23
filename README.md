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

전체 설계: **[ARCHITECTURE_v1.md](ARCHITECTURE_v1.md)**

## 실행 (예정)

```bash
pip install -r requirements.txt
python -m rca.run --system proposed --split test
python eval/analyze.py eval/runs/           # trace → 지표 표·그림
```

외부 서버 없이 임베디드 스토어로 동작한다. 서버 모드는 `configs/base.yaml`의 `backend:` 로 전환.

## 로드맵

- [ ] Step 0 — 라벨링 프로토콜 · trace 스키마 · 주지표 확정
- [ ] Step 1 — 30문항 end-to-end 관통 (SQL · Document · Graph · Verifier)
- [ ] Step 2 — 200문항 확장, 데모, 예비 결과 공개
- [ ] Step 3 — 베이스라인 8종 · ablation 5종 · arXiv v1

## 고지

본 저장소의 출력은 보험·금융·법률 **자문이 아니다**. 공개 데이터만 사용하며 개인 계약 정보는 다루지 않는다.

## 라이선스

MIT (코드) / 데이터는 각 출처 라이선스를 따른다.
