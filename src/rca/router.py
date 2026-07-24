"""Query Router — 질의를 5경로로 분류. LLM few-shot + 규칙 폴백.

경로: SQL(통계 집계) · DOCUMENT(조문 서술) · GRAPH(관계·위계) · COMPOSITE(둘 이상) · ABSTAIN(범위 밖)

LLM이 있으면 few-shot으로, 없거나 실패하면 규칙으로 폴백한다(오프라인 재현성).
반환 (route, confidence). confidence가 임계값 미만이면 오케스트레이터가 거절한다.
"""

from __future__ import annotations

import re

from . import llm

ROUTES = ["SQL", "DOCUMENT", "GRAPH", "COMPOSITE", "ABSTAIN"]

_SYSTEM = """너는 한국 감염병 공공데이터 질의응답의 라우터다. 데이터 소스는 셋이다.
- SQL: 질병관리청 발생통계 (연도 2015~2026 × 시도 × 감염병 × 발생건수). 수치·집계·추이·시도별 비교.
- DOCUMENT: 감염병예방법·시행령 조문 전문. 정의·신고기한·절차·요건 등 서술형.
- GRAPH: 조문 간 위임관계·급수분류 위계. "무엇이 무엇에 위임", "몇 급".
분류 규칙:
- 발생 건수·추이·통계·시도별 수치 → SQL
- 조문 내용·정의·기한·절차 설명 → DOCUMENT
- 위임/하위규정/급수분류 등 관계 → GRAPH
- 서술 설명 + 통계가 모두 필요 → COMPOSITE
- 셋 중 어디에도 근거가 없음 → ABSTAIN
JSON만 출력: {"route":"<경로>","conf":<0~1>}"""

_FEWSHOT = [
    ("2023년 시도별 수두 발생 건수", '{"route":"SQL","conf":0.95}'),
    ("제2급감염병의 신고 기한은?", '{"route":"DOCUMENT","conf":0.9}'),
    ("제42조가 시행령에 위임한 규정은?", '{"route":"GRAPH","conf":0.9}'),
    ("수두의 신고 기준을 설명하고 최근 3년 발생 추이를 보여줘", '{"route":"COMPOSITE","conf":0.85}'),
    ("코로나 백신 예약 방법 알려줘", '{"route":"ABSTAIN","conf":0.8}'),
]

# 규칙 폴백 키워드
_KW = {
    "SQL": ["발생", "건수", "추이", "통계", "시도별", "지역별", "몇 명", "몇건", "명이"],
    "DOCUMENT": ["정의", "신고 기한", "신고기한", "절차", "요건", "무엇인가", "설명", "며칠", "기준"],
    "GRAPH": ["위임", "시행령", "하위", "세부", "급", "분류", "관계"],
}


def rule_route(q: str) -> tuple[str, float]:
    score = {r: sum(k in q for k in ks) for r, ks in _KW.items()}
    hit = {r: s for r, s in score.items() if s > 0}
    if not hit:
        return "ABSTAIN", 0.25
    if score["SQL"] and score["DOCUMENT"]:
        return "COMPOSITE", 0.6
    top = max(hit, key=hit.get)
    return top, min(0.55 + 0.15 * hit[top], 0.9)


def llm_route(q: str) -> tuple[str, float]:
    msgs = [{"role": "system", "content": _SYSTEM}]
    for u, a in _FEWSHOT:
        msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
    msgs.append({"role": "user", "content": q})
    res = llm.chat_json(msgs, max_tokens=384)
    j = res["json"]
    route = j.get("route", "ABSTAIN")
    if route not in ROUTES:
        route = "ABSTAIN"
    conf = float(j.get("conf", 0.5))
    return route, max(0.0, min(1.0, conf)), res["tokens_in"], res["tokens_out"]


def route(q: str, use_llm: bool = True) -> dict:
    """반환 {route, conf, via, tokens_in, tokens_out}. LLM 실패 시 규칙 폴백."""
    if use_llm and llm.ping():
        try:
            r, c, ti, to = llm_route(q)
            return {"route": r, "conf": c, "via": "llm", "tokens_in": ti, "tokens_out": to}
        except llm.LLMError:
            pass  # 폴백
    r, c = rule_route(q)
    return {"route": r, "conf": c, "via": "rules", "tokens_in": 0, "tokens_out": 0}


if __name__ == "__main__":
    cases = {
        "2023년 시도별 수두 발생 건수": "SQL",
        "제2급감염병 신고 기한은 며칠인가": "DOCUMENT",
        "제8조의2가 시행령에 위임한 세부 사항": "GRAPH",
        "장티푸스 신고 기준을 설명하고 최근 3년 발생 추이": "COMPOSITE",
    }
    # 규칙 라우터 먼저 (오프라인 보장)
    for q, exp in cases.items():
        r, c = rule_route(q)
        print(f"[rule] {r:9s}({c:.2f}) exp {exp}  | {q}")
    if llm.ping():
        print("--- LLM 라우터 ---")
        for q, exp in cases.items():
            res = route(q)
            print(f"[{res['via']}] {res['route']:9s}({res['conf']:.2f}) exp {exp}  | {q}")
    print("router.py OK")
