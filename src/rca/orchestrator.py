"""Orchestrator — 질의를 라우팅·실행·검증하고 ANSWER/CLARIFY/ABSTAIN을 결정한다.

흐름:  route → (COMPOSITE면 분해) → 경로별 도구 실행 → 근거 검증 → 답변 생성 → 정책
모든 도구 호출은 tracer.step으로 계측되고, 예외는 AbstainReason으로 매핑된다.

계획형(planned) 오케스트레이션이다. 예산 초과·근거 부족·범위 밖은 답이 아니라 거절이다.
"""

from __future__ import annotations

import re
import time

from . import llm, router
from .state import Budget, RunState
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

# 거절 트리거 전체. leave-one-trigger-out ablation은 여기서 하나씩 빼서 실행한다.
ALL_TRIGGERS = frozenset({
    "INSUFFICIENT_EVIDENCE", "PRIVACY_RESTRICTED", "OUT_OF_SCHEMA",
    "GRAPH_PATH_NOT_FOUND", "SQL_EXECUTION_FAILURE", "SOURCE_CONFLICT",
    "LOW_ROUTER_CONFIDENCE", "BUDGET_EXCEEDED",
})

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
            yrs = [sql_tool.YEAR_MAX - 2, sql_tool.YEAR_MAX]   # 데이터 최신연도 기준
        regions = [_REGION_ALIAS.get(r, r) for r in _REGIONS if r in q]
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

    # 질의가 '어떤 병'을 지목하는지 알아내는 표층 단서. 이 단서가 있는데 스키마의
    # 질병명과 하나도 안 맞으면, 질병 필터 없이 전체를 집계하는 대신 범위 밖으로 판정해야 한다.
    _DISEASE_CUE = re.compile(r"([가-힣A-Za-z0-9]{2,})\s*(감염병|감염증|바이러스|병|열|증)\b")
    # 급수 표현은 개별 질병이 아니라 분류 축이다(스키마의 grade). 미해결 엔티티가 아니다.
    _GRADE_CUE = re.compile(r"제\s*[1-4]\s*급")

    def unresolved_entity(self, q: str) -> str | None:
        """스키마에 없는 질병 엔티티를 지목한 질의면 그 표층 문자열을 반환.

        이것이 없으면 '화성인 감염병 발생 건수' 같은 질의가 disease=None으로 흘러
        전 질병 합계를 정답인 양 돌려준다(실측 결함). 엔티티 해석 실패는 검색 실패가
        아니라 스키마 범위 밖이므로 OUT_OF_SCHEMA로 보낸다.

        급수 표현('제2급감염병')은 개별 질병 지목이 아니므로 제외한다.
        """
        if self._match_disease(q) or self._GRADE_CUE.search(q):
            return None
        m = self._DISEASE_CUE.search(q)
        return m.group(0).strip() if m else None


_REGIONS = ["서울", "부산", "대구", "인천", "대전", "울산", "경기", "강원",
            "충북", "충남", "전북", "경북", "경남", "제주", "세종", "전남광주",
            "전남", "광주"]
# 통계 포털이 전남·광주를 '전남광주' 한 지역으로 제공 → 단독 언급도 그 컬럼으로 매핑
_REGION_ALIAS = {"전남": "전남광주", "광주": "전남광주"}


