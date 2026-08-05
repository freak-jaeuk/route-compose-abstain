"""AUGRC (Traub et al. 2024) = 미탐지 실패의 평균 위험. AURC와 달리 분모가 전체 N이다.

AURC: coverage 지점마다 risk = (수락된 것 중 실패)/(수락 수)
AUGRC: 같은 지점에서 generalized risk = (수락된 것 중 실패)/(전체 N)

즉 AURC는 '수락한 것 중 얼마나 틀렸나', AUGRC는 '전체 중 얼마나 많은 실패를 놓쳤나'.
후자가 이 논문의 손실(gold가 답변불가인데 답한 것)에 더 가깝다 — 논문 §2에서 인용만 하고
값을 안 냈던 지표다.
"""
import json, sys
from pathlib import Path

R = Path("eval/runs")
gold = {json.loads(l)["qid"]: json.loads(l) for l in open("eval/qa/gold.jsonl")}

def curve(scored, denom):
    ranked = sorted(scored, key=lambda x: -x[0])
    vals, wrong, i, n = [], 0, 0, len(ranked)
    while i < n:
        j = i
        while j < n and ranked[j][0] == ranked[i][0]:
            wrong += not ranked[j][1]
            j += 1
        vals += [wrong / (j if denom == "accepted" else n)] * (j - i)
        i = j
    return sum(vals) / len(vals) if vals else 0.0

for name, label in [("proposed", "Abstention ON"), ("no_abstain", "Abstention OFF")]:
    rows = [r for r in (json.loads(l) for l in open(R / f"{name}.jsonl")) if r.get("tool") == "_run"]
    assert len(rows) == 60, f"{name}: {len(rows)}/60"
    scored = [(r.get("answer_confidence") or 0.0, gold[r["qid"]]["answerable"]) for r in rows]
    print(f"{label:16s} AURC={curve(scored,'accepted'):.4f}  AUGRC={curve(scored,'all'):.4f}")

# 표 1의 다른 행과 같은 방식(문항 단위 paired bootstrap)으로 차이의 CI
import random
random.seed(0)
data = {}
for name in ["proposed", "no_abstain"]:
    rows = {r["qid"]: r for r in (json.loads(l) for l in open(R / f"{name}.jsonl")) if r.get("tool") == "_run"}
    data[name] = rows
qids = sorted(gold)
def augrc_of(name, sample):
    s = [(data[name][q].get("answer_confidence") or 0.0, gold[q]["answerable"]) for q in sample]
    return curve(s, "all")
base = augrc_of("proposed", qids) - augrc_of("no_abstain", qids)
d = []
for _ in range(10000):
    smp = [random.choice(qids) for _ in range(60)]
    d.append(augrc_of("proposed", smp) - augrc_of("no_abstain", smp))
d.sort()
lo, hi = d[250], d[9750]
print(f"\nAUGRC diff (ON-OFF) = {base:+.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]  0 제외: {'YES' if lo*hi>0 else 'no'}")
