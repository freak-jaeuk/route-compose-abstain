"""trace JSONL writer — 도구 호출 1회 = 1줄.

논문의 모든 정량 지표는 이 로그에서 유도된다 (ARCHITECTURE_v1.md §8).
계측을 나중에 붙이면 전체 실험을 재실행해야 하므로 1일차부터 모든 호출이 여기를 지난다.

동시성 계약: **Tracer 1개 = 파일 1개 = 프로세스 1개.**
여러 실험 조건을 병렬 실행할 때는 조건별로 파일을 분리한다 (`eval/runs/<system>.jsonl`).
같은 파일에 여러 프로세스가 붙는 경우는 지원하지 않는다 — 락도 fsync도 두지 않는 대신
분석 단계에서 `qid` 수가 gold 문항 수와 일치하는지 확인해 누락 실행을 잡는다.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path


class Tracer:
    """append-only JSONL. run 단위 공통 필드를 매 줄에 병합한다.

    질의 단위 필드(`qid`, `step`)는 `query()` 컨텍스트가 관리한다.
    """

    def __init__(self, path: str | Path, **run_fields):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_fields = run_fields
        self._qid: str | None = None
        self._step = 0
        self._q_t0 = 0.0

    def log(self, **fields) -> dict:
        row = {**self.run_fields, "qid": self._qid, **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        return row

    @contextmanager
    def query(self, qid: str):
        """질의 1건의 스코프. step 번호를 1부터 다시 센다.

        종료 시 `tool="_run"` 종결 줄에 질의 전체 wall-clock(`elapsed_ms`)을 남긴다.
        도구 지연시간 합만으로는 라우터·검증기·정책 단계가 빠져 p95 지연이 과소평가된다.
        """
        prev = (self._qid, self._step, self._q_t0)
        self._qid, self._step, self._q_t0 = qid, 0, time.perf_counter()
        summary: dict = {}
        try:
            yield summary
        finally:
            self.log(
                step=self._step + 1,
                tool="_run",
                elapsed_ms=int((time.perf_counter() - self._q_t0) * 1000),
                **summary,
            )
            self._qid, self._step, self._q_t0 = prev

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


def read_traces(path: str | Path) -> tuple[list[dict], list[str]]:
    """파일 하나 또는 디렉터리 아래 모든 *.jsonl 을 읽는다.

    반환 `(rows, corrupt)`. 프로세스가 죽어 잘린 마지막 줄 하나 때문에
    전체 분석이 막히지 않도록 깨진 줄은 격리해 함께 돌려준다 (조용히 버리지 않는다).
    """
    p = Path(path)
    files = sorted(p.rglob("*.jsonl")) if p.is_dir() else [p]
    rows: list[dict] = []
    corrupt: list[str] = []
    for f in files:
        with f.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as e:
                    corrupt.append(f"{f}:{lineno}: {e}")
    return rows, corrupt


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "proposed.jsonl"
        tr = Tracer(out, run_id="r1", system="proposed", backbone="m@2026-07", seed=0)

        with tr.query("qa_0001") as summary:
            with tr.step("retrieve_documents", {"query": "실손보험 면책"}) as rec:
                rec["output"] = {"chunks": 5}
                rec["tokens_in"] = 120
            summary.update(verdict="ANSWER", answer_confidence=0.72, cited=["doc_001#3"])

        with tr.query("qa_0002") as summary:
            try:
                with tr.step("query_structured_data", {"table": "nope"}):
                    raise ValueError("unknown table")
            except ValueError:
                pass
            summary.update(verdict="ABSTAIN", abstain_reason="OUT_OF_SCHEMA", answer_confidence=0.05)

        rows, corrupt = read_traces(out)
        assert not corrupt and len(rows) == 4, (len(rows), corrupt)

        q1 = [r for r in rows if r["qid"] == "qa_0001"]
        q2 = [r for r in rows if r["qid"] == "qa_0002"]
        assert [r["step"] for r in q1] == [1, 2], "step은 질의마다 1부터"
        assert [r["step"] for r in q2] == [1, 2], "다음 질의에서 step 재시작"
        assert q1[0]["ok"] is True and q1[0]["run_id"] == "r1"
        assert q1[-1]["tool"] == "_run" and q1[-1]["elapsed_ms"] >= 0
        assert q1[-1]["answer_confidence"] == 0.72 and q1[-1]["cited"] == ["doc_001#3"]
        assert q2[0]["ok"] is False and "unknown table" in q2[0]["error"]
        assert q2[-1]["abstain_reason"] == "OUT_OF_SCHEMA", "예외가 나도 _run 줄은 남는다"

        # 잘린 줄이 있어도 나머지는 읽힌다
        out.open("a", encoding="utf-8").write('{"qid": "qa_0003", "to')
        rows2, corrupt2 = read_traces(d)
        assert len(rows2) == 4 and len(corrupt2) == 1, (len(rows2), corrupt2)
        print("trace.py self-check OK")
