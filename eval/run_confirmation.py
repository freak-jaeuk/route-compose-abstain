"""확인셋을 두 조건(전체 정책 / abstention OFF)으로만 돌린다.

트리거 격자는 감사셋에서만 돌린다. 확인셋의 목적은 감사셋이 안 건드린 슬롯에서
게이트 거동이 재현되는가이지 귀속을 다시 하는 게 아니다.
"""
import json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from rca.orchestrator import ALL_TRIGGERS, Agents, run_query  # noqa: E402
from rca.trace import Tracer  # noqa: E402

QA = ROOT / "eval/qa/confirmation.jsonl"
RUNS = ROOT / "eval/runs_confirmation"
RUNS.mkdir(exist_ok=True)
rows = [json.loads(l) for l in open(QA)]
agents = Agents()
for name, on in [("proposed", frozenset(ALL_TRIGGERS)), ("no_abstain", frozenset())]:
    out = RUNS / f"{name}.jsonl"
    done = set()
    if out.exists():
        done = {json.loads(l)["qid"] for l in open(out) if '"_run"' in l}
    if len(done) >= len(rows):
        print(f"  {name}: 이미 완료"); continue
    tr = Tracer(out, run_id="confirm", system=name, backbone="gpt-oss-20b", temperature=0.0, seed=0)
    t0 = time.perf_counter()
    for g in rows:
        if g["qid"] in done: continue
        with tr.query(g["qid"]) as summ:
            st = run_query(g["question"], g["qid"], tr, agents, system=name,
                           abstain_on=on, use_llm=False, generate=False)
            summ.update(verdict=st.verdict, answer_confidence=st.answer_confidence,
                        abstain_reason=st.abstain_reason, route_pred=st.route_pred,
                        route_conf=st.route_conf, cited=st.cited)
    print(f"  {name}: {len(rows)}문항 {time.perf_counter()-t0:.0f}s")
print("완료 ->", RUNS.relative_to(ROOT))
