"""거절 상수 민감도 — 결과 #1(ordering이 나빠진다)이 손으로 정한 숫자의 산물인가.

이 논문의 첫 결과는 "거절을 켜면 동작점은 좋아지지만 순위(AURC·AUROC)는 나빠진다"이다.
그런데 거절된 질의의 confidence는 모델이 낸 값이 아니라 사유별 상수다(privacy 0.02,
execution gate 0.10, router는 라우터 신뢰도). 즉 60개 중 26개의 점수를 사람이 정했다.
그러면 "순위가 나빠졌다"는 것이 시스템의 성질인지, 아니면 그 세 숫자를 그렇게 정해서
생긴 것인지 구분되지 않는다 — 리뷰어가 가장 먼저 찌를 지점이다.

여기서 그 세 상수를 흔들어 본다:
  (1) 세 값을 세 그룹에 재배치하는 6가지 순열 전부
  (2) 각 그룹에 [0, 0.5) 균등난수를 주는 무작위 추출 B회
그리고 각 경우에 결론(AURC 악화 & AUROC 악화)이 유지되는 비율을 센다.

답변 생성이 꺼져 있고 라우터가 규칙 기반이라 거절 여부 자체는 상수와 무관하다.
따라서 시스템 재실행 없이 로그만 다시 채점하면 된다.

용례:  python eval/confidence_sensitivity.py [--B 1000]
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rca.trace import read_traces  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
from bootstrap_ci import _aurc, _auroc  # noqa: E402  같은 정의를 두 번 쓰지 않는다

GOLD = ROOT / "eval/qa/gold.jsonl"
RUNS = ROOT / "eval/runs"
OUT = ROOT / "eval/confidence_sensitivity.json"

# 손으로 정한 거절 상수가 붙는 세 그룹. 이 셋의 상대 순서가 순위 지표를 좌우한다.
GROUPS = {
    "privacy":  ["PRIVACY_RESTRICTED"],
    "gate":     ["INSUFFICIENT_EVIDENCE", "OUT_OF_SCHEMA",
                 "GRAPH_PATH_NOT_FOUND", "SQL_EXECUTION_FAILURE",
                 "SOURCE_CONFLICT", "BUDGET_EXCEEDED"],
    "router":   ["LOW_ROUTER_CONFIDENCE"],
}
SHIPPED = {"privacy": 0.02, "gate": 0.10, "router": 0.25}


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


def group_of(reason: str) -> str | None:
    for g, reasons in GROUPS.items():
        if reason in reasons:
            return g
    return None


def score(runs, gold, assign: dict) -> tuple[float, float]:
    """거절 상수를 assign으로 바꿔 다시 채점한다. 답한 질의의 점수는 건드리지 않는다."""
    pairs = []
    for r in runs:
        y = bool(gold[r["qid"]]["answerable"])
        if r.get("verdict") == "ABSTAIN":
            g = group_of(r.get("abstain_reason") or "")
            c = assign.get(g, r.get("answer_confidence") or 0.0)
        else:
            c = r.get("answer_confidence") or 0.0
        pairs.append((c, y))
    return _aurc(pairs), _auroc(pairs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--B", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    random.seed(args.seed)

    gold, by = load()
    for s in ("proposed", "no_abstain"):
        if s not in by:
            sys.exit(f"{s} trace 없음")
    on, off = by["proposed"], by["no_abstain"]

    off_pairs = [((r.get("answer_confidence") or 0.0), bool(gold[r["qid"]]["answerable"]))
                 for r in off]
    aurc_off, auroc_off = _aurc(off_pairs), _auroc(off_pairs)
    aurc_ship, auroc_ship = score(on, gold, SHIPPED)

    print(f"기준 OFF:  AURC {aurc_off:.4f}  AUROC {auroc_off:.4f}")
    print(f"출고 상수: AURC {aurc_ship:.4f}  AUROC {auroc_ship:.4f}  "
          f"{SHIPPED}\n")

    # (1) 세 상수의 6가지 재배치 — 값 집합은 그대로 두고 어느 그룹에 붙는지만 바꾼다
    keys = list(SHIPPED)
    vals = [SHIPPED[k] for k in keys]
    perms = []
    print("## 세 상수를 재배치한 6가지 전부\n")
    print("| privacy | gate | router | AURC | AUROC | AURC 악화 | AUROC 악화 |")
    print("|---|---|---|---|---|---|---|")
    for p in itertools.permutations(vals):
        a = dict(zip(keys, p))
        aurc, auroc = score(on, gold, a)
        w_aurc, w_auroc = aurc > aurc_off, auroc < auroc_off
        perms.append({"assignment": a, "aurc": aurc, "auroc": auroc,
                      "aurc_worse": w_aurc, "auroc_worse": w_auroc})
        print(f"| {a['privacy']:.2f} | {a['gate']:.2f} | {a['router']:.2f} | "
              f"{aurc:.4f} | {auroc:.4f} | {'예' if w_aurc else '**아니오**'} | "
              f"{'예' if w_auroc else '**아니오**'} |")

    # (2) 세 상수를 [0, 0.5)에서 무작위로 — 순서 가정 자체를 없앤다
    rnd = []
    for _ in range(args.B):
        a = {k: random.uniform(0.0, 0.5) for k in keys}
        aurc, auroc = score(on, gold, a)
        rnd.append({"aurc": aurc, "auroc": auroc,
                    "aurc_worse": aurc > aurc_off, "auroc_worse": auroc < auroc_off})

    def frac(rows, key):
        return sum(r[key] for r in rows) / len(rows)

    payload = {
        "off": {"aurc": aurc_off, "auroc": auroc_off},
        "shipped": {"assignment": SHIPPED, "aurc": aurc_ship, "auroc": auroc_ship},
        "permutations": perms,
        "random": {
            "B": args.B,
            "aurc_worse_frac": frac(rnd, "aurc_worse"),
            "auroc_worse_frac": frac(rnd, "auroc_worse"),
            "both_worse_frac": sum(r["aurc_worse"] and r["auroc_worse"] for r in rnd) / len(rnd),
            "aurc_range": [min(r["aurc"] for r in rnd), max(r["aurc"] for r in rnd)],
            "auroc_range": [min(r["auroc"] for r in rnd), max(r["auroc"] for r in rnd)],
        },
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    pa = sum(p["aurc_worse"] for p in perms)
    pr = sum(p["auroc_worse"] for p in perms)
    print(f"\n순열 6개 중 AURC 악화 {pa}/6, AUROC 악화 {pr}/6")
    R = payload["random"]
    print(f"\n## [0, 0.5) 무작위 {args.B}회")
    print(f"  AURC 악화  {R['aurc_worse_frac']:.1%}   범위 [{R['aurc_range'][0]:.3f}, {R['aurc_range'][1]:.3f}]")
    print(f"  AUROC 악화 {R['auroc_worse_frac']:.1%}   범위 [{R['auroc_range'][0]:.3f}, {R['auroc_range'][1]:.3f}]")
    print(f"  둘 다 악화 {R['both_worse_frac']:.1%}")
    print(f"\n→ {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
