"""거절 트리거별 기여도 — 로그 기반 counterfactual 분석.

"이 트리거가 없었다면 무슨 일이 벌어졌을까"를 trace에서 계산한다. 시스템을 다시
돌리지 않으므로 근사다(실제로 답을 냈다면 다른 경로를 탔을 수 있다). 그러나
거절 트리거가 각각 얼마나 위험을 줄이는지의 상대적 크기는 이 방식으로 충분히 보인다.

가정: 트리거를 끄면 해당 사유로 거절된 질의는 답을 낸다. 그 답은
  - gold가 답변불가면 → 오답 (근거가 없으므로)
  - gold가 답변가능이면 → 정답 (오거절이 회복됨)

출력: eval/trigger_ablation.json + 표 (논문 §5용)
용례:  python eval/trigger_ablation.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rca.trace import read_traces  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "eval/qa/gold.jsonl"
RUNS = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "eval/runs")
OUT = ROOT / "eval/trigger_ablation.json"

# 사유 코드 → 논문에서 부를 트리거 이름
TRIGGER = {
    "PRIVACY_RESTRICTED": "privacy screen",
    "OUT_OF_SCHEMA": "schema-range check",
    "INSUFFICIENT_EVIDENCE": "evidence gate",
    "GRAPH_PATH_NOT_FOUND": "graph path check",
    "LOW_ROUTER_CONFIDENCE": "router confidence",
    "SQL_EXECUTION_FAILURE": "SQL execution check",
    "SOURCE_CONFLICT": "source conflict check",
    "BUDGET_EXCEEDED": "budget guard",
}


def main() -> None:
    gold = {}
    for line in GOLD.read_text(encoding="utf-8").splitlines():
        if line.strip():
            g = json.loads(line)
            gold[g["qid"]] = g

    rows, _ = read_traces(RUNS)
    runs = [r for r in rows if r.get("tool") == "_run" and r.get("system") == "proposed"]
    if not runs:
        sys.exit("proposed trace 없음")
    n = len(runs)

    answered = [r for r in runs if r.get("verdict") == "ANSWER"]
    abstained = [r for r in runs if r.get("verdict") == "ABSTAIN"]

    def risk(ans_set):
        """답한 집합의 answerability risk = 답변불가에 답한 비율."""
        if not ans_set:
            return 0.0
        return sum(not gold[r["qid"]]["answerable"] for r in ans_set) / len(ans_set)

    base_cov, base_risk = len(answered) / n, risk(answered)

    by_reason = defaultdict(list)
    for r in abstained:
        if r.get("abstain_reason"):
            by_reason[r["abstain_reason"]].append(r)

    results = []
    for reason, rs in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        # 이 트리거를 끄면 rs가 답변 집합에 합류한다
        cf_answered = answered + rs
        harmful = sum(not gold[r["qid"]]["answerable"] for r in rs)   # 답하면 오답이 되는 것
        recovered = len(rs) - harmful                                  # 오거절이었던 것
        results.append({
            "reason": reason,
            "trigger": TRIGGER.get(reason, reason),
            "n": len(rs),
            "would_be_wrong": harmful,
            "would_be_recovered": recovered,
            "cf_coverage": len(cf_answered) / n,
            "cf_risk": risk(cf_answered),
            "risk_delta": risk(cf_answered) - base_risk,
        })

    payload = {"n": n, "trace_dir": str(RUNS.relative_to(ROOT)),
               "base_answered": len(answered),
               "base_wrong": sum(not gold[r["qid"]]["answerable"] for r in answered),
               "base_coverage": base_cov, "base_risk": base_risk, "triggers": results}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    # 어느 trace에서 나온 추정인지 항상 찍는다. 이 값을 실측(LOO 재실행)과 나란히 놓을 때
    # 두 열이 같은 시스템에서 나왔는지가 비교의 전제인데, 그게 눈에 보이지 않으면
    # 수정 전 trace의 추정치와 수정 후의 실측치를 대조하는 사고가 조용히 일어난다.
    print(f"trace: {payload['trace_dir']}")
    print(f"기준(전 트리거 ON): coverage {base_cov:.3f} ({len(answered)}/{n}), "
          f"risk {base_risk:.3f} ({payload['base_wrong']}/{len(answered)})\n")
    print("| 트리거 제거 | n_T | u_T(오답화) | 오거절회복 | coverage | risk | Δrisk |")
    print("|---|---|---|---|---|---|---|")
    for t in results:
        print(f"| {t['trigger']} | {t['n']} | {t['would_be_wrong']} | {t['would_be_recovered']} | "
              f"{t['cf_coverage']:.3f} | {t['cf_risk']:.3f} | {t['risk_delta']:+.3f} |")
    print(f"\n추정식: risk_T = ({payload['base_wrong']} + u_T) / ({len(answered)} + n_T), "
          f"Δ = risk_T − {base_risk:.4f}")
    print(f"→ {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
