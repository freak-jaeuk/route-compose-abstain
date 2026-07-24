"""Orchestrator — 질의를 라우팅·실행·검증하고 ANSWER/CLARIFY/ABSTAIN을 결정한다.

흐름:  route → (COMPOSITE면 분해) → 경로별 도구 실행 → 근거 검증 → 답변 생성 → 정책
모든 도구 호출은 tracer.step으로 계측되고, 예외는 AbstainReason으로 매핑된다.

계획형(planned) 오케스트레이션이다. 예산 초과·근거 부족·범위 밖은 답이 아니라 거절이다.
"""

from __future__ import annotations

import re
import time

from . import llm, router
from .state import RunState
from .tools import docs as docs_tool
from .tools import graph as graph_tool
from .tools import sql as sql_tool
from .trace import Tracer

# 예외/상황 → 거절 사유 코드
EXC_REASON = {
    "OUT_OF_SCHEMA": "OUT_OF_SCHEMA",
    "PRIVACY_RESTRICTED": "PRIVACY_RESTRICTED",
    "SQL_EXECUTION_FAILURE": "SQL_EXECUTION_FAILURE",
}
COMPOSITE_PLAN = {"COMPOSITE": ["DOCUMENT", "SQL"]}   # v0.1: 서술+통계

_RERANK_MIN = 0.15     # 문서 rerank 점수 하한 (근거 게이트)


class Agents:
    """도구 3종을 한 번 로드해 재사용. 모델·DB 로드가 비싸므로 프로세스당 1회."""

    def __init__(self):
        self.docs = docs_tool.DocAgent()
        self.graph = graph_tool.GraphAgent()

    # ── NL → QuerySpec (SQL 경로) ────────────────────────────
    def nl_to_queryspec(self, q: str) -> tuple[sql_tool.QuerySpec, int, int]:
        """질문에서 질병·연도·지역·집계를 뽑아 QuerySpec으로. LLM 실패 시 규칙 폴백."""
        ti = to = 0
        disease = self._match_disease(q)
        yrs = [int(y) for y in re.findall(r"(20\d\d)", q)]
        if "최근 3년" in q:
            yrs = [2023, 2025]
        regions = [r for r in _REGIONS if r in q]
        agg = ("by_region" if re.search(r"시도별|지역별", q)
               else "by_year" if re.search(r"추이|연도별|년도별", q)
               else "sum")
        spec = sql_tool.QuerySpec(
            disease=disease,
            year_from=min(yrs) if yrs else None,
            year_to=max(yrs) if yrs else None,
            regions=regions, agg=agg)
        return spec, ti, to

    def _match_disease(self, q: str) -> str | None:
        hits = [d for d in self.graph.diseases if d and d in q]
        return max(hits, key=len) if hits else None


_REGIONS = ["서울", "부산", "대구", "인천", "대전", "울산", "경기", "강원",
            "충북", "충남", "전북", "경북", "경남", "제주", "세종", "전남광주"]


def _leg(st: RunState, tr: Tracer, agents: Agents, leg: str) -> str | None:
    """한 경로 실행. 근거를 st.evidence에 쌓고, 실패 시 abstain 사유를 반환(성공=None)."""
    if leg == "SQL":
        try:
            spec, ti, to = agents.nl_to_queryspec(st.question)
            with tr.step("query_structured_data", spec.model_dump(exclude_none=True),
                         kind="tool", route_pred=st.route_pred) as rec:
                out = sql_tool.answer(st.question, spec)
                rec["output"] = {"row_count": out["row_count"]}
                rec["tokens_in"], rec["tokens_out"] = ti, to
            if not out["rows"] or all(not r.get("cases") for r in out["rows"]):
                return "INSUFFICIENT_EVIDENCE"
            st.evidence.append({"source_type": "sql", "source_id": out["source_id"],
                                "text": str(out["rows"][:20]), "sql": out["sql"]})
            st.cited.append(out["source_id"])
            return None
        except sql_tool.SqlError as e:
            return EXC_REASON.get(e.reason, "INSUFFICIENT_EVIDENCE")

    if leg == "DOCUMENT":
        with tr.step("retrieve_documents", {"query": st.question, "k": 5},
                     kind="tool", route_pred=st.route_pred) as rec:
            hits = agents.docs.search(st.question, k=5, min_score=_RERANK_MIN)
            rec["output"] = {"hits": len(hits)}
        if not hits:
            return "INSUFFICIENT_EVIDENCE"
        for h in hits[:3]:
            st.evidence.append({"source_type": "document", "source_id": h["source_id"],
                                "text": h["text"], "article_no": h["article_no"]})
        st.cited += [h["source_id"] for h in hits[:3]]
        return None

    if leg == "GRAPH":
        with tr.step("query_knowledge_graph", {"query": st.question}, kind="tool",
                     route_pred=st.route_pred) as rec:
            res = agents.graph.query(st.question)
            rec["output"] = {"template": res["template"], "paths": len(res["paths"])}
        if not res["paths"]:
            return "GRAPH_PATH_NOT_FOUND"
        st.evidence.append({"source_type": "graph", "source_id": ",".join(res["source_ids"]),
                            "text": str(res["paths"])})
        st.cited += res["source_ids"]
        return None
    return "INSUFFICIENT_EVIDENCE"


