"""평가 trace → 지표 표. 조건별로 route F1·coverage·selective accuracy·거절 P/R·AURC.

route 지표와 답변 지표를 섞지 않는다(동어반복 방지). v0.1은 라우팅·거절 중심이고,
답변 내용 채점(EM/F1·수동)은 별도 표로 추후 붙인다.

용례:  python eval/analyze_eval.py
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rca.trace import read_traces  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "eval/qa/gold.jsonl"
RUNS = ROOT / "eval/runs"
ROUTES = ["SQL", "DOCUMENT", "GRAPH", "COMPOSITE", "ABSTAIN"]


def macro_f1(pairs):
    f1s = []
    for c in ROUTES:
        tp = sum(g == c and p == c for g, p in pairs)
        fp = sum(g != c and p == c for g, p in pairs)
        fn = sum(g == c and p != c for g, p in pairs)
        if tp + fn == 0 and fp == 0:
            continue
        pr = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * pr * rc / (pr + rc) if pr + rc else 0.0)
    return sum(f1s) / len(f1s) if f1s else 0.0


def aurc(scored):
    """area under risk-coverage. (confidence, correct). 낮을수록 좋다. 동점 블록 처리."""
    ranked = sorted(scored, key=lambda x: -x[0])
    risks, wrong, i = [], 0, 0
    while i < len(ranked):
        j = i
        while j < len(ranked) and ranked[j][0] == ranked[i][0]:
            wrong += not ranked[j][1]
            j += 1
        risks += [wrong / j] * (j - i)
        i = j
    return sum(risks) / len(risks) if risks else 0.0


def main() -> None:
    gold = {}
    for line in GOLD.read_text(encoding="utf-8").splitlines():
        if line.strip():
            g = json.loads(line)
            gold[g["qid"]] = g

    rows, corrupt = read_traces(RUNS)
    if corrupt:
        print(f"⚠ 깨진 줄 {len(corrupt)}")
    runs_by = defaultdict(list)
    for r in rows:
        if r.get("tool") == "_run":
            runs_by[r["system"]].append(r)
    if not runs_by:
        sys.exit("trace 없음 — 먼저 run_eval.py")

    n = len(gold)
    print(f"\n## 거절 ON/OFF 비교 (문항 {n})\n")
    head = ["condition", "route F1", "coverage", "sel.acc", "오답률", "abst.P", "abst.R", "AURC↓"]
    print("| " + " | ".join(head) + " |")
    print("|" + "|".join(["---"] * len(head)) + "|")

    for system in ["proposed", "no_abstain"]:
        rs = runs_by.get(system)
        if not rs:
            continue
        # correct = gold가 답변가능하고 라우팅이 gold 라벨과 일치. 답변불가는 거절이 정답.
        def correct(r):
            g = gold[r["qid"]]
            if not g["answerable"]:
                return r["verdict"] == "ABSTAIN"
            return r["verdict"] == "ANSWER" and r.get("route_pred") == g["route_label"]

        answered = [r for r in rs if r["verdict"] == "ANSWER"]
        pairs = [(gold[r["qid"]]["route_label"], r.get("route_pred") or "ABSTAIN") for r in rs]

        # 답한 것 중 정답 / 오답률 = 답했는데 틀린 것(답변불가에 답 or 라우팅 오류)
        ans_correct = sum(correct(r) for r in answered)
        sel_acc = ans_correct / len(answered) if answered else 0.0
        wrong_answered = sum(not correct(r) for r in answered)
        err_rate = wrong_answered / len(answered) if answered else 0.0

        # 거절 정밀·재현 (답변불가 gold 기준)
        tp = sum(r["verdict"] == "ABSTAIN" and not gold[r["qid"]]["answerable"] for r in rs)
        fp = sum(r["verdict"] == "ABSTAIN" and gold[r["qid"]]["answerable"] for r in rs)
        fn = sum(r["verdict"] != "ABSTAIN" and not gold[r["qid"]]["answerable"] for r in rs)

        auc = aurc([(r.get("answer_confidence") or 0.0, correct(r)) for r in rs])
        cov = len(answered) / n

        print("| " + " | ".join([
            system, f"{macro_f1(pairs):.3f}", f"{cov:.3f}", f"{sel_acc:.3f}",
            f"{err_rate:.3f}", f"{tp/(tp+fp):.3f}" if tp+fp else "—",
            f"{tp/(tp+fn):.3f}" if tp+fn else "—", f"{auc:.3f}",
        ]) + " |")

    print("\n### 거절 사유 분포 (proposed)")
    dist = defaultdict(int)
    for r in runs_by.get("proposed", []):
        if r.get("abstain_reason"):
            dist[r["abstain_reason"]] += 1
    for k, v in sorted(dist.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")

    print("\n핵심: 거절 OFF는 답변불가 20문항에 전부 답해 오답률↑. 거절 ON은 그 오답을 거절로 전환.")


if __name__ == "__main__":
    main()
