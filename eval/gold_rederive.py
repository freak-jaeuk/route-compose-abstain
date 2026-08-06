"""Independent rule-based re-derivation of the 60 audit-set answerability labels.

NOT a second annotator.  A single rule set, written against the *stores* only
(SQLite schema + rows, statute article set, disease hub), that never reads a
system trace, run log, or the authored gold.  Its purpose is to catch labels
that the stores themselves contradict -- the class of error that produced the
g046 correction (Sec. 5.3).

Run:  python eval/gold_rederive.py
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/structured/kdca.db"
CHUNKS = ROOT / "data/documents/law_chunks.jsonl"
GOLD = ROOT / "eval/qa/gold.jsonl"

con = sqlite3.connect(DB)

# ---- store vocabulary (read from the stores, not from the router) ----------
DISEASES = {r[0] for r in con.execute("SELECT DISTINCT disease FROM infectious_stats")}
HUB = {r[0]: r for r in con.execute("SELECT disease, law_grade, surveillance, matched FROM disease_hub")}
REGIONS = {r[0] for r in con.execute("SELECT DISTINCT region FROM infectious_stats")}
# a year is *reported* iff its rows do not all sum to zero (placeholder years)
YEAR_TOTAL = dict(con.execute("SELECT year, SUM(cases) FROM infectious_stats GROUP BY year"))
REPORTED_YEARS = {y for y, s in YEAR_TOTAL.items() if s > 0}
CHUNK = [json.loads(l) for l in CHUNKS.open()]
ARTICLES = {c["article_no"] for c in CHUNK}
LAWTEXT = "\n".join(c["text"] for c in CHUNK)

# schema fact: no column of either table names a person.
SCHEMA_COLS = {d[1] for t in ("infectious_stats", "disease_hub")
               for d in con.execute(f"PRAGMA table_info({t})")}

ART_RE = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")
YEAR_RE = re.compile(r"(19|20)\d{2}")
# 2-4 hangul syllables immediately followed by a person-marker
PERSON_RE = re.compile(r"[가-힣]{2,4}\s*(씨|님|환자|양|군)\b|주민등록번호|개인정보")
# place terms are tested against the region column's own domain, not a hand list:
# an explicit aggregation unit ("<X>별") or a bare administrative name.
AGG_RE = re.compile(r"([가-힣]{1,6}?(?:구|동|읍|면|군|시))별")
BARE_RE = re.compile(r"(?<![가-힣])([가-힣]{2,3}(?:구|군|시))(?![가-힣])")
STAT_INTENT = re.compile(r"발생|건수|통계|추이|현황|환자\s*수|몇\s*명")
FUTURE = re.compile(r"전망|예측|예상")


def longest_disease(q: str) -> str | None:
    hits = [d for d in DISEASES if d in q]
    return max(hits, key=len) if hits else None


def years(q: str) -> list[int]:
    return [int(m.group(0)) for m in YEAR_RE.finditer(q)]


def derive(q: str) -> tuple[bool, str]:
    """Return (answerable, rule id).  Rules are checked in store order."""
    # R1  no store column identifies a person
    if PERSON_RE.search(q):
        return False, "R1 no personal-identifier column in either table"
    # R2  aggregation unit below the region column's domain
    for rx in (AGG_RE, BARE_RE):
        for m in rx.finditer(q):
            if m.group(1) not in REGIONS:
                return False, f"R2 '{m.group(1)}' outside region domain"
    ys = years(q)
    # R3  future / unreported year, or a projection the store cannot express
    if FUTURE.search(q):
        return False, "R3 projection; store holds observations only"
    if ys and STAT_INTENT.search(q) and not any(y in REPORTED_YEARS for y in ys):
        return False, f"R3 year(s) {ys} carry no reported rows"
    # R4  statistical query: disease must resolve in the stats table
    if STAT_INTENT.search(q) and (ys or any(r in q for r in ("시도", "지역", "전국"))):
        d = longest_disease(q)
        if d is None:
            return False, "R4 no disease name resolves against infectious_stats"
        return True, f"R4 {d} x {ys} resolves in infectious_stats"
    # R5  statute query by article number
    m = ART_RE.search(q)
    if m:
        no = f"제{m.group(1)}조" + (f"의{m.group(2)}" if m.group(2) else "")
        return (no in ARTICLES), f"R5 {no} {'in' if no in ARTICLES else 'not in'} article set"
    # R6  graph: statutory grade of a disease
    if re.search(r"급\s*감염병|급수|몇\s*급|분류", q):
        d = longest_disease(q)
        if d and HUB.get(d, ("", "", "", "N"))[1]:
            return True, f"R6 {d} has law_grade in disease_hub"
        if re.search(r"제[1-4]급", q):
            return True, "R6 grade term defined in statute text"
        return False, "R6 no statutory grade resolves"
    # R7  statute query by defined term: the term must appear in the corpus
    for term in re.findall(r"[가-힣]{2,10}(?=란|의 정의|이란|에 관한|의 목적)", q):
        if term in LAWTEXT:
            return True, f"R7 term '{term}' occurs in statute corpus"
    if re.search(r"신고|규정|법|조문|처분|대책|목적|접종|조사", q) and not ys:
        # any statute-side question whose content words hit the corpus
        toks = [t for t in re.findall(r"[가-힣]{2,}", q) if len(t) >= 2]
        if any(t in LAWTEXT for t in toks):
            return True, "R7 content words occur in statute corpus"
    return False, "R8 no store supports the request"


def main() -> None:
    gold = [json.loads(l) for l in GOLD.open()]
    dis = []
    for g in gold:
        pred, why = derive(g["question"])
        if pred != g["answerable"]:
            dis.append((g["qid"], g["answerable"], pred, why, g["question"]))
    agree = len(gold) - len(dis)
    print(f"agreement {agree}/{len(gold)} = {agree/len(gold):.3f}")
    for qid, au, pr, why, q in dis:
        print(f"  {qid}  gold={au}  rule={pr}  [{why}]  {q}")
    # self-check: the rule set must reproduce the two facts the paper asserts
    assert 2025 not in REPORTED_YEARS and 2026 not in REPORTED_YEARS
    assert not any("name" in c or "person" in c for c in SCHEMA_COLS)


if __name__ == "__main__":
    main()
