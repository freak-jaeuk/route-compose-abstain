"""SQL Agent — 정형 스토어 질의. 자연어를 바로 SQL로 만들지 않는다.

흐름:  NL → QuerySpec → 스키마·값 검증 → 파라미터 바인딩 SQL → 읽기전용 실행 → 결과

보안이 축소 불가 영역이다 (ARCHITECTURE §10). 사용자 입력이 DB에 닿는 유일한 지점.
- 읽기전용 커넥션(mode=ro) — 쓰기 물리적으로 불가
- SELECT만 코드가 생성 — 자유 SQL 문자열 실행 경로 없음
- 값은 100% 파라미터 바인딩 — 문자열 연결 없음
- 테이블·컬럼·집계함수는 화이트리스트에서만
- 개인 식별 질의 → PRIVACY_RESTRICTED (이 DB엔 개인정보 컬럼이 없다)
- 범위 밖 질병/지역/연도 → OUT_OF_SCHEMA

QuerySpec을 채우는 NL 파서는 규칙 기반(v0.1) → LLM(추후)으로 교체되지만,
검증·빌드·실행 코어는 그대로다. 어느 파서든 QuerySpec만 넘기면 된다.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "data/structured/kdca.db"

# ── 화이트리스트 (스키마가 곧 계약) ─────────────────────────────────
TABLE = "infectious_stats"
COLS = {"year", "grade", "disease_code", "disease", "region", "cases"}
AGG = {"sum", "by_year", "by_region", "raw"}
YEAR_MIN, YEAR_MAX = 2015, 2026
AGG_REGIONS = {"전국", "기타"}          # 집계행. 시도 목록과 섞으면 이중집계
MAX_REGIONS = 20                       # 지역 카디널리티(18) 상한. 폭주 방어

# 개인 식별 질의 차단 패턴. 이 DB는 집계 통계뿐이라 개인 조회는 스키마 밖이자 PII 위반.
PII_PATTERNS = [
    re.compile(r"[가-힣]{2,4}\s*(씨|님|환자|씨의|님의)"),                 # 이름 + 호칭
    re.compile(r"(주민등록번호|주민번호|연락처|전화번호|주소|이름|성명)"),
    re.compile(r"개인\s*(정보|식별|이력)"),
    # 개인 판정 질의(이름 + 확진/양성/감염 + 여부/받). '확진자 통계'처럼 집계어가 뒤따르면 안 걸린다.
    re.compile(r"[가-힣]{2,4}\s*(씨|님|이|가)?\s*(확진|양성|감염)\s*(여부|됐|되었|인지|받)"),
]


class SqlError(Exception):
    """reason 코드를 실은 도구 실패. 오케스트레이터가 abstain 사유로 매핑한다."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


class QuerySpec(BaseModel):
    """검증된 질의 의도. LLM/규칙 파서가 채우고, 빌더가 SQL로 옮긴다."""

    disease: str | None = None          # 질병명 (정확 일치, 검증됨)
    grade: str | None = None            # 급수 01/02/03
    regions: list[str] = Field(default_factory=list)  # 시도. 빈=전국 한 줄
    year_from: int | None = None
    year_to: int | None = None
    agg: Literal["sum", "by_year", "by_region", "raw"] = "sum"


# ── 도메인 값 로딩 (검증용, DB에서 1회) ──────────────────────────────
def _domain(db: Path = DB) -> dict[str, set]:
    con = _connect(db)
    try:
        cur = con.cursor()
        return {
            "disease": {r[0] for r in cur.execute(f"SELECT DISTINCT disease FROM {TABLE}")},
            "region": {r[0] for r in cur.execute(f"SELECT DISTINCT region FROM {TABLE}")},
            "grade": {r[0] for r in cur.execute(f"SELECT DISTINCT grade FROM {TABLE}")},
        }
    finally:
        con.close()


def _connect(db: Path = DB) -> sqlite3.Connection:
    """읽기전용 커넥션. 쓰기 시도는 OperationalError로 물리 차단된다."""
    if not db.exists():
        raise SqlError("SQL_EXECUTION_FAILURE", f"DB 없음: {db}")
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


