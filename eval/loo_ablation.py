"""Leave-one-trigger-out ablation — 실측. counterfactual 추정을 대체한다.

`trigger_ablation.py`는 "거절된 질의가 그 경로로 답했다면"을 로그에서 가정했다.
여기서는 트리거를 실제로 끄고 60문항을 다시 돌린 결과를 읽는다. 재라우팅·폴백이
실제로 일어나므로, 추정이 낙관적이었는지 아닌지가 드러난다.

기준(proposed, 전 트리거 ON) 대비 각 LOO 조건의 coverage·risk 변화를 보고하고,
추정치와 실측치를 나란히 놓는다.

용례:  python eval/loo_ablation.py
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
RUNS = ROOT / "eval/runs"
EST = ROOT / "eval/trigger_ablation.json"
OUT = ROOT / "eval/loo_ablation.json"

TRIGGER_NAME = {
    "INSUFFICIENT_EVIDENCE": "evidence gate",
    "PRIVACY_RESTRICTED": "privacy screen",
    "OUT_OF_SCHEMA": "schema-range check",
    "GRAPH_PATH_NOT_FOUND": "graph path check",
}


def load():
    gold = {}
    for line in GOLD.read_text(encoding="utf-8").splitlines():
        if line.strip():
            g = json.loads(line)
            gold[g["qid"]] = g
    rows, _ = read_traces(RUNS)
    by = defaultdict(list)
    for r in rows:
        if r.get("tool") == "_run":
            by[r["system"]].append(r)
    return gold, by


def stats(runs, gold) -> dict:
    n = len(runs)
    answered = [r for r in runs if r.get("verdict") == "ANSWER"]
    risk = (sum(not gold[r["qid"]]["answerable"] for r in answered) / len(answered)
            if answered else 0.0)
    return {"n": n, "coverage": len(answered) / n, "risk": risk,
            "answered": len(answered)}


def main() -> None:
    gold, by = load()
    if "proposed" not in by:
        sys.exit("proposed trace 없음")
    base = stats(by["proposed"], gold)
    print(f"기준 proposed: coverage {base['coverage']:.3f}, risk {base['risk']:.3f} "
          f"(답한 것 {base['answered']}/{base['n']})\n")

    est = {}
    if EST.exists():
        payload = json.loads(EST.read_text(encoding="utf-8"))
        # 추정과 실측이 같은 시스템에서 나왔는지 검사한다. 이 표의 의미는 전적으로
        # "같은 시스템을 두 방법으로 재면 다르다"이므로, 추정이 다른 trace(예: 수정 전
        # 시스템)에서 왔다면 표는 두 시스템의 차이를 재는 것이 되어 주장이 무효가 된다.
        # 실제로 그 사고가 한 번 있었고 논문에 실렸다.
        src = payload.get("trace_dir")
        want = str(RUNS.relative_to(ROOT))
        if src != want:
            sys.exit(f"MISMATCH: 추정은 '{src}', 실측은 '{want}' 에서 나왔다.\n"
                     f"  같은 trace로 맞춰라:  python eval/trigger_ablation.py {want}")
        if payload.get("base_answered") != base["answered"]:
            sys.exit(f"MISMATCH: 추정 기준 answered={payload.get('base_answered')} "
                     f"≠ 실측 기준 answered={base['answered']} — trigger_ablation.py를 다시 돌려라")
        for t in payload["triggers"]:
            est[t["reason"]] = t

    print("| 트리거 제거 | coverage | risk | Δrisk 실측 | Δrisk 추정 | 추정 오차 |")
    print("|---|---|---|---|---|---|")
    results = []
    for reason, label in TRIGGER_NAME.items():
        key = f"loo_{reason.lower()}"
        if key not in by:
            continue
        s = stats(by[key], gold)
        d_real = s["risk"] - base["risk"]
        e = est.get(reason)
        d_est = e["risk_delta"] if e else None
        row = {"reason": reason, "trigger": label, **s,
               "delta_risk_measured": d_real, "delta_risk_estimated": d_est,
               "estimate_error": (d_est - d_real) if d_est is not None else None}
        results.append(row)
        est_s = f"{d_est:+.3f}" if d_est is not None else "—"
        err_s = f"{d_est - d_real:+.3f}" if d_est is not None else "—"
        print(f"| {label} | {s['coverage']:.3f} | {s['risk']:.3f} | "
              f"{d_real:+.3f} | {est_s} | {err_s} |")

    OUT.write_text(json.dumps({"base": base, "triggers": results},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nΔrisk > 0 = 트리거를 끄면 위험이 오른다(= 트리거가 도움이 됨).")
    print("'추정'은 trigger_ablation.py의 로그 기반 counterfactual, '실측'은 실제 재실행이다.")
    print(f"→ {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
