"""FastAPI 데모 — 다중 소스 질의응답 엔드포인트.

모델·DB 로드가 비싸므로 Agents를 lifespan에서 1회 로드해 재사용한다.
Qdrant local·그래프 DB가 단일 프로세스 락을 쓰므로 workers=1로 기동한다.
엔드포인트는 sync def(블로킹 LLM·검색을 threadpool에서).

기동:  PYTHONPATH=src uvicorn rca.api:app --port 8000 --workers 1
질의:  curl -s localhost:8000/query -H 'content-type: application/json' -d '{"question":"..."}'
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .orchestrator import Agents, run_query
from .trace import Tracer

RUNS = None
_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    _state["agents"] = Agents()          # 모델·DB 1회 로드
    _state["n"] = 0
    yield
    _state.clear()


app = FastAPI(title="Route·Compose·Abstain", lifespan=lifespan)


class Q(BaseModel):
    question: str
    use_llm: bool = True


@app.post("/query")
def query(q: Q) -> dict:
    _state["n"] += 1
    qid = f"api_{_state['n']:05d}"
    # 요청당 Tracer 인스턴스 → 동시 요청의 step 번호가 섞이지 않는다(단일 writer 계약).
    from pathlib import Path
    tr = Tracer(Path("data/runs/api.jsonl"), run_id="api", system="proposed",
                backbone="gpt-oss-20b")
    t0 = time.perf_counter()
    with tr.query(qid) as summ:
        st = run_query(q.question, qid, tr, _state["agents"], use_llm=q.use_llm)
        summ.update(verdict=st.verdict, answer_confidence=st.answer_confidence,
                    abstain_reason=st.abstain_reason, cited=st.cited)
    return {
        "question": q.question,
        "route": st.route_pred, "route_conf": st.route_conf,
        "verdict": st.verdict,
        "answer": st.answer,
        "abstain_reason": st.abstain_reason,
        "confidence": st.answer_confidence,
        "cited": st.cited,
        "evidence": [{"type": e["source_type"], "source": e.get("article_no") or e["source_id"]}
                     for e in st.evidence],
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "disclaimer": "본 응답은 의료·법률 자문이 아니며 공개 통계·법령에 근거한 참고용입니다.",
    }


@app.get("/health")
def health() -> dict:
    return {"ok": True, "requests": _state.get("n", 0)}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _HTML


_HTML = """<!doctype html><html lang=ko><meta charset=utf-8>
<title>Route·Compose·Abstain — 감염병 QA</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}
 h1{font-size:1.3rem} .sub{color:#666;font-size:.9rem;margin-top:-.5rem}
 input{width:100%;padding:.7rem;font-size:1rem;border:1px solid #ccc;border-radius:6px}
 button{margin-top:.5rem;padding:.6rem 1.2rem;font-size:1rem;border:0;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer}
 .ex{margin:.6rem 0;font-size:.85rem} .ex a{color:#2563eb;cursor:pointer;margin-right:.8rem}
 #out{margin-top:1.2rem} .card{border:1px solid #e5e5e5;border-radius:8px;padding:1rem;margin-top:.8rem}
 .tag{display:inline-block;font-size:.75rem;padding:.15rem .5rem;border-radius:4px;background:#eef;color:#224;margin-right:.4rem}
 .abstain{background:#fee;color:#822} .answer{white-space:pre-wrap;line-height:1.5}
 .meta{color:#888;font-size:.8rem;margin-top:.6rem} .disc{color:#999;font-size:.75rem;margin-top:1rem}
</style>
<h1>Route · Compose · Abstain</h1>
<p class=sub>질의를 SQL·법령문서·지식그래프 중 필요한 경로로 라우팅하고, 근거가 없으면 답하지 않습니다.</p>
<input id=q placeholder="예: 2023년 시도별 수두 발생 건수" onkeydown="if(event.key=='Enter')go()">
<button onclick=go()>질의</button>
<div class=ex>예시:
 <a onclick="fill('2023년 시도별 수두 발생 건수')">통계(SQL)</a>
 <a onclick="fill('제2급감염병의 신고 기한은?')">조문(문서)</a>
 <a onclick="fill('제8조의2가 시행령에 위임한 세부 사항은?')">위임관계(그래프)</a>
 <a onclick="fill('김철수 확진 여부 알려줘')">거절(PII)</a>
</div>
<div id=out></div>
<script>
function fill(t){document.getElementById('q').value=t;go()}
async function go(){
 const q=document.getElementById('q').value.trim(); if(!q)return;
 const out=document.getElementById('out'); out.innerHTML='<p>조회 중…</p>';
 try{
  const r=await fetch('/query',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({question:q})});
  const d=await r.json();
  const ab=d.verdict!=='ANSWER';
  out.innerHTML=`<div class=card>
   <span class="tag ${ab?'abstain':''}">${d.verdict}</span>
   <span class=tag>route: ${d.route} (${(d.route_conf||0).toFixed(2)})</span>
   ${d.abstain_reason?`<span class="tag abstain">${d.abstain_reason}</span>`:''}
   <div class=answer style="margin-top:.6rem">${(d.answer||'근거가 부족하여 답변하지 않습니다.').replace(/</g,'&lt;')}</div>
   <div class=meta>근거: ${(d.evidence||[]).map(e=>e.type+':'+e.source).join(', ')||'—'} · ${d.elapsed_ms}ms</div>
   <div class=disc>${d.disclaimer}</div>
  </div>`;
 }catch(e){out.innerHTML='<p>오류: '+e+'</p>'}
}
</script></html>"""
