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
    _assert_complete(runs_by, gold)

    n = len(gold)
    n_una = sum(1 for g in gold.values() if not g["answerable"])
    print(f"\n## 거절 ON/OFF 비교 (문항 {n}, 답변불가 {n_una})")
    print("\n두 지표 family를 섞지 않는다. **answerability**는 '답할지 말지'의 품질만 보고"
          "\n(답변불가에 답하면 오답, 라우팅 무관), **task**는 라우팅 정확도까지 포함한다."
          "\n논문 헤드라인은 answerability 쪽이다.\n")
    head = ["condition", "coverage", "risk(ans)", "AURC(ans)↓", "abst.P", "abst.R",
            "route F1", "err(task)", "sel.acc(task)"]
    print("| " + " | ".join(head) + " |")
    print("|" + "|".join(["---"] * len(head)) + "|")

    for system in ["proposed", "no_abstain"]:
        rs = runs_by.get(system)
        if not rs:
            continue

        def answerable(r):
            """answerability 기준: 이 질의에 답했다면 맞았을까 = gold가 답변가능한가."""
            return bool(gold[r["qid"]]["answerable"])

        def task_correct(r):
            """task 기준: 라우팅까지 맞아야 정답. 답변불가는 거절이 정답."""
            g = gold[r["qid"]]
            if not g["answerable"]:
                return r["verdict"] == "ABSTAIN"
            return r["verdict"] == "ANSWER" and r.get("route_pred") == g["route_label"]

        answered = [r for r in rs if r["verdict"] == "ANSWER"]
        pairs = [(gold[r["qid"]]["route_label"], r.get("route_pred") or "ABSTAIN") for r in rs]
        cov = len(answered) / n

        # answerability family (논문 헤드라인)
        risk_ans = (sum(not answerable(r) for r in answered) / len(answered)) if answered else 0.0
        auc_ans = aurc([(r.get("answer_confidence") or 0.0, answerable(r)) for r in rs])

        # task family (라우팅 포함)
        sel_acc = (sum(task_correct(r) for r in answered) / len(answered)) if answered else 0.0
        err_task = 1.0 - sel_acc if answered else 0.0

        tp = sum(r["verdict"] == "ABSTAIN" and not gold[r["qid"]]["answerable"] for r in rs)
        fp = sum(r["verdict"] == "ABSTAIN" and gold[r["qid"]]["answerable"] for r in rs)
        fn = sum(r["verdict"] != "ABSTAIN" and not gold[r["qid"]]["answerable"] for r in rs)

        print("| " + " | ".join([
            system, f"{cov:.3f}", f"{risk_ans:.3f}", f"{auc_ans:.3f}",
            f"{tp/(tp+fp):.3f}" if tp+fp else "—", f"{tp/(tp+fn):.3f}" if tp+fn else "—",
            f"{macro_f1(pairs):.3f}", f"{err_task:.3f}", f"{sel_acc:.3f}",
        ]) + " |")

    print("\n### 거절 사유 분포 (proposed) — 정당한 거절 / 오거절")
    just, false_ = defaultdict(int), defaultdict(int)
    for r in runs_by.get("proposed", []):
        if not r.get("abstain_reason"):
            continue
        (false_ if gold[r["qid"]]["answerable"] else just)[r["abstain_reason"]] += 1
    for k in sorted(set(just) | set(false_), key=lambda x: -(just[x] + false_[x])):
        print(f"  {k}: {just[k]} / {false_[k]}")

    print(f"\n핵심: 거절 OFF는 답변불가 {n_una}문항에 전부 답한다. 거절 ON은 그중 일부를 거절로 전환하되,"
          "\n답변가능 질의도 일부 거절해 coverage를 잃는다. CI는 bootstrap_ci.py 참조.")


if __name__ == "__main__":
    main()
