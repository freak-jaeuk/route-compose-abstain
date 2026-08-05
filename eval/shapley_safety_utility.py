"""조건부 risk 하나로 게이트를 평가하면 두 효과가 섞인다.

risk(S) = (답한 것 중 답변불가) / (답한 수) 는 분모가 coalition마다 바뀐다.
그래서 φ<0 이 '해로운 답을 막았다'인지 '답변가능한 걸 더 거절해 분모를 줄였다'인지
구분되지 않는다. 실제로 schema 단독(4/42=0.095)과 전체 정책(4/34=0.118)은
**오답 수가 같은데** 조건부 risk만 다르다.

그래서 분모가 고정된 두 value function을 따로 Shapley 분해한다.
  H(S) = 답변불가인데 답한 수 / N            (안전 이득; 낮을수록 좋다)
  F(S) = 답변가능한데 거절한 수 / N_answerable (커버리지 비용; 낮을수록 좋다)
새 실행 없이 기존 32개 coalition 결과만 쓴다.
"""
import json, math, itertools
from pathlib import Path

P = ["INSUFFICIENT_EVIDENCE", "PRIVACY_RESTRICTED", "OUT_OF_SCHEMA",
     "GRAPH_PATH_NOT_FOUND", "LOW_ROUTER_CONFIDENCE"]
SH = {"INSUFFICIENT_EVIDENCE": "E", "PRIVACY_RESTRICTED": "P", "OUT_OF_SCHEMA": "S",
      "GRAPH_PATH_NOT_FOUND": "G", "LOW_ROUTER_CONFIDENCE": "R"}
R = Path("eval/runs")
gold = {json.loads(l)["qid"]: json.loads(l) for l in open("eval/qa/gold.jsonl")}
N = len(gold)
NA = sum(g["answerable"] for g in gold.values())

def cond(s):
    if s == frozenset(P): return "proposed"
    m = frozenset(P) - s
    if len(m) == 1: return "loo_" + next(iter(m)).lower()
    return "shap5_" + ("".join(SH[p] for p in P if p in s) or "none")

subs = [frozenset(c) for k in range(6) for c in itertools.combinations(P, k)]
V = {}
for s in subs:
    rows = [r for r in (json.loads(l) for l in open(R / f"{cond(s)}.jsonl")) if r.get("tool") == "_run"]
    assert len(rows) == N
    ans = [r for r in rows if r["verdict"] == "ANSWER"]
    harm = sum(not gold[r["qid"]]["answerable"] for r in ans)
    falseref = sum(gold[r["qid"]]["answerable"] for r in rows if r["verdict"] != "ANSWER")
    V[s] = {"risk": harm / len(ans) if ans else 0.0, "H": harm / N, "F": falseref / NA}

def shapley(key):
    n = len(P); out = {}
    for p in P:
        others = [q for q in P if q != p]; tot = 0.0
        for k in range(len(others) + 1):
            w = math.factorial(k) * math.factorial(n - k - 1) / math.factorial(n)
            for c in itertools.combinations(others, k):
                S = frozenset(c); tot += w * (V[S | {p}][key] - V[S][key])
        out[p] = tot
    return out

phi = {k: shapley(k) for k in ("risk", "H", "F")}
e = frozenset(); f = frozenset(P)
print(f"{'':26s} {'risk':>19s} {'harmful-answer':>19s} {'false-refusal':>19s}")
print(f"{'v(empty) -> v(N)':26s} "
      f"{V[e]['risk']:.3f}->{V[f]['risk']:.3f}      "
      f"{V[e]['H']:.3f}->{V[f]['H']:.3f}      {V[e]['F']:.3f}->{V[f]['F']:.3f}")
print()
print(f"{'gate':26s} {'phi_risk':>10s} {'phi_H':>10s} {'phi_F':>10s}")
for p in sorted(P, key=lambda x: phi['risk'][x]):
    print(f"{p:26s} {phi['risk'][p]:+10.4f} {phi['H'][p]:+10.4f} {phi['F'][p]:+10.4f}")
print()
for k in ("risk", "H", "F"):
    print(f"  sum phi_{k:5s} = {sum(phi[k].values()):+.4f}   v(N)-v(empty) = {V[f][k]-V[e][k]:+.4f}")
json.dump({"v": {cond(s): V[s] for s in subs}, "shapley": phi},
          open("eval/shapley_safety_utility.json", "w"), indent=1)
print("\n-> eval/shapley_safety_utility.json")
