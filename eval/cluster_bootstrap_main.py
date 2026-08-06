"""주 표 지표를 클러스터 부트스트랩으로 다시 낸다 (shapley_cluster_bootstrap.py와 동일한 클러스터링).

bootstrap_ci.py 는 문항 단위 paired bootstrap, shapley_cluster_bootstrap.py 는
대조쌍을 묶은 클러스터 부트스트랩이다. 같은 논문에서 두 스킴이 섞여 있는지 확인용.
지표 정의는 bootstrap_ci.py 에서 그대로 재사용한다(정의가 갈라지면 비교가 무의미하므로).

용례:  python eval/cluster_bootstrap_main.py [--B 10000] [--seed 0]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bootstrap_ci import ci, load, metrics  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# shapley_cluster_bootstrap.py 와 동일 (답변불가 twin, 최소편집 원본)
PAIRS = [("g034", "g004"), ("g035", "g001"), ("g036", "g001"),
         ("g037", "g046"), ("g041", "g004"), ("g055", "g001")]
KEYS = ["risk", "coverage", "aurc", "auroc", "abst_precision", "abst_recall"]


def clusters_of(qids):
    """연결 성분으로 묶는다 — 원본 하나가 여러 twin의 모체일 수 있다."""
    parent = {q: q for q in qids}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, b in PAIRS:
        if a not in parent or b not in parent:
            raise SystemExit(f"MISSING PAIR ID: {a} or {b} not in gold")
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups = {}
    for q in qids:
        groups.setdefault(find(q), []).append(q)
    return list(groups.values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--B", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    random.seed(args.seed)

    gold, by = load()
    qids = sorted(set(by["proposed"]) & set(by["no_abstain"]))
    CL = clusters_of(qids)
    sizes = sorted(len(c) for c in CL)
    print(f"공통 문항 {len(qids)}개 -> 클러스터 {len(CL)}개 (최대 {sizes[-1]}), B={args.B}, seed={args.seed}\n")

    point = {s: metrics(by[s], qids, gold) for s in ("proposed", "no_abstain")}
    diffs = {k: [] for k in KEYS}
    draws = {s: {k: [] for k in KEYS} for s in point}
    for _ in range(args.B):
        # shapley_cluster_bootstrap.py 와 동일: 클러스터를 개수만큼 복원추출, 총 문항 수는 가변
        samp = [q for _ in CL for q in random.choice(CL)]
        m_on = metrics(by["proposed"], samp, gold)
        m_off = metrics(by["no_abstain"], samp, gold)
        for k in KEYS:
            draws["proposed"][k].append(m_on[k])
            draws["no_abstain"][k].append(m_off[k])
            diffs[k].append(m_on[k] - m_off[k])

    old = json.loads((ROOT / "eval/bootstrap_ci.json").read_text())
    payload = {"n": len(qids), "clusters": len(CL), "B": args.B, "seed": args.seed,
               "pairs": PAIRS, "point": point, "ci": {}, "diff": {}}
    print("| 지표 | 차이 [95% CI cluster] | 0제외 | [95% CI item, 기존] | 0제외 |")
    print("|---|---|---|---|---|")
    for k in KEYS:
        d_lo, d_hi = ci(diffs[k])
        d = point["proposed"][k] - point["no_abstain"][k]
        exc = not (d_lo <= 0 <= d_hi)
        o = old["diff"][k]
        payload["ci"][k] = {"proposed": list(ci(draws["proposed"][k])),
                            "no_abstain": list(ci(draws["no_abstain"][k]))}
        payload["diff"][k] = {"point": d, "ci": [d_lo, d_hi], "excludes_zero": exc}
        print(f"| {k} | {d:+.4f} [{d_lo:+.4f}, {d_hi:+.4f}] | {'YES' if exc else 'NO':3s} "
              f"| [{o['ci'][0]:+.4f}, {o['ci'][1]:+.4f}] | {'YES' if o['excludes_zero'] else 'NO'} |")

    (ROOT / "eval/cluster_bootstrap_main.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n-> eval/cluster_bootstrap_main.json")


if __name__ == "__main__":
    main()
