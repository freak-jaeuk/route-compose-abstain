"""논문에 인쇄된 수치가 raw trace 에서 그대로 재현되는지 검사한다.

gold label 을 한 번 고쳤을 때 논문·RESULTS.md 의 여러 수치가 조용히 stale 이 됐다
(u_T, repair recall, evidence marginal, Shapley CI, false-refusal 수, LOO 부호).
그 부류를 다시 놓치지 않기 위한 회귀 검사다. 새 실행 없이 eval/runs 만 읽는다.

    python eval/check_paper_numbers.py
"""
import collections
import json
import sys
from pathlib import Path

E = Path(__file__).resolve().parent
GOLD = {r["qid"]: r["answerable"] for r in map(json.loads, open(E / "qa/gold.jsonl"))}
N_ANS = sum(GOLD.values())
N_UNANS = len(GOLD) - N_ANS


def runs(name):
    """한 실행의 _run 종료 라인만."""
    return [r for r in map(json.loads, open(E / "runs" / f"{name}.jsonl"))
            if r.get("tool") == "_run"]


def answered(name):
    """(응답 수, 그중 gold-unanswerable 수)."""
    a = [r for r in runs(name) if r["verdict"] == "ANSWER"]
    return len(a), sum(1 for r in a if not GOLD[r["qid"]])


