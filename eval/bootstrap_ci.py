"""부트스트랩 신뢰구간 — n=60에서 조건 간 차이가 통계적으로 말할 수 있는 크기인지.

거절 ON/OFF의 risk 차이는 문항 몇 개 수준이다. 점추정만 보고하면 리뷰어가
"n=60에서 이 차이가 의미 있나"를 즉시 묻는다. 문항 단위 부트스트랩으로
각 지표의 95% CI와, 두 조건의 **차이**에 대한 CI를 함께 낸다
(차이 CI가 0을 포함하는지가 실제로 답해야 할 질문이다).

두 조건이 같은 60문항을 공유하므로 문항을 쌍으로 재표집한다(paired bootstrap).

용례:  python eval/bootstrap_ci.py [--B 10000]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rca.trace import read_traces  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "eval/qa/gold.jsonl"
RUNS = ROOT / "eval/runs"
OUT = ROOT / "eval/bootstrap_ci.json"


def load():
    gold = {}
    for line in GOLD.read_text(encoding="utf-8").splitlines():
        if line.strip():
            g = json.loads(line)
            gold[g["qid"]] = g
    rows, _ = read_traces(RUNS)
    by = defaultdict(dict)
    for r in rows:
        if r.get("tool") == "_run":
            by[r["system"]][r["qid"]] = r
    return gold, by


def metrics(runs_by_qid, qids, gold) -> dict:
    """한 재표집 샘플에서의 지표. runs_by_qid: {qid: _run row}."""
    rs = [runs_by_qid[q] for q in qids if q in runs_by_qid]
    if not rs:
        return {}
    answered = [r for r in rs if r.get("verdict") == "ANSWER"]
    n = len(rs)

    # answerability risk: 답한 것 중 답변불가 비율
    risk = (sum(not gold[r["qid"]]["answerable"] for r in answered) / len(answered)
            if answered else 0.0)
    cov = len(answered) / n

    tp = sum(r.get("verdict") == "ABSTAIN" and not gold[r["qid"]]["answerable"] for r in rs)
    fp = sum(r.get("verdict") == "ABSTAIN" and gold[r["qid"]]["answerable"] for r in rs)
    fn = sum(r.get("verdict") != "ABSTAIN" and not gold[r["qid"]]["answerable"] for r in rs)
    return {
        "risk": risk,
        "coverage": cov,
        "abst_precision": tp / (tp + fp) if tp + fp else 0.0,
        "abst_recall": tp / (tp + fn) if tp + fn else 0.0,
    }


def ci(vals, alpha=0.05):
    s = sorted(vals)
    lo = s[int(alpha / 2 * len(s))]
    hi = s[min(len(s) - 1, int((1 - alpha / 2) * len(s)))]
    return lo, hi


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--B", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    random.seed(args.seed)

    gold, by = load()
    if "proposed" not in by or "no_abstain" not in by:
        sys.exit("두 조건의 trace가 모두 필요하다")
    qids = sorted(set(by["proposed"]) & set(by["no_abstain"]))
    n = len(qids)
    print(f"공통 문항 {n}개, B={args.B} paired bootstrap\n")

    point = {s: metrics(by[s], qids, gold) for s in ("proposed", "no_abstain")}
    keys = ["risk", "coverage", "abst_precision", "abst_recall"]
    draws = {s: {k: [] for k in keys} for s in point}
    diffs = {k: [] for k in keys}

    for _ in range(args.B):
        samp = [random.choice(qids) for _ in range(n)]   # 문항을 쌍으로 재표집
        m_on = metrics(by["proposed"], samp, gold)
        m_off = metrics(by["no_abstain"], samp, gold)
        for k in keys:
            draws["proposed"][k].append(m_on[k])
            draws["no_abstain"][k].append(m_off[k])
            diffs[k].append(m_on[k] - m_off[k])

    payload = {"n": n, "B": args.B, "point": point, "ci": {}, "diff": {}}
    print("| 지표 | 거절 ON [95% CI] | 거절 OFF [95% CI] | 차이 [95% CI] |")
    print("|---|---|---|---|")
    for k in keys:
        lo_on, hi_on = ci(draws["proposed"][k])
        lo_off, hi_off = ci(draws["no_abstain"][k])
        d_lo, d_hi = ci(diffs[k])
        d = point["proposed"][k] - point["no_abstain"][k]
        sig = "" if d_lo <= 0 <= d_hi else " *"
        payload["ci"][k] = {"proposed": [lo_on, hi_on], "no_abstain": [lo_off, hi_off]}
        payload["diff"][k] = {"point": d, "ci": [d_lo, d_hi], "excludes_zero": not (d_lo <= 0 <= d_hi)}
        print(f"| {k} | {point['proposed'][k]:.3f} [{lo_on:.3f}, {hi_on:.3f}] "
              f"| {point['no_abstain'][k]:.3f} [{lo_off:.3f}, {hi_off:.3f}] "
              f"| {d:+.3f} [{d_lo:+.3f}, {d_hi:+.3f}]{sig} |")

    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n* = 차이의 95% CI가 0을 포함하지 않음")
    print(f"→ {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
