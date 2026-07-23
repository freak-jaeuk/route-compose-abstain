"""trace JSONL writer — 도구 호출 1회 = 1줄.

논문의 모든 정량 지표는 이 로그에서 유도된다 (ARCHITECTURE_v1.md §8).
계측을 나중에 붙이면 전체 실험을 재실행해야 하므로 1일차부터 모든 호출이 여기를 지난다.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path


class Tracer:
    """append-only JSONL. run 단위 공통 필드를 매 줄에 병합한다."""

    def __init__(self, path: str | Path, **run_fields):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_fields = run_fields
        self._step = 0

    def log(self, **fields) -> dict:
        row = {**self.run_fields, **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        return row

    @contextmanager
    def step(self, tool: str, tool_input: dict, **extra):
        """with tracer.step(...) as rec: rec['output'] = ...

        지연시간·예외를 자동 기록한다. 예외는 ok=False 로 남기고 그대로 전파한다.
        """
        self._step += 1
        rec: dict = {
            "step": self._step,
            "tool": tool,
            "input": tool_input,
            "output": None,
            "ok": True,
            "error": None,
            "retry": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            **extra,
        }
        t0 = time.perf_counter()
        try:
            yield rec
        except Exception as e:  # noqa: BLE001 - 실패도 실험 데이터다
            rec["ok"] = False
            rec["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            rec["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            self.log(**rec)


def read_traces(path: str | Path) -> list[dict]:
    """파일 하나 또는 디렉터리 아래 모든 *.jsonl 을 읽는다."""
    p = Path(path)
    files = sorted(p.rglob("*.jsonl")) if p.is_dir() else [p]
    rows = []
    for f in files:
        with f.open(encoding="utf-8") as fh:
            rows.extend(json.loads(line) for line in fh if line.strip())
    return rows


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "run.jsonl"
        tr = Tracer(out, run_id="r1", qid="qa_0001", system="proposed", seed=0)

        with tr.step("retrieve_documents", {"query": "실손보험 면책"}) as rec:
            rec["output"] = {"chunks": 5}
            rec["tokens_in"] = 120

        try:
            with tr.step("query_structured_data", {"table": "nope"}):
                raise ValueError("OUT_OF_SCHEMA")
        except ValueError:
            pass

        tr.log(step=3, tool="policy", verdict="ABSTAIN", abstain_reason="OUT_OF_SCHEMA")

        rows = read_traces(out)
        assert len(rows) == 3, rows
        assert rows[0]["ok"] is True and rows[0]["step"] == 1
        assert rows[0]["run_id"] == "r1" and rows[0]["qid"] == "qa_0001"
        assert rows[0]["latency_ms"] >= 0
        assert rows[1]["ok"] is False and "OUT_OF_SCHEMA" in rows[1]["error"]
        assert rows[2]["verdict"] == "ABSTAIN"
        # 디렉터리 읽기도 동작
        assert len(read_traces(d)) == 3
        print("trace.py self-check OK")
