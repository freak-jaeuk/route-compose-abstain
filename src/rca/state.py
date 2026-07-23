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

    def exceeded(self, steps: list[ToolCall], elapsed_ms: int) -> bool:
        if len(steps) >= self.max_steps or elapsed_ms >= self.deadline_ms:
            return True
        if sum(c.tokens_in + c.tokens_out for c in steps) >= self.max_tokens:
            return True
        for tool in {c.tool for c in steps}:
            if sum(1 for c in steps if c.tool == tool) > self.max_calls_per_tool:
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
    answer: str | None = None


if __name__ == "__main__":
    b = Budget(max_steps=3, max_calls_per_tool=2, max_tokens=100)
    calls = [ToolCall(step=i, tool="retrieve_documents", tokens_in=10) for i in range(2)]
    assert not b.exceeded(calls, elapsed_ms=0)
    assert b.exceeded(calls + [ToolCall(step=2, tool="retrieve_documents")], 0), "step 한도"
    assert b.exceeded(calls, elapsed_ms=99_999), "deadline"
    assert b.exceeded([ToolCall(step=i, tool="t", tokens_out=60) for i in range(2)], 0), "토큰 한도"
    over_tool = [ToolCall(step=i, tool="t") for i in range(3)]
    assert b.exceeded(over_tool, 0), "도구별 호출 한도"

    s = RunState(run_id="r1", qid="qa_0001", question="q", system="proposed")
    assert s.verdict is None and s.budget.max_steps == 8
    print("state.py self-check OK")
