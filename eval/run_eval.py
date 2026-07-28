"""평가 실행 — 60문항 × 2조건(거절 ON/OFF). trace를 남기고 지표는 analyze로.

v0.1의 헤드라인 산출물: "거절이 오답률을 낮추는가"를 같은 백본·같은 검색으로 비교.
- proposed   : abstention ON  (근거 부족·범위 밖·PII → 거절)
- no_abstain : abstention OFF (무조건 답을 낸다 = 일반 RAG 대응물)

답변 텍스트 생성(느린 LLM)은 끈다(generate=False). route·verdict·거절 지표가 목적.
라우팅만 LLM(빠름), 나머지는 실제 SQL·문서·그래프 검색.

용례:  PYTHONPATH=src python eval/run_eval.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rca.orchestrator import Agents, run_query  # noqa: E402
from rca.trace import Tracer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "eval/qa/gold.jsonl"
RUNS = ROOT / "eval/runs"

from rca.orchestrator import ALL_TRIGGERS  # noqa: E402

# 주 비교 2조건 + leave-one-trigger-out 4조건.
# LOO는 counterfactual 추정을 실측으로 대체한다: 트리거를 실제로 끄고 다시 돌리면
# 거절되던 질의가 어느 경로를 타는지, 정말 답이 나오는지가 로그에 남는다.
LOO_TRIGGERS = ["INSUFFICIENT_EVIDENCE", "PRIVACY_RESTRICTED",
                "OUT_OF_SCHEMA", "GRAPH_PATH_NOT_FOUND"]

CONDITIONS = [
    {"name": "proposed", "abstain_on": ALL_TRIGGERS},
    {"name": "no_abstain", "abstain_on": frozenset()},
] + [
    {"name": f"loo_{t.lower()}", "abstain_on": ALL_TRIGGERS - {t}}
    for t in LOO_TRIGGERS
]


def main() -> None:
    import json
    gold = [json.loads(l) for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip()]
    agents = Agents()   # 모델·DB 1회 로드
    print(f"문항 {len(gold)} × 조건 {len(CONDITIONS)}")

    for cfg in CONDITIONS:
        out = RUNS / f"{cfg['name']}.jsonl"
        # 이어가기: 이미 완료된 qid는 건너뛴다(공유 메모리 환경에서 OOM으로 죽어도 재개 가능).
        done = set()
        if out.exists():
            done = {json.loads(l)["qid"] for l in out.read_text(encoding="utf-8").splitlines()
                    if l.strip() and '"_run"' in l}
        tr = Tracer(out, run_id="eval", system=cfg["name"], backbone="gpt-oss-20b",
                    temperature=0.0, seed=0)
        t0 = time.perf_counter()
        for g in gold:
            if g["qid"] in done:
                continue
            with tr.query(g["qid"]) as summ:
                st = run_query(g["question"], g["qid"], tr, agents,
                               system=cfg["name"], abstain_on=cfg["abstain_on"],
                               use_llm=False, generate=False)
                summ.update(verdict=st.verdict, answer_confidence=st.answer_confidence,
                            abstain_reason=st.abstain_reason, route_pred=st.route_pred,
                            route_conf=st.route_conf, cited=st.cited)
        print(f"  {cfg['name']:11s} {len(gold)}문항 {time.perf_counter()-t0:.0f}s → {out.relative_to(ROOT)}")

    print("\n지표: python eval/analyze_eval.py")


if __name__ == "__main__":
    main()
