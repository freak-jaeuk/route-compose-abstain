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


def _aurc(pairs) -> float:
    """커버리지 가중 AURC. (confidence, would_be_correct) 목록."""
    ranked = sorted(pairs, key=lambda x: -x[0])
    per, wrong, i, n = [], 0, 0, len(ranked)
    while i < n:
        j = i
        while j < n and ranked[j][0] == ranked[i][0]:
            wrong += not ranked[j][1]
            j += 1
        per += [wrong / j] * (j - i)
        i = j
    return sum(per) / len(per) if per else 0.0


def _auroc(pairs) -> float:
    """AUROC — 신뢰도가 답변가능/불가를 얼마나 가르는가. 동점은 0.5로 센다.

    AURC와 달리 커버리지에 의존하지 않으므로, '신호 자체가 얼마나 약한가'를 직접 보여준다.
    """
    pos = [c for c, y in pairs if y]
    neg = [c for c, y in pairs if not y]
    if not pos or not neg:
        return 0.5
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


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
    # AURC: 신뢰도가 답변가능성을 얼마나 잘 정렬하는가(라우팅 무관)
    aurc = _aurc([(r.get("answer_confidence") or 0.0, bool(gold[r["qid"]]["answerable"]))
                  for r in rs])
    auroc = _auroc([(r.get("answer_confidence") or 0.0, bool(gold[r["qid"]]["answerable"]))
                    for r in rs])
    return {
        "aurc": aurc,
        "auroc": auroc,
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



def _assert_complete(by, gold):
    """조건별 문항 수가 gold와 다르면 즉시 실패한다.

    부분 실행된 trace로도 지표가 '그럴듯하게' 계산되는 것이 이 파이프라인의 실제 위험이다
    (리뷰어가 17/60 실행분으로 계산된 수치를 본 사례가 있다).
    """
    for system, rs in by.items():
        n = len(rs) if isinstance(rs, list) else len(rs)
        if n != len(gold):
            raise SystemExit(f"INCOMPLETE: {system} has {n}/{len(gold)} queries — rerun eval/run_eval.py")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--B", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    random.seed(args.seed)

    gold, by = load()
    _assert_complete(by, gold)
    if "proposed" not in by or "no_abstain" not in by:
        sys.exit("두 조건의 trace가 모두 필요하다")
    qids = sorted(set(by["proposed"]) & set(by["no_abstain"]))
    n = len(qids)
    print(f"공통 문항 {n}개, B={args.B} paired bootstrap\n")

    point = {s: metrics(by[s], qids, gold) for s in ("proposed", "no_abstain")}
    keys = ["risk", "coverage", "aurc", "auroc", "abst_precision", "abst_recall"]
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
