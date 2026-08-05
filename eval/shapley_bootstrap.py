"""Shapley φ의 항목 단위 부트스트랩. 새 실행 없이 기존 960개 결과만 사용."""
import json, math, itertools, random
from pathlib import Path

PLAYERS = ["INSUFFICIENT_EVIDENCE","PRIVACY_RESTRICTED","OUT_OF_SCHEMA","GRAPH_PATH_NOT_FOUND"]
SHORT = {"INSUFFICIENT_EVIDENCE":"E","PRIVACY_RESTRICTED":"P","OUT_OF_SCHEMA":"S","GRAPH_PATH_NOT_FOUND":"G"}
R = Path("eval/runs")

def cond(sub):
    if sub == frozenset(PLAYERS): return "proposed"
    miss = frozenset(PLAYERS) - sub
    if len(miss) == 1: return f"loo_{next(iter(miss)).lower()}"
    return "shap_" + ("".join(SHORT[p] for p in PLAYERS if p in sub) or "none")

gold = {json.loads(l)["qid"]: json.loads(l) for l in open("eval/qa/gold.jsonl")}
qids = sorted(gold)
subs = [frozenset(c) for k in range(5) for c in itertools.combinations(PLAYERS,k)]

# per-item: 각 구성에서 (답변했나, 답변했다면 오답인가)
tab = {}
for s in subs:
    rows = {r["qid"]: r for r in (json.loads(l) for l in open(R/f"{cond(s)}.jsonl")) if r.get("tool")=="_run"}
    assert len(rows)==60, (cond(s), len(rows))
    tab[s] = {q: (rows[q]["verdict"]=="ANSWER",
                  rows[q]["verdict"]=="ANSWER" and not gold[q]["answerable"]) for q in qids}

def phi_of(sample):
    v = {}
    for s in subs:
        a = sum(tab[s][q][0] for q in sample)
        e = sum(tab[s][q][1] for q in sample)
        v[s] = e/a if a else 0.0
    n = len(PLAYERS); out = {}
    for p in PLAYERS:
        others = [x for x in PLAYERS if x != p]; tot = 0.0
        for k in range(len(others)+1):
            w = math.factorial(k)*math.factorial(n-k-1)/math.factorial(n)
            for c in itertools.combinations(others,k):
                S = frozenset(c); tot += w*(v[S|{p}]-v[S])
        out[p] = tot
    return out

base = phi_of(qids)
print("point estimates (논문 대조용):")
for p in PLAYERS: print(f"  {p:24s} {base[p]:+.4f}")

random.seed(0); B = 6000
draws = {p: [] for p in PLAYERS}
for _ in range(B):
    smp = [random.choice(qids) for _ in range(60)]
    ph = phi_of(smp)
    for p in PLAYERS: draws[p].append(ph[p])

print(f"\n부트스트랩 B={B}, 항목 재표집:")
print(f"{'trigger':24s} {'phi':>8s}  {'95% CI':>20s}   0 제외?")
for p in PLAYERS:
    d = sorted(draws[p]); lo, hi = d[int(.025*B)], d[int(.975*B)]
    print(f"  {p:22s} {base[p]:+.4f}  [{lo:+.4f}, {hi:+.4f}]  {'YES' if lo*hi>0 else 'no'}")
