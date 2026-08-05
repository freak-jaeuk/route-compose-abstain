"""P0-4: 결정은 그대로 두고 로깅만 바꾼다.

ON 조건에서 거절된 질의의 confidence는 사유별 상수로 덮어써진다(privacy 0.02,
실행 게이트 0.10, router 0.25). 대신 그 질의가 **실제로 받은 근거 개수**로 같은 공식
c=min(0.9, 0.5+0.1|E|)을 적용하면, 결정(coverage·risk)은 한 글자도 안 바뀌고
순위 지표만 바뀐다. 이게 회복되면 AUROC 하락은 abstention이 아니라 로깅 탓이다.
"""
import json, collections
from pathlib import Path

R = Path("eval/runs")
gold = {json.loads(l)["qid"]: json.loads(l) for l in open("eval/qa/gold.jsonl")}

def auroc(pairs):
    pos = [s for s, y in pairs if y]; neg = [s for s, y in pairs if not y]
    return sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg) / (len(pos) * len(neg))

def aurc(pairs):
    r = sorted(pairs, key=lambda x: -x[0]); v = []; w = 0; i = 0; n = len(r)
    while i < n:
        j = i
        while j < n and r[j][0] == r[i][0]: w += not r[j][1]; j += 1
        v += [w / j] * (j - i); i = j
    return sum(v) / len(v)

def evidence_count(calls):
    """그 질의가 실제로 받은 근거 개수. row_count는 제외(agg는 데이터 없어도 1)."""
    tot = 0
    for c in calls:
        o = c.get("output") or {}
        if isinstance(o, dict):
            for k in ("hits", "paths"):
                if isinstance(o.get(k), int): tot += o[k]
    return tot

rows = collections.defaultdict(list)
for l in open(R / "proposed.jsonl"):
    r = json.loads(l); rows[r["qid"]].append(r)

overwritten, preserved = [], []
n_ans = n_ref = 0
for q, rs in rows.items():
    run = next(r for r in rs if r.get("tool") == "_run")
    calls = [c for c in rs if c.get("tool") not in ("_run", "route")]
    y = gold[q]["answerable"]
    overwritten.append((run.get("answer_confidence") or 0.0, y))
    if run["verdict"] == "ANSWER":
        preserved.append((run.get("answer_confidence") or 0.0, y)); n_ans += 1
    else:
        preserved.append((min(0.9, 0.5 + 0.1 * evidence_count(calls)), y)); n_ref += 1

off = [(r.get("answer_confidence") or 0.0, gold[r["qid"]]["answerable"])
       for r in (json.loads(l) for l in open(R / "no_abstain.jsonl")) if r.get("tool") == "_run"]

print(f"answered {n_ans}, refused {n_ref}  (결정은 두 로깅에서 동일)")
print()
print(f"{'logging':34s} {'AUROC':>7s} {'AURC':>7s}")
print(f"{'ON, overwritten (as published)':34s} {auroc(overwritten):7.3f} {aurc(overwritten):7.3f}")
print(f"{'ON, pre-policy preserved':34s} {auroc(preserved):7.3f} {aurc(preserved):7.3f}")
print(f"{'OFF (no abstention)':34s} {auroc(off):7.3f} {aurc(off):7.3f}")
