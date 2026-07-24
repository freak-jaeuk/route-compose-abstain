"""Graph Agent — 지식그래프 질의. 자유 Cypher 생성 대신 템플릿 3종.

자유 Cypher 생성은 실패율·보안 위험이 크다. 관계 유형별 템플릿을 고정하고
파라미터만 바인딩한다. 쓰기 절(CREATE/MERGE/DELETE/SET)은 코드 경로에 없다.

템플릿:
  T1 질병 → 급수          (d:DiseaseType)-[:CLASSIFIED_AS]->(g:Grade)
  T2 질병 → 정의 조문      (d:DiseaseType)-[:DEFINED_IN]->(a:Article)
  T3 법 조문 → 위임 시행령  (e:Article)-[:DELEGATES_TO]->(a:Article {no})   2-hop

문서 검색이 위계를 판정하지 못하는 관계형 질의를 순회로 답한다.
도구 진입점: GraphAgent(...).query(question) → {template, paths, source_ids}
"""

from __future__ import annotations

import re
from pathlib import Path

import ladybug

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "data/graph/idc.kuzu"

# 파라미터만 바인딩. 구조는 고정 문자열이라 주입 불가.
T1 = ("MATCH (d:DiseaseType {name:$name})-[:CLASSIFIED_AS]->(g:Grade) "
      "RETURN d.name, g.level, d.surveillance")
T2 = ("MATCH (d:DiseaseType {name:$name})-[:DEFINED_IN]->(a:Article) "
      "RETURN d.name, a.no, a.title")
T3 = ("MATCH (e:Article)-[:DELEGATES_TO]->(a:Article {no:$no}) "
      "RETURN a.no, a.title, e.no, e.title ORDER BY e.no")

ARTICLE_RE = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")


class GraphAgent:
    def __init__(self, db: Path = DB):
        if not db.exists():
            raise FileNotFoundError(f"그래프 DB 없음: {db} (build_graph.py 먼저)")
        self.con = ladybug.Connection(ladybug.Database(str(db), read_only=True))
        self.diseases = {r[0] for r in self._rows("MATCH (d:DiseaseType) RETURN d.name")}

    def _rows(self, cypher: str, params: dict | None = None) -> list[list]:
        r = self.con.execute(cypher, params or {})
        out = []
        while r.has_next():
            out.append(r.get_next())
        return out

    # ── 템플릿 ──────────────────────────────────────────────
    def disease_grade(self, disease: str) -> list[dict]:
        return [{"disease": d, "grade": g, "surveillance": s}
                for d, g, s in self._rows(T1, {"name": disease})]

    def disease_definition(self, disease: str) -> list[dict]:
        return [{"disease": d, "article_no": no, "article_title": t}
                for d, no, t in self._rows(T2, {"name": disease})]

    def article_delegations(self, article_no: str) -> list[dict]:
        return [{"law_article": a, "law_title": at, "decree_article": e, "decree_title": et}
                for a, at, e, et in self._rows(T3, {"no": article_no})]

    # ── 라우팅 (규칙; LLM 템플릿 선택은 추후) ────────────────
    def _find_disease(self, q: str) -> str | None:
        # 질의에 포함된 등록 질병명 중 가장 긴 것
        hits = [d for d in self.diseases if d and d in q]
        return max(hits, key=len) if hits else None

    def query(self, question: str) -> dict:
        """질의 유형을 규칙으로 판별해 템플릿 선택. 결과+근거 반환.

        매칭 실패 시 paths 빈 목록 → 오케스트레이터가 GRAPH_PATH_NOT_FOUND로.
        """
        art = ARTICLE_RE.search(question)
        disease = self._find_disease(question)

        # 위임/시행령/하위규정 의도 + 조문번호 → T3
        if art and re.search(r"위임|시행령|하위|세부|규정", question):
            no = f"제{art.group(1)}조" + (f"의{art.group(2)}" if art.group(2) else "")
            paths = self.article_delegations(no)
            return {"template": "T3", "paths": paths,
                    "source_ids": [f"시행령 {p['decree_article']}" for p in paths]}
        # 질병 + 정의/근거/조문 → T2
        if disease and re.search(r"정의|근거|조문|규정|어디", question):
            paths = self.disease_definition(disease)
            return {"template": "T2", "paths": paths,
                    "source_ids": [p["article_no"] for p in paths]}
        # 질병 + 급수/분류/신고 → T1
        if disease:
            paths = self.disease_grade(disease)
            return {"template": "T1", "paths": paths,
                    "source_ids": [f"{disease} 급수분류"] if paths else []}
        return {"template": None, "paths": [], "source_ids": []}


if __name__ == "__main__":
    g = GraphAgent()
    print(f"질병 노드 {len(g.diseases)}종")
    cases = [
        "수두는 몇 급 감염병인가",
        "장티푸스의 정의는 어느 조문에 있나",
        "제8조의2가 시행령에 위임한 세부 사항은",
        "제42조 위임 시행령 규정",
    ]
    for q in cases:
        r = g.query(q)
        print(f"\nQ: {q}\n  → {r['template']}, {len(r['paths'])}건")
        for p in r["paths"][:3]:
            print("   ", p)

    assert g.disease_grade("수두")[0]["grade"] == "2", "수두=2급"
    assert g.disease_definition("장티푸스"), "정의 조문 있어야"
    assert g.article_delegations("제8조의2"), "제8조의2 위임 시행령 있어야"
    assert g.query("존재하지않는병 급수")["paths"] == [], "미매칭은 빈 경로"
    print("\ngraph.py self-check OK")