def _leg(st: RunState, tr: Tracer, agents: Agents, leg: str) -> str | None:
    """한 경로 실행. 근거를 st.evidence에 쌓고, 실패 시 abstain 사유를 반환(성공=None)."""
    if leg == "SQL":
        # 스키마에 없는 질병을 지목한 질의는 전체 집계로 흘려보내지 않는다.
        if agents.unresolved_entity(st.question):
            return "OUT_OF_SCHEMA"
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
              abstention: bool = True, use_llm: bool = True,
              generate: bool = True,
              abstain_on: frozenset[str] | None = None) -> RunState:
    """tr.query(qid) 컨텍스트 안에서 호출한다(step 번호가 질의마다 1부터).

    generate=False면 답변 텍스트 생성(느린 LLM 콜)을 건너뛴다. 평가에서
    route·verdict·거절 지표만 필요할 때 120회 × 20초를 피한다. 이때 confidence는
    근거 수 기반 휴리스틱으로, AURC의 연속 점수 역할을 한다.

    abstain_on: 활성화할 거절 사유 집합. None이면 `abstention` 불리언을 따른다
    (True=전부, False=없음). leave-one-trigger-out ablation은 여기에
    `ALL_TRIGGERS - {제거할 사유}` 를 넘긴다.
    """
    on = (ALL_TRIGGERS if abstention else frozenset()) if abstain_on is None else abstain_on
    # CPU 검색·리랭킹이 느려 기본 30초 deadline이 평가 중 발동했다. 예산 트리거가
    # 거절 효과 측정에 섞이지 않도록 평가에서는 넉넉히 준다(무한 루프 방지는 step 한도가 맡는다).
    st = RunState(run_id=tr.run_fields.get("run_id", "?"),
                  qid=qid, question=question, system=system,
                  budget=Budget(deadline_ms=180_000))
    t0 = time.perf_counter()

    def finish(**kw) -> RunState:
        for k, v in kw.items():
            setattr(st, k, v)
        return st

    # 0) PII 게이트 — 라우팅보다 앞에 둔다. 개인 식별 질의가 라우터에서 우연히
    #    ABSTAIN 처리되면 사유가 PRIVACY_RESTRICTED 대신 다른 코드로 찍혀 측정이 왜곡된다.
    #    abstention=False 는 실험 통제 조건이며 정책 거절까지 끈다(배포 설정이 아니다).
    try:
        sql_tool.guard_pii(question)
    except sql_tool.SqlError as e:
        if e.reason in on:
            return finish(verdict="ABSTAIN", abstain_reason=e.reason, answer_confidence=0.02)

    # 1) 라우팅
    with tr.step("route", {"q": question}, kind="llm") as rec:
        r = router.route(question, use_llm=use_llm)
        rec["output"] = {"route": r["route"], "conf": r["conf"], "via": r["via"]}
        rec["tokens_in"], rec["tokens_out"] = r["tokens_in"], r["tokens_out"]
    st.route_pred, st.route_conf = r["route"], r["conf"]

    if st.route_pred == "ABSTAIN":
        # 라우터가 어느 경로도 못 고른 것은 '검색해봤으나 근거가 없음'이 아니다.
        # 두 상황을 같은 코드로 찍으면 오류 분석에서 검색 리콜 문제로 오독된다.
        if "LOW_ROUTER_CONFIDENCE" in on:
            return finish(verdict="ABSTAIN", abstain_reason="LOW_ROUTER_CONFIDENCE",
                          answer_confidence=r["conf"])
        st.route_pred = "DOCUMENT"   # 트리거 꺼짐: 근거가 애매해도 문서 경로로 답을 강제한다
    if "LOW_ROUTER_CONFIDENCE" in on and st.route_conf < threshold:
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
            if "BUDGET_EXCEEDED" in on:
                return finish(verdict="ABSTAIN", abstain_reason="BUDGET_EXCEEDED",
                              answer_confidence=0.1)
            break   # 트리거 꺼짐: 예산이 끝나면 실행만 멈추고, 모은 근거로 답한다
        reason = _leg(st, tr, agents, leg)
        if reason and reason in on:
            return finish(verdict="ABSTAIN", abstain_reason=reason, answer_confidence=0.1)

    # 4) 근거 게이트 + 답변
    if not st.evidence and "INSUFFICIENT_EVIDENCE" in on:
        return finish(verdict="ABSTAIN", abstain_reason="INSUFFICIENT_EVIDENCE",
                      answer_confidence=0.1)
    if not generate:
        # 답변 텍스트 없이 verdict·confidence만. confidence = 근거 수 기반(AURC용 연속점수).
        conf = min(0.9, 0.5 + 0.1 * len(st.evidence))
        return finish(verdict="ANSWER", answer_confidence=conf)
    answer, conf = _generate(st, tr)
    return finish(verdict="ANSWER", answer=answer, answer_confidence=conf)
