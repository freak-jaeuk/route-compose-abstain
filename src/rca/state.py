"""실행 상태 모델. 오케스트레이터는 RunState 하나만 들고 다닌다."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Route = Literal["SQL", "DOCUMENT", "GRAPH", "COMPOSITE", "ABSTAIN"]
Verdict = Literal["ANSWER", "CLARIFY", "ABSTAIN"]
AbstainReason = Literal[
    "OUT_OF_SCHEMA",
    "INSUFFICIENT_EVIDENCE",
    "LOW_ROUTER_CONFIDENCE",
    "SQL_EXECUTION_FAILURE",
    "GRAPH_PATH_NOT_FOUND",
    "SOURCE_CONFLICT",
    "PRIVACY_RESTRICTED",
    "BUDGET_EXCEEDED",
]
ClarifyReason = Literal[
    "MISSING_TIME_RANGE",
    "MISSING_REGION",
    "AMBIGUOUS_ENTITY",
    "MULTIPLE_INTERPRETATIONS",
]


class ToolCall(BaseModel):
    step: int
    tool: str
    input: dict = Field(default_factory=dict)
    output: dict | None = None
    ok: bool = True
    error: str | None = None
    retry: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0


class Evidence(BaseModel):
    source_type: Literal["sql", "document", "graph"]
    source_id: str
    text: str = ""
    span: tuple[int, int] | None = None
    score: float | None = None


class Budget(BaseModel):
    max_steps: int = 8
    max_calls_per_tool: int = 3
    max_tokens: int = 20_000
    deadline_ms: int = 30_000

    def exhausted(self, steps: list[ToolCall], elapsed_ms: int, next_tool: str | None = None) -> bool:
        """이미 실행된 `steps` 기준으로 **다음 호출을 시작할 수 없는지** 판정한다.

        모든 한도가 동일하게 '한도에 도달하면 더 못 간다'(>=) 의미를 갖는다.
        `next_tool` 을 주면 해당 도구의 호출 한도까지 함께 본다.
        """
        if len(steps) >= self.max_steps or elapsed_ms >= self.deadline_ms:
            return True
        if sum(c.tokens_in + c.tokens_out for c in steps) >= self.max_tokens:
            return True
        if next_tool is not None:
            if sum(1 for c in steps if c.tool == next_tool) >= self.max_calls_per_tool:
                return True
        return False


class RunState(BaseModel):
    run_id: str
    qid: str
    question: str
    system: str
    route_pred: Route | None = None
    route_conf: float | None = None
    plan: list[str] = Field(default_factory=list)
    steps: list[ToolCall] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    verdict: Verdict | None = None
    abstain_reason: AbstainReason | None = None
    clarify_reason: ClarifyReason | None = None
    answer: str | None = None
    # risk-coverage 곡선(AURCC)은 연속 점수가 있어야 그린다. verdict만으로는 점 하나뿐.
    answer_confidence: float | None = None
    # 최종 답변이 실제로 인용한 근거. 검색된 전체 evidence 와 구분해야 citation precision 계산 가능.
    cited: list[str] = Field(default_factory=list)


if __name__ == "__main__":
    b = Budget(max_steps=3, max_calls_per_tool=2, max_tokens=100)
    two = [ToolCall(step=i, tool="docs", tokens_in=10) for i in range(2)]

    assert not b.exhausted(two, elapsed_ms=0)
    assert b.exhausted(two + [ToolCall(step=2, tool="docs")], 0), "step 한도(=3)에서 정지"
    assert b.exhausted(two, elapsed_ms=30_000), "deadline 경계 포함"
    assert b.exhausted([ToolCall(step=i, tool="t", tokens_out=50) for i in range(2)], 0), "토큰 한도 경계"

    # 도구별 한도는 step 한도에 가려지지 않도록 넉넉한 예산에서 따로 검증한다
    wide = Budget(max_steps=99, max_calls_per_tool=2, max_tokens=10**9)
    one_sql = [ToolCall(step=0, tool="sql")]
    assert not wide.exhausted(one_sql, 0, next_tool="sql"), "1회 호출 후에는 가능"
    assert wide.exhausted(one_sql * 2, 0, next_tool="sql"), "2회 호출 후 도구 한도 도달"
    assert not wide.exhausted(one_sql * 2, 0, next_tool="docs"), "다른 도구는 영향 없음"
    assert not wide.exhausted(one_sql * 2, 0), "next_tool 미지정 시 도구 한도 미적용"

    s = RunState(run_id="r1", qid="qa_0001", question="q", system="proposed")
    assert s.verdict is None and s.answer_confidence is None and s.cited == []
    print("state.py self-check OK")
