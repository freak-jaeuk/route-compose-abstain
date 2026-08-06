"""두 가지 항목 제외 민감도.

(1) gold 를 정정한 단 하나의 항목(g046)을 빼면 결론이 바뀌는가.
    리뷰어의 자연스러운 의심: "trace 를 본 뒤 라벨을 고쳤는데, 그 정정이 결과를 유리하게 만든 건 아닌가."
(2) policy 로 금지된 privacy 질의를 빼면 결론이 바뀌는가.
    "답을 모르는 것"과 "알아도 답하면 안 되는 것"은 다른 개념이고, unanswerable 이 둘을 합치고 있다.

둘 다 사람이 필요 없다. 기존 trace 만 다시 센다. 지표 정의는 bootstrap_ci 에서 가져온다.

    python eval/sensitivity.py
"""
import json
import sys
from pathlib import Path

E = Path(__file__).resolve().parent
sys.path.insert(0, str(E))

from bootstrap_ci import load, metrics  # noqa: E402

CORRECTED = {"g046"}          # trace 재검토 후 answerable -> unanswerable 로 정정한 항목


def privacy_items(by, gold):
    """privacy screen 이 거절한 gold-unanswerable 항목 = policy 로 금지된 질의."""
    out = set()
    for qid, r in by["proposed"].items():
        if r.get("verdict") != "ANSWER" and r.get("abstain_reason") == "PRIVACY_RESTRICTED" \
                and not gold[qid]["answerable"]:
            out.add(qid)
    return out


def evidence_zero(by, qids, gold):
    """OFF 조건에서 |E|=0 인 것을 거절로 본 베이스라인 (§5.1)."""
    ez = {q for q in qids if abs((by["no_abstain"][q].get("answer_confidence") or 0.0) - 0.5) < 1e-9}
    tp = {q for q in ez if not gold[q]["answerable"]}
    nu = sum(1 for q in qids if not gold[q]["answerable"])
    return len(ez), len(tp), (len(tp) / len(ez) if ez else 0.0), (len(tp) / nu if nu else 0.0)


def report(name, by, gold, qids):
    m_on = metrics(by["proposed"], qids, gold)
    m_off = metrics(by["no_abstain"], qids, gold)
    na = sum(1 for q in qids if gold[q]["answerable"])
    n, tp, prec, rec = evidence_zero(by, qids, gold)
    print(f"\n=== {name}  (n={len(qids)}, answerable={na}, unanswerable={len(qids)-na}) ===")
    print(f"  risk        ON {m_on['risk']:.3f}   OFF {m_off['risk']:.3f}   diff {m_on['risk']-m_off['risk']:+.3f}")
    print(f"  AUROC       ON {m_on['auroc']:.3f}   OFF {m_off['auroc']:.3f}   diff {m_on['auroc']-m_off['auroc']:+.3f}")
    print(f"  AURC        ON {m_on['aurc']:.3f}   OFF {m_off['aurc']:.3f}   diff {m_on['aurc']-m_off['aurc']:+.3f}")
    print(f"  coverage    ON {m_on['coverage']:.3f}")
    print(f"  abst prec   {m_on['abst_precision']:.3f}   recall {m_on['abst_recall']:.3f}")
    print(f"  evidence-zero baseline: n={n} tp={tp} precision={prec:.3f} recall={rec:.3f}")
    return m_on, m_off


def main() -> None:
    gold, by = load()
    qids = sorted(set(by["proposed"]) & set(by["no_abstain"]))
    full_on, full_off = report("전체 60", by, gold, qids)

    drop_corrected = [q for q in qids if q not in CORRECTED]
    c_on, c_off = report("정정 항목(g046) 제외", by, gold, drop_corrected)

    priv = privacy_items(by, gold)
    drop_priv = [q for q in qids if q not in priv]
    p_on, p_off = report(f"policy 금지 privacy 항목 제외 ({sorted(priv)})", by, gold, drop_priv)

    print("\n=== 결론 방향 보존 여부 ===")
    for name, on, off in [("정정 항목 제외", c_on, c_off), ("privacy 제외", p_on, p_off)]:
        ok_risk = (on["risk"] - off["risk"]) < 0
        ok_auroc = (on["auroc"] - off["auroc"]) < 0
        ok_aurc = (on["aurc"] - off["aurc"]) > 0
        print(f"  {name:16s} risk↓ {ok_risk}  AUROC↓ {ok_auroc}  AURC↑ {ok_aurc}  "
              f"-> {'모든 방향 보존' if ok_risk and ok_auroc and ok_aurc else '방향 변화 있음'}")

    out = E / "sensitivity.json"
    out.write_text(json.dumps({
        "full": {"on": full_on, "off": full_off, "n": len(qids)},
        "drop_corrected": {"on": c_on, "off": c_off, "n": len(drop_corrected), "dropped": sorted(CORRECTED)},
        "drop_privacy": {"on": p_on, "off": p_off, "n": len(drop_priv), "dropped": sorted(priv)},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {out.name}")


if __name__ == "__main__":
    main()
