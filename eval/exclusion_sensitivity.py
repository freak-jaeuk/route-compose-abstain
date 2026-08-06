"""Leave-a-subset-out sensitivity for the headline comparison.

Two questions a reviewer asks and neither needs a second annotator:
  (a) does the result depend on the one item whose gold label we corrected (g046)?
  (b) does it depend on the four personal-information items, which the OFF
      control is forced to answer because OFF disables the privacy screen?

Recomputes risk / coverage / AURC / AUROC / precision / recall for both
conditions and the paired-bootstrap CI of each difference, plus the exact
five-player Shapley decomposition, over the reduced item set.  Reads only
eval/runs/*.jsonl.

    python eval/exclusion_sensitivity.py
"""
from __future__ import annotations

import json
import random
from itertools import combinations
from math import factorial
from pathlib import Path

E = Path(__file__).resolve().parent
GOLD = {r["qid"]: r["answerable"] for r in map(json.loads, open(E / "qa/gold.jsonl"))}
ALL = sorted(GOLD)

PLAYERS = ["E", "P", "S", "G", "R"]
CODE = {"E": "INSUFFICIENT_EVIDENCE", "P": "PRIVACY_RESTRICTED", "S": "OUT_OF_SCHEMA",
        "G": "GRAPH_PATH_NOT_FOUND", "R": "LOW_ROUTER_CONFIDENCE"}


_CACHE: dict[str, dict] = {}


def runs(name):
    if name not in _CACHE:
        _CACHE[name] = {r["qid"]: r for r in map(json.loads, open(E / "runs" / f"{name}.jsonl"))
                        if r.get("tool") == "_run"}
    return _CACHE[name]


def coalition_run(s: str) -> str:
    """Run file holding the coalition `s`.  4-subsets were logged as leave-one-out."""
    if len(s) == 5:
        return "proposed"
    if len(s) == 4:
        missing = next(p for p in PLAYERS if p not in s)
        return f"loo_{CODE[missing].lower()}"
    return f"shap5_{s}" if s else "shap5_none"


def aurc(pairs):
    ranked = sorted(pairs, key=lambda x: -x[0])
    per, wrong, i, n = [], 0, 0, len(ranked)
    while i < n:
        j = i
        while j < n and ranked[j][0] == ranked[i][0]:
            wrong += not ranked[j][1]
            j += 1
        per += [wrong / j] * (j - i)
        i = j
    return sum(per) / len(per) if per else 0.0


def auroc(pairs):
    pos = [c for c, y in pairs if y]
    neg = [c for c, y in pairs if not y]
    if not pos or not neg:
        return 0.5
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def metrics(R, qids):
    rs = [R[q] for q in qids]
    ans = [r for r in rs if r["verdict"] == "ANSWER"]
    ref = [r for r in rs if r["verdict"] != "ANSWER"]
    tp = sum(1 for r in ref if not GOLD[r["qid"]])
    fn = sum(1 for r in ans if not GOLD[r["qid"]])
    pr = [(r.get("answer_confidence") or 0.0, bool(GOLD[r["qid"]])) for r in rs]
    return {"n": len(rs), "cov": len(ans) / len(rs),
            "risk": (fn / len(ans)) if ans else 0.0, "harm": fn, "ans": len(ans),
            "aurc": aurc(pr), "auroc": auroc(pr),
            "prec": tp / len(ref) if ref else 0.0,
            "rec": tp / (tp + fn) if tp + fn else 0.0}


def risk_of(name, qids):
    R = runs(name)
    ans = [q for q in qids if R[q]["verdict"] == "ANSWER"]
    return (sum(1 for q in ans if not GOLD[q]) / len(ans)) if ans else 0.0


def shapley(qids):
    v = {"": risk_of("shap5_none", qids)}
    for k in range(1, 6):
        for c in combinations(PLAYERS, k):
            s = "".join(c)
            v[s] = risk_of(coalition_run(s), qids)
    phi = {}
    for i in PLAYERS:
        rest = [p for p in PLAYERS if p != i]
        tot = 0.0
        for k in range(5):
            for c in combinations(rest, k):
                key = "".join(sorted(c, key=PLAYERS.index))
                wi = "".join(sorted(c + (i,), key=PLAYERS.index))
                tot += factorial(k) * factorial(4 - k) / factorial(5) * (v[wi] - v[key])
        phi[CODE[i]] = tot
    return phi, v


def paired_ci(qids, B=10000, seed=0):
    random.seed(seed)
    ON, OFF = runs("proposed"), runs("no_abstain")
    keys = ["risk", "aurc", "auroc"]
    d = {k: [] for k in keys}
    for _ in range(B):
        s = [random.choice(qids) for _ in qids]
        a, b = metrics(ON, s), metrics(OFF, s)
        for k in keys:
            d[k].append(a[k] - b[k])
    out = {}
    for k in keys:
        v = sorted(d[k])
        out[k] = (v[int(0.025 * B)], v[min(B - 1, int(0.975 * B))])
    return out


def report(label, drop):
    qids = [q for q in ALL if q not in drop]
    ON, OFF = metrics(runs("proposed"), qids), metrics(runs("no_abstain"), qids)
    ci = paired_ci(qids)
    print(f"\n### {label}  (drop {sorted(drop)} -> n={len(qids)}, "
          f"answerable {sum(GOLD[q] for q in qids)})")
    print(f"  coverage   ON {ON['cov']:.3f} ({ON['ans']}/{ON['n']})   OFF {OFF['cov']:.3f}")
    print(f"  risk       ON {ON['risk']:.3f} ({ON['harm']}/{ON['ans']})   "
          f"OFF {OFF['risk']:.3f} ({OFF['harm']}/{OFF['ans']})   "
          f"diff {ON['risk']-OFF['risk']:+.3f} [{ci['risk'][0]:+.3f}, {ci['risk'][1]:+.3f}]")
    print(f"  AURC       ON {ON['aurc']:.3f}   OFF {OFF['aurc']:.3f}   "
          f"diff {ON['aurc']-OFF['aurc']:+.3f} [{ci['aurc'][0]:+.3f}, {ci['aurc'][1]:+.3f}]")
    print(f"  AUROC      ON {ON['auroc']:.3f}   OFF {OFF['auroc']:.3f}   "
          f"diff {ON['auroc']-OFF['auroc']:+.3f} [{ci['auroc'][0]:+.3f}, {ci['auroc'][1]:+.3f}]")
    print(f"  abst prec  {ON['prec']:.3f}   recall {ON['rec']:.3f}")
    phi, v = shapley(qids)
    print(f"  Shapley    " + "  ".join(f"{c[:4]} {p:+.3f}" for c, p in phi.items()))
    print(f"             sum {sum(phi.values()):+.3f}   v(N)-v(0) "
          f"{v['EPSGR'] - v['']:+.3f}   v(0)={v['']:.3f}")
    # evidence gate alone
    Ea = [q for q in qids if runs("shap5_E")[q]["verdict"] == "ANSWER"]
    print(f"  E alone: answers {len(Ea)}, errors {sum(1 for q in Ea if not GOLD[q])}")


PRIV_UNANS = {"g038", "g039", "g040", "g057"}
PRIV_ALL = PRIV_UNANS | {"g015", "g049"}

if __name__ == "__main__":
    report("baseline (all 60)", set())
    report("item 4: corrected item g046 excluded", {"g046"})
    report("item 5a: 4 gold-unanswerable privacy items excluded", PRIV_UNANS)
    report("item 5b: all 6 privacy-gate refusals excluded", PRIV_ALL)
    report("items 4+5a jointly", PRIV_UNANS | {"g046"})
