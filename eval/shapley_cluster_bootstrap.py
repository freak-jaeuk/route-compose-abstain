"""대조쌍을 클러스터로 묶어 재표집한다.

벤치마크의 6개 답변불가 문항은 답변가능 문항의 최소 편집본이다. 즉 의도적으로
종속인 관측이므로, 문항을 독립 추출하면 설계상 짝지어진 예를 독립 관측으로 세게 된다.
쌍을 하나의 클러스터로 묶고, 나머지는 단독 클러스터로 두어 클러스터 단위로 재표집한다.
"""
import json, math, itertools, random
from pathlib import Path

P = ["INSUFFICIENT_EVIDENCE", "PRIVACY_RESTRICTED", "OUT_OF_SCHEMA",
     "GRAPH_PATH_NOT_FOUND", "LOW_ROUTER_CONFIDENCE"]
SH = {"INSUFFICIENT_EVIDENCE": "E", "PRIVACY_RESTRICTED": "P", "OUT_OF_SCHEMA": "S",
      "GRAPH_PATH_NOT_FOUND": "G", "LOW_ROUTER_CONFIDENCE": "R"}
# (답변불가 twin, 그 최소편집 원본) — difflib 최근접 + 수작업 확인
PAIRS = [("g034", "g004"), ("g035", "g001"), ("g036", "g001"),
         ("g037", "g046"), ("g041", "g004"), ("g055", "g001")]
R = Path("eval/runs")
gold = {json.loads(l)["qid"]: json.loads(l) for l in open("eval/qa/gold.jsonl")}

def cond(s):
    if s == frozenset(P): return "proposed"
    m = frozenset(P) - s
    if len(m) == 1: return "loo_" + next(iter(m)).lower()
    return "shap5_" + ("".join(SH[p] for p in P if p in s) or "none")

subs = [frozenset(c) for k in range(6) for c in itertools.combinations(P, k)]
tab = {}
for s in subs:
    rows = {r["qid"]: r for r in (json.loads(l) for l in open(R / f"{cond(s)}.jsonl"))
            if r.get("tool") == "_run"}
    tab[s] = {q: (rows[q]["verdict"] == "ANSWER",
                  rows[q]["verdict"] == "ANSWER" and not gold[q]["answerable"]) for q in rows}

# 클러스터 구성: 원본 하나가 여러 twin의 모체이므로 연결 성분으로 묶는다
parent = {q: q for q in gold}
def find(a):
    while parent[a] != a: parent[a] = parent[parent[a]]; a = parent[a]
    return a
for a, b in PAIRS:
    ra, rb = find(a), find(b)
    if ra != rb: parent[ra] = rb
groups = {}
for q in gold: groups.setdefault(find(q), []).append(q)
CLUSTERS = list(groups.values())

def phi_of(sample):
    v = {}
    for s in subs:
        a = sum(tab[s][q][0] for q in sample)
        e = sum(tab[s][q][1] for q in sample)
        v[s] = e / a if a else 0.0
    n = len(P); out = {}
    for p in P:
        others = [x for x in P if x != p]; tot = 0.0
        for k in range(len(others) + 1):
            w = math.factorial(k) * math.factorial(n - k - 1) / math.factorial(n)
            for c in itertools.combinations(others, k):
                S = frozenset(c); tot += w * (v[S | {p}] - v[S])
        out[p] = tot
    return out

qids = sorted(gold)
base = phi_of(qids)
random.seed(0); B = 6000
draws = {p: [] for p in P}
frac_neg = {p: 0 for p in P}
for _ in range(B):
    # 표준 클러스터 부트스트랩: 클러스터를 개수만큼 복원추출한다(총 문항 수는 가변).
    smp = [q for _ in CLUSTERS for q in random.choice(CLUSTERS)]
    ph = phi_of(smp)
    for p in P:
        draws[p].append(ph[p])
        frac_neg[p] += ph[p] < 0

sizes = sorted(len(c) for c in CLUSTERS)
print(f"클러스터 {len(CLUSTERS)}개 (최대 {sizes[-1]}문항, 대조쌍 {len(PAIRS)}개 반영)")
print(f"\n{'gate':26s} {'phi':>9s}  {'95% CI (cluster)':>22s}  0 제외")
out = {}
for p in P:
    d = sorted(draws[p]); lo, hi = d[int(.025 * B)], d[int(.975 * B)]
    out[p] = {"phi": base[p], "ci": [lo, hi], "excludes_zero": lo * hi > 0,
              "frac_negative": frac_neg[p] / B}
    print(f"  {p:24s} {base[p]:+9.4f}  [{lo:+8.4f}, {hi:+8.4f}]  {'YES' if lo*hi>0 else 'no':3s}  P(phi<0)={frac_neg[p]/B:.3f}")
json.dump({"clusters": len(CLUSTERS), "pairs": PAIRS, "B": B, "result": out},
          open("eval/shapley_cluster_bootstrap.json", "w"), indent=1)
print("\n-> eval/shapley_cluster_bootstrap.json")
