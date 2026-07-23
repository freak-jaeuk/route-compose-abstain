"""trace JSONL → 지표 표. 입력은 로그뿐이며 시스템을 다시 실행하지 않는다.

ARCHITECTURE_v1.md §8.1 유도표의 실행 가능한 형태다.
새 지표가 필요하면 실험이 아니라 이 파일을 고친다.

실행:  python eval/analyze.py [runs_dir]
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rca.trace import read_traces  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ["SQL", "DOCUMENT", "GRAPH", "COMPOSITE", "ABSTAIN"]


def macro_f1(pairs: list[tuple[str, str]]) -> float:
    """pairs = [(gold, pred)]. 등장한 클래스만 평균낸다."""
    f1s = []
    for c in ROUTES:
        tp = sum(g == c and p == c for g, p in pairs)
        fp = sum(g != c and p == c for g, p in pairs)
        fn = sum(g == c and p != c for g, p in pairs)
        if tp + fn == 0:
            continue
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn)
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return sum(f1s) / len(f1s) if f1s else 0.0


def aurc(scored: list[tuple[float, bool]]) -> float:
    """area under risk–coverage curve. (confidence, would_be_correct) 목록.

    신뢰도 내림차순으로 커버리지를 넓히며 위험(오답률)을 적분한다. 낮을수록 좋다.
    verdict 만으로는 점 하나뿐이라 계산할 수 없다 — 연속 점수가 필요한 이유.
    """
    ranked = sorted(scored, key=lambda x: -x[0])
    wrong = 0
    risks = []
    for k, (_, ok) in enumerate(ranked, 1):
        wrong += not ok
        risks.append(wrong / k)
    return sum(risks) / len(risks) if risks else 0.0


def pctl(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def main(runs_dir: str = "eval/runs") -> None:
    gold = {}
    for line in (ROOT / "eval/qa/demo_gold.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            g = json.loads(line)
            gold[g["qid"]] = g

    rows, corrupt = read_traces(ROOT / runs_dir)
    if corrupt:
        print(f"⚠ 깨진 줄 {len(corrupt)}개 격리: {corrupt[:3]}")
    if not rows:
        sys.exit(f"trace 없음: {runs_dir} — 먼저 `PYTHONPATH=src python -m rca.demo`")

    by_system: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_system[r["system"]].append(r)

    print(f"\n문항 {len(gold)}개 · 조건 {len(by_system)}개 · trace {len(rows)}줄\n")
    head = ["system", "route F1", "coverage", "sel.acc", "abst.P", "abst.R", "AURC↓",
            "calls/q", "tok/q", "p95 ms"]
    print("| " + " | ".join(head) + " |")
    print("|" + "|".join(["---"] * len(head)) + "|")

    for system, rs in sorted(by_system.items()):
        runs = [r for r in rs if r["tool"] == "_run"]
        calls = [r for r in rs if r["tool"] != "_run"]
        n = len(runs)

        answered = [r for r in runs if r["verdict"] == "ANSWER"]
        # 스모크 채점 규약: gold 가 답변가능하고 라우팅이 맞아야 정답. 실제 실험에서는 EM/F1 로 대체된다.
        correct = lambda r: gold[r["qid"]]["answerable"] and r.get("route_pred") == gold[r["qid"]]["route_label"]  # noqa: E731

        pairs = [(gold[r["qid"]]["route_label"], r.get("route_pred") or "ABSTAIN") for r in runs]
        tp = sum(r["verdict"] == "ABSTAIN" and not gold[r["qid"]]["answerable"] for r in runs)
        fp = sum(r["verdict"] == "ABSTAIN" and gold[r["qid"]]["answerable"] for r in runs)
        fn = sum(r["verdict"] != "ABSTAIN" and not gold[r["qid"]]["answerable"] for r in runs)

        per_q_calls = defaultdict(int)
        per_q_tok = defaultdict(int)
        for c in calls:
            per_q_calls[c["qid"]] += 1
            per_q_tok[c["qid"]] += c.get("tokens_in", 0) + c.get("tokens_out", 0)

        print("| " + " | ".join([
            system,
            f"{macro_f1(pairs):.3f}",
            f"{len(answered) / n:.3f}",
            f"{sum(map(correct, answered)) / len(answered):.3f}" if answered else "—",
            f"{tp / (tp + fp):.3f}" if tp + fp else "—",
            f"{tp / (tp + fn):.3f}" if tp + fn else "—",
            f"{aurc([(r.get('answer_confidence') or 0.0, correct(r)) for r in runs]):.3f}",
            f"{sum(per_q_calls.values()) / n:.2f}",
            f"{sum(per_q_tok.values()) / n:.0f}",
            f"{pctl([r['elapsed_ms'] for r in runs], 0.95):.0f}",
        ]) + " |")

    print("\n거절 사유 분포")
    for system, rs in sorted(by_system.items()):
        dist = defaultdict(int)
        for r in rs:
            if r["tool"] == "_run" and r.get("abstain_reason"):
                dist[r["abstain_reason"]] += 1
        print(f"  {system:9s} {dict(dist) or '없음'}")

    print("\n위 수치는 stub 도구 기반 배선 점검이며 연구 결과가 아니다.")


if __name__ == "__main__":
    main(*sys.argv[1:2])
