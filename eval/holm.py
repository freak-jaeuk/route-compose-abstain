"""§5.1 의 7개 차이 검정에 Holm-Bonferroni 를 실제로 적용한다.

논문은 "AURC 는 이 절의 7개 검정에 대한 보정을 통과하지 못한다"고만 적고 보정을 돌리지
않았다. 그러면 리뷰어는 헤드라인인 AUROC 도 못 통과하는 것 아니냐고 묻는다. 돌려서 답한다.

각 검정의 ASL(achieved significance level)은 페어드 항목 부트스트랩 분포에서
    ASL = 2 * min( P(d* <= 0), P(d* >= 0) )
로 잡는다. 지표 정의는 bootstrap_ci / augrc / alt_signal_auroc 에서 그대로 가져온다 —
여기서 다시 구현하면 정의가 갈라져 비교가 무의미해진다.

    python eval/holm.py [--B 10000] [--seed 0]
"""
import argparse
import json
import random
import sys
from pathlib import Path

E = Path(__file__).resolve().parent
sys.path.insert(0, str(E))

from bootstrap_ci import load, metrics                      # noqa: E402
from alt_signal_auroc import auroc as alt_auroc, _yield     # noqa: E402
from augrc import augrc_of                                  # noqa: E402

ALPHA = 0.05


def alt_score(by, qid, which):
    rs = by[qid]
    run = next(r for r in rs if r.get("tool") == "_run")
    if which == "n_cited":
        return len(run.get("cited") or [])
    if which == "route_conf":
        return run.get("route_conf") or 0.0
    return sum(_yield(c) for c in rs if c.get("tool") not in ("_run", "route"))


def asl(draws, observed):
    """양측 achieved significance level. 관측 부호와 무관하게 0 을 기준으로 잰다."""
    n = len(draws)
    le = sum(1 for d in draws if d <= 0)
    ge = sum(1 for d in draws if d >= 0)
    p = 2.0 * min(le, ge) / n
    return min(1.0, max(p, 1.0 / n)), observed          # 0 은 못 준다; 하한은 1/B


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--B", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    random.seed(args.seed)

    gold, by = load()
    qids = sorted(set(by["proposed"]) & set(by["no_abstain"]))
    assert len(qids) == 60, len(qids)

    # 원자료를 qid -> 그 질의의 모든 trace 줄 로 다시 묶는다 (alt signal 용).
    raw = {}
    for name in ("proposed", "no_abstain"):
        d = {}
        for r in map(json.loads, open(E / "runs" / f"{name}.jsonl")):
            d.setdefault(r["qid"], []).append(r)
        raw[name] = d

    def d_metric(key, sample):
        return metrics(by["proposed"], sample, gold)[key] - metrics(by["no_abstain"], sample, gold)[key]

    def d_augrc(sample):
        return augrc_of("proposed", sample) - augrc_of("no_abstain", sample)

    def d_alt(which, sample):
        a = alt_auroc([(alt_score(raw["proposed"], q, which), gold[q]["answerable"]) for q in sample])
        b = alt_auroc([(alt_score(raw["no_abstain"], q, which), gold[q]["answerable"]) for q in sample])
        return a - b

    TESTS = [
        ("risk difference",            lambda s: d_metric("risk", s)),
        ("AURC difference",            lambda s: d_metric("aurc", s)),
        ("AUROC difference",           lambda s: d_metric("auroc", s)),
        ("AUGRC difference",           d_augrc),
        ("AUROC, cited-source count",  lambda s: d_alt("n_cited", s)),
        ("AUROC, evidence count",      lambda s: d_alt("tool_yield", s)),
        ("AUROC, router confidence",   lambda s: d_alt("route_conf", s)),
    ]

    samples = [[random.choice(qids) for _ in range(60)] for _ in range(args.B)]

    rows = []
    for name, fn in TESTS:
        obs = fn(qids)
        draws = [fn(s) for s in samples]
        p, _ = asl(draws, obs)
        rows.append({"test": name, "observed": obs, "asl": p})

    # Holm-Bonferroni: p 오름차순, i 번째 임계값 alpha/(m-i), 처음 실패하는 곳에서 전부 중단.
    m = len(rows)
    order = sorted(range(m), key=lambda i: rows[i]["asl"])
    still = True
    for rank, i in enumerate(order):
        thr = ALPHA / (m - rank)
        rows[i]["holm_threshold"] = thr
        rows[i]["survives_holm"] = still and rows[i]["asl"] <= thr
        if not rows[i]["survives_holm"]:
            still = False

    print(f"페어드 항목 부트스트랩 B={args.B}, seed={args.seed}, Holm m={m}, alpha={ALPHA}\n")
    print(f"{'test':28s} {'observed':>10s} {'ASL':>9s} {'Holm 임계':>10s}  통과")
    for i in order:
        r = rows[i]
        print(f"{r['test']:28s} {r['observed']:+10.4f} {r['asl']:9.4f} {r['holm_threshold']:10.5f}  "
              f"{'YES' if r['survives_holm'] else 'no'}")

    out = E / "holm.json"
    out.write_text(json.dumps({"B": args.B, "seed": args.seed, "alpha": ALPHA,
                               "m": m, "resample": "paired item", "tests": rows},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {out.name}")


if __name__ == "__main__":
    main()
