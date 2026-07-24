"""Document Agent — 비정형 법령 검색. 한국어 하이브리드(BM25 + dense) + rerank.

파이프라인:
  질의 → BM25(kiwi 형태소) top-N  ┐
       → dense(BGE-m3-ko) top-N   ┘→ RRF 융합 → cross-encoder rerank → top-k + 근거

한국어는 형태소 토크나이저 없이 BM25가 무의미하므로 kiwipiepy를 쓴다.
dense/reranker는 CPU 고정 — 공유 GPU 부하와 무관하게 재현되도록(워크플로우 권고).
임베딩은 빌드 시 1회 계산해 캐시하고, 이후엔 로드만 한다.

도구 진입점: DocAgent(...).search(query, k) → [{chunk_id, article_no, text, score, span}]
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")   # CPU 고정
os.environ.setdefault("HF_HUB_OFFLINE", "1")        # 캐시된 모델만 사용(권한 경고·네트워크 조회 억제)

ROOT = Path(__file__).resolve().parents[3]
CHUNKS = ROOT / "data/documents/law_chunks.jsonl"
EMB_CACHE = ROOT / "data/documents/law_emb.npy"
EMB_MODEL = "dragonkue/BGE-m3-ko"
RERANK_MODEL = "dragonkue/bge-reranker-v2-m3-ko"

_embedder = None
_reranker = None


def _emb_model():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(EMB_MODEL, device="cpu")
    return _embedder


def _rerank_model():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder(RERANK_MODEL, device="cpu")
    return _reranker


def _tokenize(text: str, kiwi) -> list[str]:
    """kiwi 형태소 표면형. 조사·기호는 BM25 노이즈라 실질 품사만 남긴다."""
    keep = {"NNG", "NNP", "NNB", "NR", "NP", "VV", "VA", "SL", "SN", "SH"}
    return [t.form for t in kiwi.tokenize(text) if t.tag in keep]


def rrf(rank_lists: list[list[int]], k: int = 60) -> dict[int, float]:
    """Reciprocal Rank Fusion. 각 순위목록에서 1/(k+rank)를 합산."""
    score: dict[int, float] = {}
    for ranks in rank_lists:
        for r, idx in enumerate(ranks):
            score[idx] = score.get(idx, 0.0) + 1.0 / (k + r)
    return score


class DocAgent:
    def __init__(self, chunks_path: Path = CHUNKS, rebuild: bool = False):
        from kiwipiepy import Kiwi
        import bm25s

        self.chunks = [json.loads(l) for l in chunks_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.kiwi = Kiwi()

        corpus_tokens = [_tokenize(c["text"], self.kiwi) for c in self.chunks]
        self.bm25 = bm25s.BM25()
        self.bm25.index(corpus_tokens)

        self.emb = self._load_or_build_emb(rebuild)

    def _load_or_build_emb(self, rebuild: bool) -> np.ndarray:
        if EMB_CACHE.exists() and not rebuild:
            emb = np.load(EMB_CACHE)
            if len(emb) == len(self.chunks):
                return emb
        vecs = _emb_model().encode([c["text"] for c in self.chunks],
                                   normalize_embeddings=True, show_progress_bar=False)
        emb = np.asarray(vecs, dtype=np.float32)
        EMB_CACHE.parent.mkdir(parents=True, exist_ok=True)
        np.save(EMB_CACHE, emb)
        return emb

    def search(self, query: str, k: int = 5, cand: int = 30,
               min_score: float | None = None) -> list[dict]:
        """하이브리드 검색 + rerank. min_score 미만이면 빈 목록(→ 근거 불충분)."""
        import bm25s

        q_tokens = _tokenize(query, self.kiwi)
        # BM25 후보
        if q_tokens:
            res, _ = self.bm25.retrieve(bm25s.tokenize([" ".join(q_tokens)], show_progress=False),
                                        k=min(cand, len(self.chunks)), show_progress=False)
            bm25_ranks = list(res[0])
        else:
            bm25_ranks = []
        # dense 후보
        qv = _emb_model().encode([query], normalize_embeddings=True, show_progress_bar=False)[0]
        sims = self.emb @ np.asarray(qv, dtype=np.float32)
        dense_ranks = list(np.argsort(-sims)[:cand])

        fused = rrf([bm25_ranks, dense_ranks])
        top = sorted(fused, key=fused.get, reverse=True)[:cand]
        if not top:
            return []

        # cross-encoder rerank
        pairs = [(query, self.chunks[i]["text"]) for i in top]
        scores = _rerank_model().predict(pairs, show_progress_bar=False)
        order = np.argsort(-np.asarray(scores))[:k]

        out = []
        for rank_i in order:
            idx = top[int(rank_i)]
            c = self.chunks[idx]
            sc = float(scores[int(rank_i)])
            if min_score is not None and sc < min_score:
                continue
            out.append({"chunk_id": c["chunk_id"], "article_no": c["article_no"],
                        "article_title": c["article_title"], "text": c["text"],
                        "score": round(sc, 4), "span": [0, len(c["text"])],
                        "source_id": c["chunk_id"]})
        return out


if __name__ == "__main__":
    agent = DocAgent()
    print(f"색인: {len(agent.chunks)}청크, emb {agent.emb.shape}")
    for q in ["감염병의 정의와 급수 분류", "제2급감염병 신고 기한", "역학조사는 언제 하나"]:
        hits = agent.search(q, k=3)
        print(f"\nQ: {q}")
        for h in hits:
            print(f"  [{h['score']:.2f}] {h['article_no']}({h['article_title']}) {h['text'][:55]}")
    assert agent.search("감염병 정의", k=3), "검색 결과가 있어야"
    # 도메인 밖 질의는 rerank 점수가 낮아 근거 게이트에서 걸러진다
    print("\ndocs.py self-check OK")
