"""그래프 스토어 구축 — 감염병 법령 지식그래프 (ladybug, 임베디드 openCypher).

그래프를 LLM으로 만들지 않는다. 조문 간 관계가 규칙으로 추출 가능하므로
재현성이 높고 "LLM이 만든 부정확한 그래프"라는 비판을 원천 차단한다.

노드:  Article(법·시행령 조문) · DiseaseType(감염병) · Grade(급수)
엣지:  (시행령 Article)-[:DELEGATES_TO]->(법 Article)   시행령 본문 "법 제N조" 위임
       (DiseaseType)-[:CLASSIFIED_AS]->(Grade)          법 제2조 급수 분류
       (DiseaseType)-[:DEFINED_IN]->(법 제2조 Article)  정의 근거

문서 검색이 위계를 판정하지 못하는 질의(예: "제3급감염병 신고의 시행령 위임")를
2-hop 순회로 답한다.

입력:  data/law/{idc_law,idc_enforce}.json · data/disease_hub.csv
출력:  data/graph/idc.kuzu (디렉터리)

용례:  python scripts/build_graph.py
"""

from __future__ import annotations

import csv
import json
import re
import shutil
from pathlib import Path

import ladybug

ROOT = Path(__file__).resolve().parents[1]
LAW = ROOT / "data/law/idc_law.json"
ENF = ROOT / "data/law/idc_enforce.json"
HUB = ROOT / "data/disease_hub.csv"
DB = ROOT / "data/graph/idc.kuzu"

DELEG = re.compile(r"법\s*제(\d+)조(?:의(\d+))?")   # 시행령 본문의 법 위임 참조


def _as_list(x):
    return [] if x is None else (x if isinstance(x, list) else [x])


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", str(s))).strip()


def _aid(a: dict, prefix: str) -> tuple[str, str]:
    no, br = a.get("조문번호", ""), a.get("조문가지번호")
    key = f"{prefix}:{no}-{br}" if br else f"{prefix}:{no}"
    label = f"제{no}조의{br}" if br else f"제{no}조"
    return key, label


def _full_text(a: dict) -> str:
    parts = [_clean(a.get("조문내용", ""))]
    for h in _as_list(a.get("항")):
        parts.append(_clean(h.get("항내용", "")))
        for ho in _as_list(h.get("호")):
            parts.append(_clean(ho.get("호내용", "")))
    return " ".join(p for p in parts if p)


def load_articles(path: Path, prefix: str) -> dict[str, dict]:
    d = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for a in _as_list(d["법령"]["조문"]["조문단위"]):
        if a.get("조문여부") != "조문":
            continue
        key, label = _aid(a, prefix)
        out[key] = {"id": key, "law": prefix, "no": label,
                    "title": _clean(a.get("조문제목", "")), "text": _full_text(a),
                    "raw_no": a.get("조문번호", ""), "branch": a.get("조문가지번호")}
    return out


def main() -> None:
    law = load_articles(LAW, "law")
    enf = load_articles(ENF, "enf")
    hub = list(csv.DictReader(HUB.open(encoding="utf-8")))

    # 위임 엣지: 시행령 조문 → 법 조문 (법에 실제 존재하는 것만; dangling 제외)
    law_keys = set(law)
    deleg = []
    for k, a in enf.items():
        for n, b in set(DELEG.findall(a["text"])):
            tgt = f"law:{n}-{b}" if b else f"law:{n}"
            if tgt in law_keys:
                deleg.append((k, tgt))
    dangling = sum(1 for k, a in enf.items() for n, b in set(DELEG.findall(a["text"]))
                   if (f"law:{n}-{b}" if b else f"law:{n}") not in law_keys)

    if DB.exists():
        shutil.rmtree(DB)
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = ladybug.Connection(ladybug.Database(str(DB)))

    con.execute("CREATE NODE TABLE Article(id STRING, law STRING, no STRING, title STRING, text STRING, PRIMARY KEY(id))")
    con.execute("CREATE NODE TABLE DiseaseType(name STRING, code STRING, stat_grade STRING, law_grade STRING, surveillance STRING, PRIMARY KEY(name))")
    con.execute("CREATE NODE TABLE Grade(level STRING, PRIMARY KEY(level))")
    con.execute("CREATE REL TABLE DELEGATES_TO(FROM Article TO Article)")
    con.execute("CREATE REL TABLE CLASSIFIED_AS(FROM DiseaseType TO Grade)")
    con.execute("CREATE REL TABLE DEFINED_IN(FROM DiseaseType TO Article)")

    for a in {**law, **enf}.values():
        con.execute("CREATE (n:Article {id:$id, law:$law, no:$no, title:$title, text:$text})",
                    {"id": a["id"], "law": a["law"], "no": a["no"],
                     "title": a["title"], "text": a["text"][:2000]})
    for lv in ["1", "2", "3", "4"]:
        con.execute("CREATE (g:Grade {level:$l})", {"l": lv})
    for r in hub:
        con.execute("CREATE (d:DiseaseType {name:$n, code:$c, stat_grade:$sg, law_grade:$lg, surveillance:$s})",
                    {"n": r["disease"], "c": r["stat_code"], "sg": r["stat_grade"],
                     "lg": r["law_grade"], "s": r["surveillance"]})

    # 정의 조문 = 법 제2조
    defn = "law:2" if "law:2" in law else None
    for src, tgt in deleg:
        con.execute("MATCH (a:Article {id:$s}),(b:Article {id:$t}) CREATE (a)-[:DELEGATES_TO]->(b)",
                    {"s": src, "t": tgt})
    n_cls = n_def = 0
    for r in hub:
        if r["law_grade"]:
            con.execute("MATCH (d:DiseaseType {name:$n}),(g:Grade {level:$l}) CREATE (d)-[:CLASSIFIED_AS]->(g)",
                        {"n": r["disease"], "l": r["law_grade"]})
            n_cls += 1
        if defn:
            con.execute("MATCH (d:DiseaseType {name:$n}),(a:Article {id:$a}) CREATE (d)-[:DEFINED_IN]->(a)",
                        {"n": r["disease"], "a": defn})
            n_def += 1

    # 검증
    def one(q):
        r = con.execute(q); return r.get_next()[0] if r.has_next() else 0
    n_art = one("MATCH (a:Article) RETURN count(a)")
    n_deleg = one("MATCH ()-[r:DELEGATES_TO]->() RETURN count(r)")
    print(f"노드: Article {n_art} (법 {len(law)}+시행령 {len(enf)}), DiseaseType {len(hub)}, Grade 4")
    print(f"엣지: DELEGATES_TO {n_deleg} (dangling {dangling} 제외), CLASSIFIED_AS {n_cls}, DEFINED_IN {n_def}")

    # 샘플 2-hop: 제8조의2를 위임받은 시행령 조문
    r = con.execute("MATCH (e:Article)-[:DELEGATES_TO]->(a:Article {no:'제8조의2'}) RETURN e.no, e.title LIMIT 3")
    hits = []
    while r.has_next():
        hits.append(r.get_next())
    print(f"2-hop 예(제8조의2 위임): {hits}")
    print(f"→ {DB.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