# ── 검증 ────────────────────────────────────────────────────────────
def guard_pii(question: str) -> None:
    for pat in PII_PATTERNS:
        if pat.search(question):
            raise SqlError("PRIVACY_RESTRICTED", "개인 식별 질의는 처리하지 않는다")


def validate(spec: QuerySpec, domain: dict | None = None) -> QuerySpec:
    dom = domain or _domain()
    if spec.disease is not None and spec.disease not in dom["disease"]:
        raise SqlError("OUT_OF_SCHEMA", f"질병 '{spec.disease}' 없음")
    if spec.grade is not None and spec.grade not in dom["grade"]:
        raise SqlError("OUT_OF_SCHEMA", f"급수 '{spec.grade}' 없음")
    if len(spec.regions) > MAX_REGIONS:
        raise SqlError("OUT_OF_SCHEMA", f"지역 {len(spec.regions)}개 (최대 {MAX_REGIONS})")
    for r in spec.regions:
        if r not in dom["region"]:
            raise SqlError("OUT_OF_SCHEMA", f"지역 '{r}' 없음 (시도 단위만 지원)")
        if r in AGG_REGIONS:
            # 전국은 지역 미지정으로, 기타는 집계행. 시도 목록에 섞으면 sum이 이중집계된다.
            raise SqlError("OUT_OF_SCHEMA", f"'{r}'는 시도 목록에 넣을 수 없음 (집계행)")
    for y in (spec.year_from, spec.year_to):
        if y is not None and not (YEAR_MIN <= y <= YEAR_MAX):
            raise SqlError("OUT_OF_SCHEMA", f"연도 {y} 범위 밖 ({YEAR_MIN}~{YEAR_MAX})")
    if spec.agg not in AGG:
        raise SqlError("OUT_OF_SCHEMA", f"집계 '{spec.agg}' 미지원")
    return spec


# ── SQL 빌더 (값은 100% 바인딩, 구조는 화이트리스트) ──────────────────
def build(spec: QuerySpec) -> tuple[str, list]:
    where, params = [], []
    if spec.disease:
        where.append("disease = ?"); params.append(spec.disease)
    if spec.grade:
        where.append("grade = ?"); params.append(spec.grade)
    if spec.regions:
        where.append(f"region IN ({','.join('?' * len(spec.regions))})")
        params += spec.regions
    elif spec.agg != "by_region":
        # 지역 미지정 = 전국 한 줄. 시도합과 전국을 섞으면 이중집계.
        # by_region은 시도별로 쪼개는 게 목적이라 전국 필터를 붙이지 않는다(아래서 전국·기타 제외).
        where.append("region = ?"); params.append("전국")
    if spec.year_from is not None:
        where.append("year >= ?"); params.append(spec.year_from)
    if spec.year_to is not None:
        where.append("year <= ?"); params.append(spec.year_to)
    w = (" WHERE " + " AND ".join(where)) if where else ""

    if spec.agg == "sum":
        sql = f"SELECT SUM(cases) AS cases FROM {TABLE}{w}"
    elif spec.agg == "by_year":
        sql = f"SELECT year, SUM(cases) AS cases FROM {TABLE}{w} GROUP BY year ORDER BY year"
    elif spec.agg == "by_region":
        # by_region은 시도 비교가 목적이므로 전국·기타 합계행을 제외한다
        w2 = w + (" AND " if w else " WHERE ") + "region NOT IN ('전국','기타')"
        sql = f"SELECT region, SUM(cases) AS cases FROM {TABLE}{w2} GROUP BY region ORDER BY cases DESC"
    else:  # raw
        sql = f"SELECT year, region, disease, cases FROM {TABLE}{w} ORDER BY year, region LIMIT 500"
    return sql, params


