# 평가 결과

> **현재 실린 수치는 stub 도구 기반 배선 점검이며 연구 결과가 아니다.**
> 실제 검색·LLM이 붙기 전까지 이 문서의 표는 "지표가 trace 로그만으로 계산되는가"를 보이는 용도다.

재생성:

```bash
PYTHONPATH=src python -m rca.demo    # eval/runs/*.jsonl 생성
python eval/analyze.py               # 아래 표 출력
```

## 배선 점검 (stub, 8문항 × 2조건)

| system | route F1 | coverage | sel.acc | abst.P | abst.R | AURC↓ | calls/q | tok/q |
|---|---|---|---|---|---|---|---|---|
| doc_only | 0.080 | 1.000 | 0.250 | — | 0.000 | 0.594 | 1.00 | 570 |
| proposed | 0.800 | 0.625 | 1.000 | 1.000 | 1.000 | 0.118 | 1.00 | 348 |

거절 사유 분포 — `proposed`: `{OUT_OF_SCHEMA: 2, PRIVACY_RESTRICTED: 1}` · `doc_only`: 없음

지표 정의와 trace 필드 대응은 [ARCHITECTURE §8.1](../ARCHITECTURE_v1.md#81-trace--논문-지표-유도표).
`p95 ms`는 stub이 1ms 미만에 반환해 0으로 나온다.

### contrastive twin 동작 확인

| qid | 질문 | 라우팅 | 결과 |
|---|---|---|---|
| `qa_001` | …**지역별** 보험금 지급액 합계 | SQL | ANSWER |
| `qa_004` | …**읍면동별** 보험금 지급액 합계 | SQL | ABSTAIN(`OUT_OF_SCHEMA`) |

두 질문은 어절 하나 차이이며 라우팅도 동일하다. 스키마에 실제로 닿아야만 갈린다
— 표면 단서로 걸러지지 않는 답변불가 질의라는 설계 요건([연구계획서 v2 §4](../docs/RESEARCH_PLAN_v2.md#4-확정-필요--contrastive-unanswerable-규칙-o2))을 만족한다.

---

## 본 실험 (미실행)

Step 3에서 조건 8종 · ablation 5종 · 400문항으로 채운다. 주지표는 AURC.
