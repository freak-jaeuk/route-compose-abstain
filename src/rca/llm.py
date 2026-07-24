"""LLM 백본 — 로컬 vLLM(gpt-oss-20b, OpenAI 호환)에 붙는다.

OpenAI 호환이라 코드가 로컬/원격 무관하다. base_url·model만 환경변수로 바꾸면
클라우드 API로도 동작한다. urllib만 쓰므로 openai SDK 의존이 없다.

gpt-oss-20b는 reasoning 모델이라 max_tokens를 넉넉히 줘야 content가 빈 채로
끊기지 않는다(reasoning에 다 쓰고 answer가 안 나오는 사고 방지).

계측: 매 호출이 (content, tokens_in, tokens_out)을 돌려줘 trace에 kind='llm'으로 남는다.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

BASE = os.environ.get("RCA_LLM_BASE", "http://localhost:30070/v1")
MODEL = os.environ.get("RCA_LLM_MODEL", "openai/gpt-oss-20b")


class LLMError(Exception):
    pass


def chat(messages: list[dict], max_tokens: int = 512, temperature: float = 0.0,
         timeout: int = 60) -> dict:
    """단일 호출. 반환 {content, tokens_in, tokens_out}. 실패는 LLMError."""
    body = json.dumps({"model": MODEL, "messages": messages,
                       "temperature": temperature, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(f"{BASE}/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"LLM 호출 실패: {e}") from e
    msg = d["choices"][0]["message"]
    usage = d.get("usage", {})
    return {"content": (msg.get("content") or "").strip(),
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0)}


def chat_json(messages: list[dict], max_tokens: int = 512, **kw) -> dict:
    """content에서 첫 JSON 객체를 파싱해 함께 반환. reasoning 모델 대비 넉넉한 토큰."""
    res = chat(messages, max_tokens=max_tokens, **kw)
    m = re.search(r"\{.*\}", res["content"], re.S)
    if not m:
        raise LLMError(f"JSON 없음: {res['content'][:120]}")
    try:
        res["json"] = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON 파싱 실패: {e}") from e
    return res


def ping(timeout: int = 5) -> bool:
    """서버 생존 확인. 오프라인이면 규칙 라우터로 폴백하는 데 쓴다."""
    try:
        with urllib.request.urlopen(f"{BASE}/models", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    if not ping():
        print(f"vLLM 서버 없음({BASE}) — 스킵")
        raise SystemExit(0)
    r = chat_json([
        {"role": "system", "content": "질의를 SQL/DOCUMENT/GRAPH/COMPOSITE/ABSTAIN 중 하나로 분류. "
                                      'JSON만: {"route":"...","conf":0.0}'},
        {"role": "user", "content": "2023년 시도별 수두 발생 건수"},
    ], max_tokens=256)
    print("route:", r["json"], f"(tok {r['tokens_in']}/{r['tokens_out']})")
    assert r["json"]["route"] in {"SQL", "DOCUMENT", "GRAPH", "COMPOSITE", "ABSTAIN"}
    print("llm.py self-check OK")
