"""confidence 함수 형태에 대한 방어: 손으로 설계한 min(0.9,0.5+0.1|E|) 대신
trace에 독립적으로 기록된 다른 신호로 AUROC를 다시 잰다.

리뷰어의 공격은 "AUROC 하락이 typed abstention의 성질이 아니라 이 confidence 함수의
성질 아니냐"이다. 상수 민감도(1000회 추출)는 **상수**에 대한 방어지 **함수 형태**에
대한 방어가 아니다. 그래서 형태가 전혀 다른 신호 셋으로 같은 비교를 반복한다.
"""
import json, collections
from pathlib import Path

R = Path("eval/runs")
gold = {json.loads(l)["qid"]: json.loads(l) for l in open("eval/qa/gold.jsonl")}

def auroc(pairs):
    """(score, positive). 동점은 0.5로 센다. positive = gold answerable."""
    pos = [s for s, y in pairs if y]
    neg = [s for s, y in pairs if not y]
    if not pos or not neg:
        return float("nan")
    tot = 0.0
    for p in pos:
        for n in neg:
            tot += 1.0 if p > n else (0.5 if p == n else 0.0)
    return tot / (len(pos) * len(neg))

def _yield(call):
    o = call.get("output") or {}
    if not isinstance(o, dict):
        return 0
    # row_count 제외: agg="sum" 호출은 데이터가 없어도 항상 1이라 산출 신호가 아니다
    for k in ("hits", "paths"):
        v = o.get(k)
        if isinstance(v, int):
            return v
    return 0

for name, label in [("proposed", "Abstention ON "), ("no_abstain", "Abstention OFF")]:
    rows = [json.loads(l) for l in open(R / f"{name}.jsonl")]
    byq = collections.defaultdict(list)
    for r in rows:
        byq[r["qid"]].append(r)
    sig = {"answer_confidence": [], "route_conf": [], "n_cited": [], "tool_yield": []}
    for q, rs in byq.items():
        run = next(r for r in rs if r.get("tool") == "_run")
        y = gold[q]["answerable"]
        calls = [c for c in rs if c.get("tool") not in ("_run", "route")]
        sig["answer_confidence"].append((run.get("answer_confidence") or 0.0, y))
        sig["route_conf"].append((run.get("route_conf") or 0.0, y))
        sig["n_cited"].append((len(run.get("cited") or []), y))
        sig["tool_yield"].append((sum(_yield(c) for c in calls), y))
    print(label, " ".join(f"{k}={auroc(v):.3f}" for k, v in sig.items()))

# 차이의 문항 단위 paired bootstrap
import random
random.seed(0)
D = {}
for name in ["proposed", "no_abstain"]:
    rows = [json.loads(l) for l in open(R / f"{name}.jsonl")]
    byq = collections.defaultdict(list)
    for r in rows:
        byq[r["qid"]].append(r)
    D[name] = byq
qids = sorted(gold)

def score(name, q, which):
    rs = D[name][q]
    run = next(r for r in rs if r.get("tool") == "_run")
    if which == "answer_confidence": return run.get("answer_confidence") or 0.0
    if which == "route_conf":        return run.get("route_conf") or 0.0
    if which == "n_cited":           return len(run.get("cited") or [])
    return sum(_yield(c) for c in rs if c.get("tool") not in ("_run", "route"))

print()
for which in ["answer_confidence", "n_cited", "tool_yield", "route_conf"]:
    def delta(sample):
        a = auroc([(score("proposed", q, which), gold[q]["answerable"]) for q in sample])
        b = auroc([(score("no_abstain", q, which), gold[q]["answerable"]) for q in sample])
        return a - b
    base = delta(qids)
    d = sorted(delta([random.choice(qids) for _ in range(60)]) for _ in range(2000))
    lo, hi = d[50], d[1950]
    print(f"{which:20s} ΔAUROC={base:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]  {'0 제외' if lo*hi>0 else '0 포함'}")
