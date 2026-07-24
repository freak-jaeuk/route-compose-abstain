"""조인 허브 구축 — 세 스토어를 잇는 disease_type 마스터.

감염병예방법 제2조의 급수별 감염병 목록(문서·그래프 쪽)과
질병청 통계 코드(SQL 쪽)를 질병명으로 조인한다. 이 파일 하나가
"결핵"(법 조문)과 "결핵"(통계 컬럼)이 같은 엔티티임을 보장한다 — COMPOSITE의 전제.

입력:
  data/law/law_grade_diseases.json   {제1급감염병: [질병명...], ...}  (법 제2조 규칙 파싱)
  data/raw/code_grade.json           {NA0001: {grade, name}, ...}     (통계 포털 코드)
출력:
  data/disease_hub.csv   disease · law_grade · stat_code · stat_grade · surveillance · matched

용례:  python scripts/build_disease_hub.py
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAW = ROOT / "data/law/law_grade_diseases.json"
CODE = ROOT / "data/raw/code_grade.json"
OUT = ROOT / "data/disease_hub.csv"

GRADE_NUM = {"제1급감염병": "1", "제2급감염병": "2", "제3급감염병": "3", "제4급감염병": "4"}
STAT_GRADE_NUM = {"01": "1", "02": "2", "03": "3"}


def norm(name: str) -> str:
    """조인용 정규화(엄격). 공백·중점 제거, 괄호는 남긴다.

    풍진(선천성)/풍진(후천성)처럼 통계가 괄호로 구분하는 것을 뭉개지 않으려 1차는 괄호 유지.
    """
    s = re.sub(r"\s+", "", name)
    return s.replace("·", "").replace("‧", "").lower()


def loose(name: str) -> str:
    """느슨한 정규화. 괄호(영문약어·부가)와 끝의 '감염증' 제거.

    통계 '반코마이신내성황색포도알균(VRSA) 감염증' → '반코마이신내성황색포도알균'
    == 법 '반코마이신내성황색포도알균'. 하이픈도 제거해 크로이츠펠트-야콥병 계열을 맞춘다.
    """
    s = re.sub(r"\(.*?\)", "", name)
    s = re.sub(r"\s+", "", s).replace("·", "").replace("‧", "").replace("-", "")
    s = s.lstrip("@")                      # 통계 '@엠폭스'의 최근추가 마커 제거
    return re.sub(r"감염증$", "", s).lower()


def main() -> None:
    law = json.loads(LAW.read_text(encoding="utf-8"))
    codes = json.loads(CODE.read_text(encoding="utf-8"))

    # 법: 엄격키/느슨키 두 인덱스
    law_strict: dict[str, tuple[str, str]] = {}
    law_loose: dict[str, tuple[str, str]] = {}
    for grade_kr, names in law.items():
        for nm in names:
            if nm == "삭제":
                continue
            law_strict[norm(nm)] = (nm, GRADE_NUM[grade_kr])
            law_loose[loose(nm)] = (nm, GRADE_NUM[grade_kr])

    def match_law(stat_name: str):
        # 1) 엄격 정확 → 2) 느슨 정확 → 3) 느슨 prefix(법명이 통계명 접두거나 반대)
        if (h := law_strict.get(norm(stat_name))):
            return h
        lk = loose(stat_name)
        if (h := law_loose.get(lk)):
            return h
        for llk, hit in law_loose.items():
            if llk and (lk.startswith(llk) or llk.startswith(lk)):
                return hit
        return None

    rows = []
    matched = 0
    for code, v in sorted(codes.items()):
        stat_name = v["name"]
        stat_grade = STAT_GRADE_NUM.get(v["grade"], v["grade"])
        law_hit = match_law(stat_name)
        rows.append({
            "disease": stat_name,
            "law_grade": law_hit[1] if law_hit else "",
            "stat_code": code,
            "stat_grade": stat_grade,
            "surveillance": "전수감시",  # 통계 포털이 전수감시(1~3급)만 제공. 4급은 표본감시 별도.
            "matched": "Y" if law_hit else "N",
        })
        matched += bool(law_hit)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["disease", "law_grade", "stat_code",
                                          "stat_grade", "surveillance", "matched"])
        w.writeheader()
        w.writerows(rows)

    print(f"통계 {len(rows)}종 중 법 조문과 매칭 {matched}종 ({matched/len(rows)*100:.0f}%)")
    unmatched = [r["disease"] for r in rows if r["matched"] == "N"]
    if unmatched:
        print("미매칭:", unmatched)
    # 급수 정합: 통계 급수 == 법 급수 인지
    mism = [(r["disease"], r["stat_grade"], r["law_grade"]) for r in rows
            if r["matched"] == "Y" and r["stat_grade"] != r["law_grade"]]
    if mism:
        print("급수 불일치:", mism)
    print(f"→ {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
