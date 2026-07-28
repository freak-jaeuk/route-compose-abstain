"""Coverage-matched 비교 — 거절 ON/OFF를 같은 커버리지에서 견준다.

리뷰어가 즉시 묻는 것: risk@coverage 0.60(ON)과 risk@coverage 1.00(OFF)을 비교하는 것은
통제된 비교가 아니다. 답변가능률이 73%인 풀에서 40%를 버리면 **어떤 약한 신호로도**
risk가 내려간다. 거절 정책이 무작위 기각보다 나은지 확인하려면 커버리지를 맞춰야 한다.

세 가지를 같은 커버리지에서 비교한다:
  1. abstention ON   — 실제 거절 정책
  2. abstention OFF를 confidence로 잘라 같은 커버리지로 (OFF의 신뢰도 순위가 얼마나 좋은가)
  3. 무작위 기각      — 같은 커버리지, 순서 무작위 (하한선)

용례:  python eval/coverage_matched.py [--B 2000]
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
OUT = ROOT / "eval/coverage_matched.json"


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


def risk_of(rs, gold) -> float:
    """답한 집합의 answerability risk."""
    return (sum(not gold[r["qid"]]["answerable"] for r in rs) / len(rs)) if rs else 0.0


def top_k_by_conf(runs: dict, k: int) -> list:
    """신뢰도 상위 k개를 '답한 것'으로 본다. 동점은 qid 정렬로 결정론적."""
    ordered = sorted(runs.values(),
                     key=lambda r: (-(r.get("answer_confidence") or 0.0), r["qid"]))
    return ordered[:k]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--B", type=int, default=2000, help="무작위 기각 반복")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    random.seed(args.seed)

    gold, by = load()
    for s in ("proposed", "no_abstain"):
        if s not in by:
            sys.exit(f"{s} trace 없음")

    on, off = by["proposed"], by["no_abstain"]
    qids = sorted(set(on) & set(off))
    n = len(qids)

    on_answered = [r for q, r in on.items() if q in qids and r.get("verdict") == "ANSWER"]
    k = len(on_answered)                      # 맞출 커버리지
    cov = k / n
    r_on = risk_of(on_answered, gold)

    # OFF를 confidence 상위 k개로 자른다
    off_topk = top_k_by_conf({q: off[q] for q in qids}, k)
    r_off_matched = risk_of(off_topk, gold)
    r_off_full = risk_of([off[q] for q in qids], gold)

    # 무작위 기각 하한선
    pool = [off[q] for q in qids]
    rand = []
    for _ in range(args.B):
        rand.append(risk_of(random.sample(pool, k), gold))
    rand.sort()
    r_rand = sum(rand) / len(rand)
    lo, hi = rand[int(0.025 * len(rand))], rand[int(0.975 * len(rand))]

    payload = {
        "n": n, "matched_coverage": cov, "k": k,
        "risk_abstention_on": r_on,
        "risk_off_confidence_topk": r_off_matched,
        "risk_off_full_coverage": r_off_full,
        "risk_random_rejection": {"mean": r_rand, "ci95": [lo, hi]},
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"커버리지 {cov:.3f} ({k}/{n})에서 비교\n")
    print("| 답할 것을 고르는 방법 | risk |")
    print("|---|---|")
    print(f"| 거절 정책 (abstention ON) | **{r_on:.3f}** |")
    print(f"| OFF의 신뢰도 상위 {k}개 | {r_off_matched:.3f} |")
    print(f"| 무작위 기각 (평균, B={args.B}) | {r_rand:.3f} [{lo:.3f}, {hi:.3f}] |")
    print(f"\n참고: OFF 전체 커버리지(1.000)에서의 risk = {r_off_full:.3f}")
    print("\n해석: 거절 정책이 무작위 기각 CI 아래에 있어야 '정책이 신호를 쓴다'고 말할 수 있다.")
    print(f"→ {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