def main():
    fails = []

    def eq(label, got, want, nd=None):
        g = round(got, nd) if nd is not None else got
        if g != want:
            fails.append(f"{label}: 계산 {g!r} != 논문 {want!r}")

    eq("gold answerable", N_ANS, 43)
    eq("gold unanswerable", N_UNANS, 17)

    ans_on, harm_on = answered("proposed")
    ans_off, harm_off = answered("no_abstain")
    eq("ON answered", ans_on, 34)
    eq("ON harmful", harm_on, 4)
    eq("ON risk", harm_on / ans_on, 0.118, 3)
    eq("OFF risk", harm_off / ans_off, 0.283, 3)
    eq("risk difference", harm_on / ans_on - harm_off / ans_off, -0.166, 3)

    refused = [r for r in runs("proposed") if r["verdict"] != "ANSWER"]
    tp = [r for r in refused if not GOLD[r["qid"]]]
    eq("refused", len(refused), 26)
    eq("correct unanswerable refusals", len(tp), 13)
    eq("false refusals", len(refused) - len(tp), 13)
    eq("abstention precision", len(tp) / len(refused), 0.500, 3)
    eq("abstention recall", len(tp) / N_UNANS, 0.765, 3)

    # Table 2 의 n_T / u_T. 합이 26 / 13 이어야 Table 1 과 어긋나지 않는다.
    n_t = collections.Counter(r["abstain_reason"] for r in refused)
    u_t = collections.Counter(r["abstain_reason"] for r in tp)
    for code, n, u in [("INSUFFICIENT_EVIDENCE", 7, 2), ("LOW_ROUTER_CONFIDENCE", 8, 4),
                       ("OUT_OF_SCHEMA", 3, 3), ("PRIVACY_RESTRICTED", 6, 4),
                       ("GRAPH_PATH_NOT_FOUND", 2, 0)]:
        eq(f"n_T[{code}]", n_t[code], n)
        eq(f"u_T[{code}]", u_t[code], u)
    eq("Σn_T", sum(n_t.values()), 26)
    eq("Σu_T", sum(u_t.values()), 13)

    # log-based estimator. 표에 인쇄된 것과 같은 '켜는' 규약이어야 한다.
    eq("Est[evidence]", 4 / 34 - (4 + u_t["INSUFFICIENT_EVIDENCE"]) / (34 + n_t["INSUFFICIENT_EVIDENCE"]),
       -0.029, 3)

    shap = json.load(open(E / "shapley5.json"))
    eq("Σφ (efficiency)", sum(shap["shapley"].values()), -0.166, 3)
    # LOO 는 v(N)-v(N∖{i}) 이므로 φ 와 같은 부호 규약이다. 예전에 여기서 뒤집혔다.
    eq("ΣLOO", sum(shap["leave_one_out"].values()), 0.005, 3)
    eq("LOO[evidence]", shap["leave_one_out"]["INSUFFICIENT_EVIDENCE"], -0.007, 3)
    eq("LOO[router]", shap["leave_one_out"]["LOW_ROUTER_CONFIDENCE"], 0.012, 3)
    a_loo, _ = answered("loo_insufficient_evidence")
    eq("evidence 제거 시 응답", a_loo, 40)

    # 논문이 인용하는 두 부트스트랩. 서로 다른 재표집이므로 섞이면 안 된다.
    cl = json.load(open(E / "shapley_cluster_bootstrap.json"))["result"]
    lo, hi = cl["INSUFFICIENT_EVIDENCE"]["ci"]
    eq("cluster CI[evidence] lo", lo, -0.150, 3)
    eq("cluster CI[evidence] hi", hi, -0.035, 3)
    it = json.load(open(E / "shapley5_bootstrap.json"))["result"]
    lo, hi = it["OUT_OF_SCHEMA"]["ci"]
    eq("item CI[schema] lo", lo, -0.063, 3)
    eq("item CI[schema] hi", hi, -0.007, 3)

    causes = json.load(open(E / "refusal_causes.json"))["false_refusals"]
    accurate = sum(1 for r in causes if (r["logged_reason"], r["root_cause"]) in {
        ("INSUFFICIENT_EVIDENCE", "retrieval_miss"), ("LOW_ROUTER_CONFIDENCE", "router_no_match")})
    eq("root-cause 분모", len(causes), 13)
    eq("root-cause 일치", accurate, 9)

    # evidence gate 단독이 shipped 를 지배한다는 §5.2 의 주장.
    a_e, h_e = answered("shap5_E")
    eq("evidence 단독 응답", a_e, 40)
    eq("evidence 단독 오답", h_e, 4)

    # Table 2 의 Est. 합. 추정량이 측정치를 1.5배 과대평가한다는 §5.2 주장의 근거.
    eq("ΣEst", sum(4 / 34 - (4 + u_t[c]) / (34 + n_t[c]) for c in n_t), -0.249, 3)

    # §5.2 recapture. LOO 가 정확히 0 인 게이트 ⟺ recapture 가 1 이라는 이분법.
    base = {r["qid"]: (r["verdict"], r["abstain_reason"]) for r in runs("proposed")}
    for code, want in [("OUT_OF_SCHEMA", (3, 3)), ("PRIVACY_RESTRICTED", (6, 6)),
                       ("GRAPH_PATH_NOT_FOUND", (2, 2)), ("INSUFFICIENT_EVIDENCE", (1, 7)),
                       ("LOW_ROUTER_CONFIDENCE", (4, 8))]:
        targets = [q for q, (v, c) in base.items() if v != "ANSWER" and c == code]
        off = {r["qid"]: r["verdict"] for r in runs(f"loo_{code.lower()}")}
        eq(f"recapture[{code}]", (sum(1 for q in targets if off[q] != "ANSWER"), len(targets)), want)

    # §5.1 의 evidence-count 베이스라인. recall 은 정책과 '동일'하다 — 한때 여기서
    # "better on both counts" 라고 썼는데 recall 은 양쪽 다 13/17 이다.
    ez = {r["qid"] for r in runs("no_abstain") if abs(r["answer_confidence"] - 0.5) < 1e-9}
    ez_tp = {q for q in ez if not GOLD[q]}
    eq("evidence-zero 거절 수", len(ez), 16)
    # 13/16 = 0.8125. 논문은 관례대로 half-up 해서 0.813 을 인쇄한다 (파이썬 round 는 0.812).
    eq("evidence-zero precision", (len(ez_tp), len(ez)), (13, 16))
    if ez_tp != {r["qid"] for r in tp}:
        fails.append("evidence-zero 가 잡는 unanswerable 집합이 정책과 다르다 — §5.1 'same 13' 주장 붕괴")

    # §5.1 의 Holm 결과. AURC 는 통과하지 못하고 AUROC 는 통과한다는 것이 헤드라인 방어다.
    holm = {t["test"]: t for t in json.load(open(E / "holm.json"))["tests"]}
    for name, survives in [("AUROC difference", True), ("risk difference", True),
                           ("AUROC, cited-source count", True), ("AUROC, evidence count", True),
                           ("AURC difference", False), ("AUGRC difference", False),
                           ("AUROC, router confidence", False)]:
        eq(f"Holm[{name}]", holm[name]["survives_holm"], survives)

    if fails:
        print(f"실패 {len(fails)}건:")
        for f in fails:
            print("  -", f)
        return 1
    print("논문 수치 전부 재현됨.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
