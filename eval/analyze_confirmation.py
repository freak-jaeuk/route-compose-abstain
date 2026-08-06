"""확인셋을 **층별로** 본다. 감사셋과 풀링하지 않는다.

두 셋은 답변가능 비율이 다르다(감사셋 43/60, 확인셋 50/104). 그래서 risk·precision 처럼
유병률에 민감한 지표를 두 셋 사이에서 빼면 의미가 없다. 여기서 묻는 건 하나다:
감사셋이 건드리지 않은 슬롯에서도 **게이트가 옳은 이유로 발화하는가**.
"""
import json, collections
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
items = {x["qid"]: x for x in (json.loads(l) for l in open(ROOT / "eval/qa/confirmation.jsonl"))}
def load(n):
    p = ROOT / f"eval/runs_confirmation/{n}.jsonl"
    return {r["qid"]: r for r in (json.loads(l) for l in open(p)) if r.get("tool") == "_run"}
P, O = load("proposed"), load("no_abstain")
assert len(P) == len(items) == len(O), f"미완: {len(P)}/{len(items)}/{len(O)}"

print(f"{'stratum':24s} {'n':>3s} {'gold':>5s} {'answered':>8s} {'correct':>8s}  주 사유코드")
print("-" * 88)
tot = collections.Counter()
for s in sorted({i["stratum"] for i in items.values()}):
    qs = [q for q in items if items[q]["stratum"] == s]
    ans = [q for q in qs if P[q]["verdict"] == "ANSWER"]
    gold_ok = items[qs[0]]["answerable"]
    # 옳은 결정 = 답변가능이면 답함, 불가면 거절함
    corr = [q for q in qs if (P[q]["verdict"] == "ANSWER") == items[q]["answerable"]]
    codes = collections.Counter(P[q].get("abstain_reason") for q in qs if P[q]["verdict"] != "ANSWER")
    top = ", ".join(f"{k}:{v}" for k, v in codes.most_common(2)) or "—"
    print(f"{s:24s} {len(qs):3d} {str(gold_ok):>5s} {len(ans):8d} {len(corr):8d}  {top}")
    tot["n"] += len(qs); tot["correct"] += len(corr)

print("-" * 88)
print(f"{'전체':24s} {tot['n']:3d} {'':>5s} {'':>8s} {tot['correct']:8d}  ({tot['correct']/tot['n']:.1%})")

# OFF 조건: 게이트를 다 끄면 답변불가에 몇 개나 답하나
harmO = [q for q in items if O[q]["verdict"] == "ANSWER" and not items[q]["answerable"]]
harmP = [q for q in items if P[q]["verdict"] == "ANSWER" and not items[q]["answerable"]]
print(f"\n해로운 답(답변불가인데 답함):  전체 정책 {len(harmP)}   abstention OFF {len(harmO)}")
fr = [q for q in items if P[q]["verdict"] != "ANSWER" and items[q]["answerable"]]
print(f"오거절(답변가능인데 거절):     {len(fr)}")
if fr:
    c = collections.Counter(P[q].get("abstain_reason") for q in fr)
    print("  사유:", dict(c))
    for q in fr[:6]:
        print(f"    {q} [{items[q]['stratum']}] {P[q].get('abstain_reason')}  {items[q]['question'][:44]}")
json.dump({"n": len(items), "per_stratum_correct": tot["correct"],
           "harmful_full_policy": len(harmP), "harmful_off": len(harmO),
           "false_refusals": len(fr)},
          open(ROOT / "eval/confirmation_summary.json", "w"), indent=1)