def _generate(st: RunState, tr: Tracer) -> tuple[str, float]:
    """근거로 답변 생성 + 자기 신뢰도. LLM 없으면 근거를 요약 나열."""
    ev = "\n".join(f"[{e['source_type']}] {e.get('article_no','') or e['source_id']}: {e['text'][:300]}"
                   for e in st.evidence)
    if not llm.ping():
        return f"근거:\n{ev[:600]}", 0.5
    msgs = [
        {"role": "system", "content":
         "너는 한국 감염병 공공데이터 도우미다. 주어진 근거만으로 한국어로 간결히 답하라. "
         "근거에 없는 내용은 지어내지 말고, 부족하면 그렇다고 말하라. "
         "이 답변은 의료·법률 자문이 아니다. "
         'JSON만 출력: {"answer":"...","supported":true/false,"confidence":0~1}'},
        {"role": "user", "content": f"질문: {st.question}\n\n근거:\n{ev}"},
    ]
    with tr.step("generate_answer", {"evidence_n": len(st.evidence)}, kind="llm") as rec:
        res = llm.chat_json(msgs, max_tokens=800)
        rec["tokens_in"], rec["tokens_out"] = res["tokens_in"], res["tokens_out"]
    j = res["json"]
    return j.get("answer", ""), (float(j.get("confidence", 0.5)) if j.get("supported", True) else 0.1)


def run_query(question: str, qid: str, tr: Tracer, agents: Agents,
              system: str = "proposed", threshold: float = 0.5,
              abstention: bool = True, use_llm: bool = True) -> RunState:
    """tr.query(qid) 컨텍스트 안에서 호출한다(step 번호가 질의마다 1부터)."""
    st = RunState(run_id=tr.run_fields.get("run_id", "?"),
                  qid=qid, question=question, system=system)
    t0 = time.perf_counter()

    def finish(**kw) -> RunState:
        for k, v in kw.items():
            setattr(st, k, v)
        return st

    # 0) PII 게이트 — 라우팅보다 앞에 둔다. 개인 식별 질의가 라우터에서 우연히
    #    ABSTAIN 처리되면 사유가 PRIVACY_RESTRICTED 대신 다른 코드로 찍혀 측정이 왜곡된다.
    try:
        sql_tool.guard_pii(question)
    except sql_tool.SqlError as e:
        return finish(verdict="ABSTAIN", abstain_reason=e.reason, answer_confidence=0.02)

    # 1) 라우팅
    with tr.step("route", {"q": question}, kind="llm") as rec:
        r = router.route(question, use_llm=use_llm)
        rec["output"] = {"route": r["route"], "conf": r["conf"], "via": r["via"]}
        rec["tokens_in"], rec["tokens_out"] = r["tokens_in"], r["tokens_out"]
    st.route_pred, st.route_conf = r["route"], r["conf"]

    if st.route_pred == "ABSTAIN":
        return finish(verdict="ABSTAIN", abstain_reason="INSUFFICIENT_EVIDENCE",
                      answer_confidence=r["conf"])
    if abstention and st.route_conf < threshold:
        return finish(verdict="ABSTAIN", abstain_reason="LOW_ROUTER_CONFIDENCE",
                      answer_confidence=st.route_conf)

    # 2) 계획
    st.plan = COMPOSITE_PLAN.get(st.route_pred, [st.route_pred])

    # 3) 실행 (예산 관리 + 실패 매핑)
    for leg in st.plan:
        tool = {"SQL": "query_structured_data", "DOCUMENT": "retrieve_documents",
                "GRAPH": "query_knowledge_graph"}[leg]
        elapsed = int((time.perf_counter() - t0) * 1000)
        if st.budget.exhausted(st.steps, elapsed, next_tool=tool):
            return finish(verdict="ABSTAIN", abstain_reason="BUDGET_EXCEEDED",
                          answer_confidence=0.1)
        reason = _leg(st, tr, agents, leg)
        if reason and abstention:
            return finish(verdict="ABSTAIN", abstain_reason=reason, answer_confidence=0.1)

    # 4) 근거 게이트 + 답변
    if not st.evidence and abstention:
        return finish(verdict="ABSTAIN", abstain_reason="INSUFFICIENT_EVIDENCE",
                      answer_confidence=0.1)
    answer, conf = _generate(st, tr)
    return finish(verdict="ANSWER", answer=answer, answer_confidence=conf)
