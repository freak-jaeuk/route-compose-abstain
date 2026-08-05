"""오거절의 **근인**을 trace에서 분류한다 — 사유 코드가 말하는 것과 실제 원인은 다르다.

논문 §5.4가 "오거절은 검색 리콜 한계"라고 진단했는데, trace를 보면 절반이 검색기를
호출조차 하지 않았다(라우터가 어느 경로도 못 골라 폴백). 사유 코드는 '어느 게이트가
발화했는가'를 말하지 '왜 그 게이트에 도달했는가'를 말하지 않는다.

근인 분류:
  router_no_match   라우터가 키워드를 못 맞춰 ABSTAIN 폴백 (도구 호출 0회)
  router_wrong_path gold와 다른 경로로 보내 그 경로에서 실패
  retrieval_miss    옳은 경로로 갔으나 검색이 근거를 못 찾음
  composite_partial COMPOSITE 첫 leg 성공 후 둘째 leg에서 중단
  policy_overmatch  PII 정규식이 집계 질의에 과매칭
  zero_count        SQL이 정상 실행됐으나 결과가 0건

용례:  python eval/refusal_causes.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rca.trace import read_traces  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "eval/qa/gold.jsonl"
RUNS = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "eval/runs")
OUT = ROOT / "eval/refusal_causes.json"


def _yield(row: dict) -> int:
    """도구 한 번이 실제로 내놓은 근거 개수. 없으면 0.

    `row_count`는 의도적으로 제외한다. trace 전체에서 `agg="sum"` 호출 163건의
    row_count가 전부 정확히 1이다 — 집계 질의는 밑에 데이터가 하나도 없어도
    행 하나를 돌려주므로 row_count는 산출량 신호가 아니다. 이걸 근거 개수로
    세면 합계가 0인 질의가 '근거를 받았다'로 기록된다. SQL 경로의 빈 결과는
    아래 zero_count 분기가 따로 잡는다.

    이 제외는 판정을 바꾸는 가정이므로 여기 적어둔다 — 이 파일이 고친 버그가
    바로 '문서화되지 않은 분류기 가정'이었다.
    """
    out = row.get("output") or {}
    if not isinstance(out, dict):
        return 0
    for k in ("hits", "n", "count", "rows", "paths", "results"):
        v = out.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, list):
            return len(v)
    return 0


def classify(qid: str, rows: list, g: dict) -> str:
    """한 질의의 trace 줄들로 근인을 판정한다.

    분기 순서가 결론을 만든다. 이전 판은 COMPOSITE이기만 하면 도구를 몇 번 불렀든,
    그 도구가 무엇을 내놓았든 composite_partial로 보냈다. 그래서 근거를 0건 받고
    멈춘 질의까지 '첫 leg은 성공했다'로 기록됐고, retrieval_miss 분기는 COMPOSITE
    질의에서 영영 도달할 수 없었다 — "검색 리콜 실패 0건"이라는 결론이 데이터가
    아니라 이 순서에서 나왔다.

    이제 도구의 **산출**을 먼저 본다. 근거를 하나도 못 받았으면 leg 개수와 무관하게
    검색 실패이고, composite_partial은 '한 leg은 근거를 받았는데 다른 leg에서
    멈춘' 경우로 좁힌다.
    """
    run = next((r for r in rows if r.get("tool") == "_run"), None)
    calls = [r for r in rows if r.get("tool") not in ("_run", "route")]
    tools = [r["tool"] for r in calls]
    reason = run.get("abstain_reason") if run else None
    route = run.get("route_pred") if run else None

    if reason == "PRIVACY_RESTRICTED":
        return "policy_overmatch"
    if not calls:
        # 도구를 한 번도 안 불렀다 = 라우터 단계에서 끝났다
        return "router_no_match"
    if route and route != g["route_label"]:
        return "router_wrong_path"

    productive = [c for c in calls if _yield(c) > 0]
    if not productive:
        # 도구는 돌았는데 아무것도 못 받았다. SQL이 정상 실행돼 0행이면 데이터가
        # 없는 것이고, 검색기가 0건이면 리콜 실패다 — 둘은 다른 문제다.
        if all(c["tool"] == "query_structured_data" for c in calls):
            return "zero_count"
        return "retrieval_miss"
    if g["route_label"] == "COMPOSITE" and len(productive) < len(set(tools)):
        return "composite_partial"
    if "query_structured_data" in tools and reason == "INSUFFICIENT_EVIDENCE":
        return "zero_count"
    return "retrieval_miss"


def main() -> None:
    gold = {}
    for line in GOLD.read_text(encoding="utf-8").splitlines():
        if line.strip():
            g = json.loads(line)
            gold[g["qid"]] = g

    rows, _ = read_traces(RUNS)
    byq = defaultdict(list)
    for r in rows:
        if r.get("system") == "proposed":
            byq[r["qid"]].append(r)

    false_ref, just_ref = [], []
    for qid, rs in byq.items():
        run = next((r for r in rs if r.get("tool") == "_run"), None)
        if not run or run.get("verdict") != "ABSTAIN":
            continue
        (false_ref if gold[qid]["answerable"] else just_ref).append(
            (qid, run.get("abstain_reason"), classify(qid, rs, gold[qid])))

    print(f"거절 {len(false_ref) + len(just_ref)}건 = 정당 {len(just_ref)} + 오거절 {len(false_ref)}\n")
    print("## 오거절의 근인 (사유 코드가 말하지 않는 것)\n")
    print("| qid | 찍힌 사유 | 실제 근인 |")
    print("|---|---|---|")
    for qid, reason, cause in sorted(false_ref):
        print(f"| {qid} | `{reason}` | {cause} |")

    dist = Counter(c for _, _, c in false_ref)
    print("\n### 근인 분포")
    for k, v in dist.most_common():
        print(f"  {k}: {v}")

    # 사유 코드 ↔ 근인 교차표 — '코드가 원인을 말해주지 않는다'의 직접 증거
    cross = defaultdict(Counter)
    for _, reason, cause in false_ref:
        cross[reason][cause] += 1
    print("\n### 사유코드 → 근인 교차")
    for reason, cs in cross.items():
        print(f"  {reason}: {dict(cs)}")

    OUT.write_text(json.dumps({
        "false_refusals": [{"qid": q, "logged_reason": r, "root_cause": c}
                           for q, r, c in sorted(false_ref)],
        "justified": [{"qid": q, "logged_reason": r, "root_cause": c}
                      for q, r, c in sorted(just_ref)],
        "cause_distribution": dict(dist),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
