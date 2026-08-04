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


def tie_break_range(runs: dict, k: int, gold: dict) -> dict:
    """top-k 경계에 동점이 있으면 risk가 tie-break에 좌우된다 — 그 범위를 낸다.

    confidence가 몇 개 안 되는 상수라서 경계에 동점이 대량으로 쌓인다. 그러면
    "신뢰도 상위 k개"는 유일하게 정의되지 않고, 어느 것을 고르냐에 따라 risk가
    달라진다. 하나의 tie-break이 준 값만 보고하면 그 값이 임의 선택의 산물인지
    구조적 사실인지 구분할 수 없다. 최선/최악/무작위 평균을 모두 낸다.
    """
    rs = list(runs.values())
    conf = lambda r: r.get("answer_confidence") or 0.0
    boundary = sorted(rs, key=lambda r: -conf(r))[k - 1]
    b = conf(boundary)
    above = [r for r in rs if conf(r) > b]
    tied = [r for r in rs if conf(r) == b]
    need = k - len(above)                      # 동점에서 골라야 하는 수

    bad = lambda s: sum(not gold[r["qid"]]["answerable"] for r in s)
    u_above, u_tied = bad(above), bad(tied)
    # 최선: 답변가능한 동점을 우선 채운다. 최악: 답변불가를 우선 채운다.
    best = (u_above + max(0, need - (len(tied) - u_tied))) / k
    worst = (u_above + min(need, u_tied)) / k
    return {"boundary_confidence": b, "n_above": len(above), "n_tied": len(tied),
            "n_needed_from_ties": need, "unanswerable_among_ties": u_tied,
            "risk_best_case": best, "risk_worst_case": worst}


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
    off_sub = {q: off[q] for q in qids}
    off_topk = top_k_by_conf(off_sub, k)
    r_off_matched = risk_of(off_topk, gold)
    r_off_full = risk_of([off[q] for q in qids], gold)

    # 그 top-k가 tie-break에 얼마나 좌우되는가 — 단일 값만 보고하면 안 되는 이유
    tb = tie_break_range(off_sub, k, gold)
    tb_draws = []
    for _ in range(args.B):
        shuffled = sorted(off_sub.values(),
                          key=lambda r: (-(r.get("answer_confidence") or 0.0), random.random()))
        tb_draws.append(risk_of(shuffled[:k], gold))
    tb_draws.sort()
    tb["risk_random_tiebreak_mean"] = sum(tb_draws) / len(tb_draws)
    tb["risk_random_tiebreak_ci95"] = [tb_draws[int(0.025 * len(tb_draws))],
                                       tb_draws[int(0.975 * len(tb_draws))]]
    # 정책보다 나쁜 tie-break이 얼마나 흔한가 = 이 비교의 신뢰도
    tb["frac_not_better_than_policy"] = sum(d >= r_on for d in tb_draws) / len(tb_draws)

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
        "tie_break": tb,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    n_on_bad = sum(not gold[r["qid"]]["answerable"] for r in on_answered)
    n_tk_bad = sum(not gold[r["qid"]]["answerable"] for r in off_topk)
    print(f"커버리지 {cov:.3f} ({k}/{n})에서 비교\n")
    print("| 답할 것을 고르는 방법 | risk |")
    print("|---|---|")
    print(f"| 거절 정책 (abstention ON) | **{r_on:.3f}** ({n_on_bad}/{k}) |")
    print(f"| OFF의 신뢰도 상위 {k}개 | {r_off_matched:.3f} ({n_tk_bad}/{k}) |")
    print(f"| 무작위 기각 (평균, B={args.B}) | {r_rand:.3f} [{lo:.3f}, {hi:.3f}] |")
    print(f"\n참고: OFF 전체 커버리지(1.000)에서의 risk = {r_off_full:.3f}")

    print(f"\n## top-{k}의 tie-break 민감도 — 위 한 줄만 보고하면 안 되는 이유\n")
    print(f"  경계 confidence {tb['boundary_confidence']}: 초과 {tb['n_above']}건, "
          f"동점 {tb['n_tied']}건 중 {tb['n_needed_from_ties']}건을 임의로 골라야 한다")
    print(f"  동점 {tb['n_tied']}건 중 답변불가 {tb['unanswerable_among_ties']}건")
    print(f"  risk 범위 [{tb['risk_best_case']:.3f}, {tb['risk_worst_case']:.3f}], "
          f"무작위 평균 {tb['risk_random_tiebreak_mean']:.3f} "
          f"[{tb['risk_random_tiebreak_ci95'][0]:.3f}, {tb['risk_random_tiebreak_ci95'][1]:.3f}]")
    print(f"  ★ 정책({r_on:.3f})보다 낫지 않은 tie-break 비율: "
          f"{tb['frac_not_better_than_policy']:.1%}")
    if tb["risk_worst_case"] >= r_on:
        print("  ★ 최악의 tie-break은 정책보다 낫지 않다 — 단일 임계값 우위는 조건부다")
    print(f"\n→ {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