def run(spec: QuerySpec, db: Path = DB) -> dict:
    """spec을 검증 후 실행. 결과 dict 반환. 실패는 SqlError로.

    validate를 여기서 다시 호출한다 — run이 단독 호출돼도 화이트리스트를 우회할 수 없게
    (defense-in-depth). answer()를 거치지 않는 내부 경로도 안전하다.
    """
    validate(spec, _domain(db))
    sql, params = build(spec)
    if not sql.lstrip().upper().startswith("SELECT") or ";" in sql:
        raise SqlError("SQL_EXECUTION_FAILURE", "비SELECT/다중문 차단")  # 방어적: 빌더가 보장하나 이중확인
    con = _connect(db)
    try:
        cur = con.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except sqlite3.Error as e:
        raise SqlError("SQL_EXECUTION_FAILURE", str(e)) from e
    finally:
        con.close()
    return {"sql": sql, "params": params, "rows": rows, "row_count": len(rows),
            "source_id": TABLE}


def answer(question: str, spec: QuerySpec, db: Path = DB) -> dict:
    """도구 진입점: PII 차단 → 검증 → 실행. 오케스트레이터가 호출한다."""
    guard_pii(question)
    validate(spec, _domain(db))
    return run(spec, db)


if __name__ == "__main__":
    # 보안 경계 자체 점검 — 축소 불가 영역이므로 여기만 제대로 검증한다
    dom = {"disease": {"수두"}, "region": {"서울", "전국"}, "grade": {"02"}}

    # 1) 정상 집계
    s = validate(QuerySpec(disease="수두", year_from=2021, year_to=2023, agg="by_year"), dom)
    sql, p = build(s)
    assert sql.startswith("SELECT year, SUM(cases)") and "?" in sql
    assert "수두" in p and 2021 in p, "값은 바인딩으로 전달"

    # 2) SQL 인젝션 시도 → 값이 바인딩되어 무해 (문자열에 안 섞임)
    inj = build(QuerySpec(disease="수두'; DROP TABLE infectious_stats;--"))
    assert "DROP" not in inj[0], "인젝션 문자열이 SQL 본문에 없어야"
    assert inj[0].count("?") == len(inj[1]), "모든 값이 placeholder"

    # 3) 화이트리스트 밖 값 → OUT_OF_SCHEMA (결핵은 법엔 있으나 이 통계 범위 밖)
    dom2 = {"disease": {"수두"}, "region": {"서울", "부산", "전국", "기타"}, "grade": {"02"}}
    for bad in [QuerySpec(disease="코로나"), QuerySpec(disease="결핵"),
                QuerySpec(regions=["읍면동"]), QuerySpec(regions=["강남구"]),
                QuerySpec(year_from=2030), QuerySpec(grade="09"),
                QuerySpec(regions=["전국", "서울"]),      # 집계행+시도 혼합 → 이중집계 차단
                QuerySpec(regions=["기타"]),               # 집계행 단독도 금지
                QuerySpec(regions=["서울"] * 21)]:          # 폭주 방어
        try:
            validate(bad, dom2); assert False, f"통과하면 안 됨: {bad}"
        except SqlError as e:
            assert e.reason == "OUT_OF_SCHEMA", f"{bad} → {e.reason}"

    # 4) PII 질의 → PRIVACY_RESTRICTED (호칭 있는 것 + 없는 것)
    for q in ["김철수 씨의 감염 이력", "환자 이름과 주민등록번호", "이영희님의 진단",
              "김철수 확진 여부", "홍길동 감염 여부", "박영수가 양성인지"]:
        try:
            guard_pii(q); assert False, f"PII 통과: {q}"
        except SqlError as e:
            assert e.reason == "PRIVACY_RESTRICTED"

    # 5) 정상 질의는 PII 게이트 통과
    guard_pii("2023년 수두 발생 건수")

    # 6) 빌더는 SELECT만 생성
    for agg in AGG:
        sql, _ = build(QuerySpec(disease="수두", agg=agg))
        assert sql.lstrip().upper().startswith("SELECT") and ";" not in sql

    print("sql.py 보안 경계 자체 점검 OK")
