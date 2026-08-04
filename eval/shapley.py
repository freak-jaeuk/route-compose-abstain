"""트리거 기여도의 Shapley 값 — leave-one-out이 캐스케이드 때문에 실패하는 자리를 메운다.

`loo_ablation.py`는 트리거를 하나씩 꺼서 기여도를 쟀고, 그 결과 셋 중 셋이 0으로
나왔다. 0인 이유는 기여가 없어서가 아니라 하류 게이트가 그 질의를 대신 잡기 때문이다
(캐스케이드). 즉 leave-one-out은 상호작용이 있는 기여를 분해하지 못한다.

Shapley 값은 그 상호작용을 정면으로 다룬다: 각 트리거의 기여를 **모든 가능한 부분집합에
대해** 한계 기여의 가중 평균으로 정의한다. 트리거가 4개뿐이라 부분집합이 2^4 = 16개,
즉 16 × 60 = 960회 실행이면 근사 없이 **정확한** Shapley 값이 나온다.

  φ_i = Σ_{S ⊆ N\\{i}}  |S|!(n−|S|−1)!/n!  ·  [v(S∪{i}) − v(S)]

v(S) = 트리거 집합 S만 켰을 때의 answerability risk. 나머지 4개 트리거
(router confidence, SQL execution, source conflict, budget)는 모든 조건에서 켜 둔다 —
분해 대상은 논문이 다루는 4개다. φ가 음수면 그 트리거가 risk를 낮춘다(= 도움이 된다).

용례:  python eval/shapley.py          # 없는 조건은 실행하고, 다 있으면 계산만
       python eval/shapley.py --dry    # 무엇을 실행해야 하는지만 본다
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rca.orchestrator import ALL_TRIGGERS, Agents, run_query  # noqa: E402
from rca.trace import Tracer, read_traces  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "eval/qa/gold.jsonl"
RUNS = ROOT / "eval/runs"
OUT = ROOT / "eval/shapley.json"

# 분해 대상 4개. 순서 고정 — 조건 이름이 이 순서에 의존한다.
PLAYERS = ["INSUFFICIENT_EVIDENCE", "PRIVACY_RESTRICTED",
           "OUT_OF_SCHEMA", "GRAPH_PATH_NOT_FOUND"]
SHORT = {"INSUFFICIENT_EVIDENCE": "E", "PRIVACY_RESTRICTED": "P",
         "OUT_OF_SCHEMA": "S", "GRAPH_PATH_NOT_FOUND": "G"}
ALWAYS_ON = frozenset(ALL_TRIGGERS) - frozenset(PLAYERS)


def cond_name(subset: frozenset) -> str:
    """부분집합 → 조건 이름. 이미 있는 조건은 그 이름을 재사용한다(재실행 낭비 방지)."""
    if subset == frozenset(PLAYERS):
        return "proposed"
    missing = frozenset(PLAYERS) - subset
    if len(missing) == 1:
        return f"loo_{next(iter(missing)).lower()}"
    return "shap_" + ("".join(SHORT[p] for p in PLAYERS if p in subset) or "none")


def subsets():
    for k in range(len(PLAYERS) + 1):
        for c in itertools.combinations(PLAYERS, k):
            yield frozenset(c)


def load_gold() -> dict:
    return {json.loads(l)["qid"]: json.loads(l)
            for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip()}


def risk_of(runs, gold) -> float:
    answered = [r for r in runs if r.get("verdict") == "ANSWER"]
    if not answered:
        return 0.0
    return sum(not gold[r["qid"]]["answerable"] for r in answered) / len(answered)


def run_condition(name: str, abstain_on: frozenset, gold_rows: list, agents) -> None:
    out = RUNS / f"{name}.jsonl"
    done = set()
    if out.exists():
        done = {json.loads(l)["qid"] for l in out.read_text(encoding="utf-8").splitlines()
                if l.strip() and '"_run"' in l}
    if len(done) >= len(gold_rows):
        return
    tr = Tracer(out, run_id="shapley", system=name, backbone="gpt-oss-20b",
                temperature=0.0, seed=0)
    t0 = time.perf_counter()
    for g in gold_rows:
        if g["qid"] in done:
            continue
        with tr.query(g["qid"]) as summ:
            st = run_query(g["question"], g["qid"], tr, agents, system=name,
                           abstain_on=abstain_on, use_llm=False, generate=False)
            summ.update(verdict=st.verdict, answer_confidence=st.answer_confidence,
                        abstain_reason=st.abstain_reason, route_pred=st.route_pred,
                        route_conf=st.route_conf, cited=st.cited)
    print(f"  {name:14s} {len(gold_rows)}문항 {time.perf_counter() - t0:.0f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    gold = load_gold()
    gold_rows = [json.loads(l) for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip()]

    plan = [(s, cond_name(s)) for s in subsets()]
    have = {p.stem for p in RUNS.glob("*.jsonl")}
    todo = [(s, n) for s, n in plan if n not in have]
    print(f"부분집합 {len(plan)}개, 이미 있음 {len(plan) - len(todo)}, 실행 필요 {len(todo)}")
    if args.dry:
        for s, n in todo:
            print(f"  {n:14s} on={sorted(SHORT[p] for p in s) or '없음'}")
        return

    if todo:
        agents = Agents()
        for s, n in todo:
            run_condition(n, ALWAYS_ON | s, gold_rows, agents)

    # 조건별 v(S) = risk
    rows, _ = read_traces(RUNS)
    by = defaultdict(list)
    for r in rows:
        if r.get("tool") == "_run":
            by[r["system"]].append(r)

    v = {}
    for s, n in plan:
        if n not in by:
            sys.exit(f"조건 {n} 의 trace가 없다 — 먼저 실행하라")
        if len(by[n]) != len(gold):
            sys.exit(f"조건 {n}: {len(by[n])}/{len(gold)} 문항 — 미완이다")
        v[s] = risk_of(by[n], gold)

    # 정확 Shapley
    n_players = len(PLAYERS)
    phi = {}
    for p in PLAYERS:
        others = [q for q in PLAYERS if q != p]
        total = 0.0
        for k in range(len(others) + 1):
            w = math.factorial(k) * math.factorial(n_players - k - 1) / math.factorial(n_players)
            for c in itertools.combinations(others, k):
                S = frozenset(c)
                total += w * (v[S | {p}] - v[S])
        phi[p] = total

    # 효율성 공리 검증: Σφ = v(N) − v(∅). 어긋나면 구현이 틀린 것이다.
    lhs, rhs = sum(phi.values()), v[frozenset(PLAYERS)] - v[frozenset()]
    assert abs(lhs - rhs) < 1e-9, f"효율성 위반: Σφ={lhs} ≠ v(N)−v(∅)={rhs}"

    loo = {p: v[frozenset(PLAYERS)] - v[frozenset(PLAYERS) - {p}] for p in PLAYERS}

    print(f"\nv(∅) = {v[frozenset()]:.4f} (4개 전부 끔), "
          f"v(N) = {v[frozenset(PLAYERS)]:.4f} (전부 켬)\n")
    print("| 트리거 | Shapley φ | leave-one-out Δ | 차이 |")
    print("|---|---|---|---|")
    for p in PLAYERS:
        print(f"| {p} | {phi[p]:+.4f} | {-loo[p]:+.4f} | {phi[p] + loo[p]:+.4f} |")
    print(f"\nΣφ = {lhs:+.4f} = v(N) − v(∅) = {rhs:+.4f}  (효율성 공리 통과)")
    print("φ < 0 = 그 트리거가 risk를 낮춘다. LOO Δ는 부호를 뒤집어 같은 방향으로 맞췄다.")

    OUT.write_text(json.dumps({
        "players": PLAYERS,
        "v": {cond_name(s): v[s] for s in v},
        "shapley": phi,
        "leave_one_out": {p: -loo[p] for p in PLAYERS},
        "efficiency_check": {"sum_phi": lhs, "v_full_minus_empty": rhs},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
