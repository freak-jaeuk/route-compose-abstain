"""배선 점검용 스모크 실행 — 실제 검색/LLM 없이 파이프라인 한 바퀴를 돈다.

목적은 단 하나: **ARCHITECTURE_v1.md §8.1 의 "trace → 지표" 유도표가 실제로 성립하는가.**
도구는 전부 결정론적 stub 이며, 여기서 나오는 수치는 연구 결과가 아니다.
실제 도구가 붙으면 이 파일의 stub 함수만 교체된다 — 오케스트레이터·정책·계측은 그대로다.

실행:  python -m rca.demo          (PYTHONPATH=src)
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from rca.state import Budget, RunState, ToolCall
from rca.trace import Tracer

ROOT = Path(__file__).resolve().parents[2]
GOLD = ROOT / "eval/qa/demo_gold.jsonl"
RUNS = ROOT / "eval/runs"

# ---------------------------------------------------------------- stub 데이터 계층
# ponytail: 실제 SQLite/Qdrant/Kùzu 대신 상수. 스키마 경계와 실패 모드만 진짜처럼 흉내낸다.
SCHEMA = {"claim_stats": ["region", "year", "amount"]}
COVERAGE_YEARS = range(2019, 2025)
GRANULARITY = {"지역", "시도"}  # 읍면동 단위는 존재하지 않음
DOCS = {
    "면책": ("doc_001", "실손의료보험 약관 제5조(면책사항)", 0.81),
    "보장 범위": ("doc_002", "자동차보험 표준약관 대인배상 I 보장 범위", 0.78),
    "도입 취지": ("doc_003", "실손의료보험 제도 개요 및 도입 취지", 0.74),
}
GRAPH_PATHS = {"특약": ["Rider-[:MODIFIES]->Coverage-[:GOVERNED_BY]->Law:Article"]}
PII = re.compile(r"(홍길동|김철수|가입자\s*\S+의)")


def tool_inspect_schema(_: dict) -> dict:
    return {"tables": SCHEMA, "coverage_years": [min(COVERAGE_YEARS), max(COVERAGE_YEARS)]}


def tool_query_structured_data(spec: dict) -> dict:
    if spec.get("granularity") not in GRANULARITY:
        raise LookupError(f"OUT_OF_SCHEMA: granularity={spec.get('granularity')}")
    if not set(spec.get("years", [])) <= set(COVERAGE_YEARS):
        raise LookupError(f"OUT_OF_SCHEMA: years={spec.get('years')}")
    return {"sql": "SELECT region, SUM(amount) FROM claim_stats WHERE year BETWEEN ? AND ? GROUP BY region",
            "row_count": 17, "truncated": False, "source_id": "claim_stats"}


def tool_retrieve_documents(q: dict) -> dict:
    hits = [v for k, v in DOCS.items() if k in q["query"]]
    return {"chunks": [{"source_id": i, "text": t, "score": s} for i, t, s in hits]}


def tool_query_knowledge_graph(q: dict) -> dict:
    paths = [p for k, v in GRAPH_PATHS.items() if k in q["query"] for p in v]
    return {"paths": paths, "template_id": "T3" if paths else None}


# ---------------------------------------------------------------- 라우터 (규칙 변형)
KEYWORDS = {
    "SQL": ["합계", "추이", "지급액", "건수", "통계"],
    "DOCUMENT": ["면책", "취지", "보장 범위", "무엇", "설명"],
    "GRAPH": ["특약", "법령", "조항", "근거"],
}


def route(question: str) -> tuple[str, float]:
    score = {r: sum(k in question for k in ks) for r, ks in KEYWORDS.items()}
    hit = {r: s for r, s in score.items() if s > 0}
    if not hit:
        return "ABSTAIN", 0.20
    if len(hit) >= 2 and score.get("SQL") and score.get("DOCUMENT"):
        return "COMPOSITE", 0.70
    top = max(hit, key=hit.get)
    return top, min(0.55 + 0.15 * hit[top], 0.95)


def parse_spec(question: str) -> dict:
    years = [int(y) for y in re.findall(r"(20\d\d)", question)]
    if "최근 3년" in question:
        years = [2021, 2023]
    return {
        "granularity": "읍면동" if "읍면동" in question else "지역",
        "years": list(range(min(years), max(years) + 1)) if years else [2023],
    }


# ---------------------------------------------------------------- 오케스트레이션
LEGS = {
    "SQL": ("query_structured_data", parse_spec, tool_query_structured_data, (320, 60)),
    "DOCUMENT": ("retrieve_documents", lambda q: {"query": q, "k": 5},
                 tool_retrieve_documents, (480, 90)),
    "GRAPH": ("query_knowledge_graph", lambda q: {"query": q, "max_hops": 2},
              tool_query_knowledge_graph, (260, 55)),
}


def call_tool(tr: Tracer, st: RunState, tool: str, payload: dict, fn, tokens: tuple[int, int]):
    """도구 1회 호출. trace 기록과 `RunState.steps` 반영을 한 곳에서 한다.

    steps 를 채우지 않으면 `Budget.exhausted` 가 항상 빈 목록을 보게 되어
    예산 한도가 영원히 발동하지 않는다 (스모크가 거짓 통과하는 대표 경로).
    """
    call = ToolCall(step=len(st.steps) + 1, tool=tool, input=payload)
    try:
        with tr.step(tool, payload, route_pred=st.route_pred, route_conf=st.route_conf) as rec:
            rec["output"] = fn(payload)
            rec["tokens_in"], rec["tokens_out"] = tokens
            call.output, call.tokens_in, call.tokens_out = rec["output"], *tokens
        return call.output
    except LookupError:
        call.ok = False
        raise
    finally:
        st.steps.append(call)


def collect_cited(leg: str, out: dict, abstention: bool) -> list[str]:
    """도구는 성공했지만 근거가 비어 있는 경우를 검증 단계에서 걸러낸다.

    여기서 나는 예외는 traced 블록 **밖**이라 도구 줄의 `ok` 는 True 로 남는다.
    의도된 것이다 — `ok` 는 "도구 실행 성공" 이지 "근거 충분" 이 아니며,
    빈 검색 결과를 도구 실패로 찍으면 §8.1 의 invalid query rate 가 오염된다.
    거절 사실은 `_run` 줄의 `abstain_reason` 에 남는다.
    """
    if leg == "SQL":
        return [out["source_id"]]
    if leg == "DOCUMENT":
        if not out["chunks"] and abstention:
            raise LookupError("INSUFFICIENT_EVIDENCE: no chunk above threshold")
        return [c["source_id"] for c in out["chunks"][:2]]
    if not out["paths"]:
        raise LookupError("GRAPH_PATH_NOT_FOUND")
    return out["paths"]


def run_one(cfg: dict, gold: dict, tr: Tracer) -> dict:
    st = RunState(run_id=tr.run_fields["run_id"], qid=gold["qid"], question=gold["question"],
                  system=cfg["name"], budget=Budget())
    t0 = time.perf_counter()
    elapsed = lambda: int((time.perf_counter() - t0) * 1000)  # noqa: E731

    if PII.search(st.question) and cfg["abstention"]:
        # 라우팅 이전 단계의 차단이지만 시스템이 내린 경로 판정은 ABSTAIN 이다.
        # 비워두면 분석 쪽에서 결측을 ABSTAIN 으로 메워 라우터에 없는 공을 준다.
        st.route_pred, st.route_conf = "ABSTAIN", 0.0
        st.verdict, st.abstain_reason, st.answer_confidence = "ABSTAIN", "PRIVACY_RESTRICTED", 0.02
        return _finish(st)

    if cfg["router"] == "fixed":
        st.route_pred, st.route_conf = cfg["route"], 1.0
    else:
        st.route_pred, st.route_conf = route(st.question)

    if st.route_pred == "ABSTAIN" or (cfg["abstention"] and st.route_conf < cfg["threshold"]):
        if cfg["abstention"]:
            st.verdict, st.abstain_reason = "ABSTAIN", "LOW_ROUTER_CONFIDENCE"
            st.answer_confidence = st.route_conf
            return _finish(st)
        st.route_pred = "DOCUMENT"  # 거절 없는 조건은 무조건 답을 낸다

    plan = {"SQL": ["SQL"], "DOCUMENT": ["DOCUMENT"], "GRAPH": ["GRAPH"],
            "COMPOSITE": ["DOCUMENT", "SQL"]}[st.route_pred]
    st.plan = plan

    for leg in plan:
        tool, build, fn, tokens = LEGS[leg]
        if st.budget.exhausted(st.steps, elapsed(), next_tool=tool):
            st.verdict, st.abstain_reason, st.answer_confidence = "ABSTAIN", "BUDGET_EXCEEDED", 0.10
            return _finish(st)
        try:
            out = call_tool(tr, st, tool, build(st.question), fn, tokens)
            st.cited += collect_cited(leg, out, cfg["abstention"])
        except LookupError as e:
            reason = str(e).split(":")[0]
            if cfg["abstention"]:
                st.verdict, st.abstain_reason = "ABSTAIN", reason
                st.answer_confidence = 0.08
                return _finish(st)
            st.answer_confidence = 0.30  # 거절 없는 조건은 근거 없이도 답한다

    if st.verdict is None:
        st.verdict = "ANSWER"
        st.answer_confidence = round(min(0.95, 0.45 + 0.12 * len(st.cited)), 3)
    return _finish(st)


def _finish(st: RunState) -> dict:
    return {"verdict": st.verdict, "abstain_reason": st.abstain_reason,
            "answer_confidence": st.answer_confidence, "route_pred": st.route_pred,
            "cited": st.cited}


SYSTEMS = [
    {"name": "proposed", "router": "rules", "threshold": 0.50, "abstention": True},
    {"name": "doc_only", "router": "fixed", "route": "DOCUMENT", "threshold": 0.0, "abstention": False},
]


def main() -> None:
    gold = [json.loads(l) for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip()]
    for cfg in SYSTEMS:
        out = RUNS / f"{cfg['name']}.jsonl"
        out.unlink(missing_ok=True)
        tr = Tracer(out, run_id="smoke-0001", system=cfg["name"],
                    backbone="stub@2026-07", temperature=0.0, seed=0)
        for g in gold:
            with tr.query(g["qid"]) as summary:
                summary.update(run_one(cfg, g, tr))
        print(f"{cfg['name']:9s} -> {out.relative_to(ROOT)}")
    print(f"\n{len(gold)}문항 × {len(SYSTEMS)}조건 실행 완료. 지표: python eval/analyze.py")


if __name__ == "__main__":
    main()
