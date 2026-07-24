"""정형 스토어 구축 — 수집 CSV + 조인 허브를 SQLite로.

SQL Agent가 화이트리스트로 접근하는 유일한 정형 DB. 스키마가 곧 계약이다.
읽기전용으로 열리므로 여기서 만든 테이블·컬럼만 질의 가능하다.

입력:
  data/raw/kdca_infectious_region.csv   year·grade·disease_code·disease·region·cases
  data/disease_hub.csv                  disease·law_grade·stat_code·stat_grade·surveillance·matched
출력:
  data/structured/kdca.db

용례:  python scripts/build_sqlite.py
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATS_CSV = ROOT / "data/raw/kdca_infectious_region.csv"
HUB_CSV = ROOT / "data/disease_hub.csv"
DB = ROOT / "data/structured/kdca.db"

SCHEMA = """
DROP TABLE IF EXISTS infectious_stats;
DROP TABLE IF EXISTS disease_hub;

-- 지역별 감염병 발생통계 (질병청 전수감시, 2015~2026)
-- region: 16개 시도 + '전국'(합계) + '기타'(검역 등 chart 미표시 지역).
--         집계 질의는 전국을 빼거나(region != '전국') 전국만 쓰거나 택일해야 이중집계를 피한다.
-- cases: 발생 건수. 0 = 발생 없음.
CREATE TABLE infectious_stats (
    year         INTEGER NOT NULL,
    grade        TEXT    NOT NULL,   -- 통계 급수코드 01/02/03 (전수감시)
    disease_code TEXT    NOT NULL,   -- 통계 질병코드 (조인 허브 키)
    disease      TEXT    NOT NULL,
    region       TEXT    NOT NULL,
    cases        INTEGER NOT NULL
);

-- 조인 허브: 통계 질병 ↔ 법정감염병 급수. COMPOSITE에서 통계와 법 조문을 잇는다.
CREATE TABLE disease_hub (
    disease      TEXT    NOT NULL,
    stat_code    TEXT    PRIMARY KEY,
    stat_grade   TEXT,               -- 통계 급수 1/2/3
    law_grade    TEXT,               -- 감염병예방법 제2조 급수 1~4 ('' = 법 열거 밖=고시지정)
    surveillance TEXT,               -- 전수감시 / 표본감시
    matched      TEXT                -- 법 조문 매칭 여부 Y/N
);

CREATE INDEX idx_stats_year    ON infectious_stats(year);
CREATE INDEX idx_stats_code    ON infectious_stats(disease_code);
CREATE INDEX idx_stats_region  ON infectious_stats(region);
CREATE INDEX idx_stats_disease ON infectious_stats(disease);
"""


def _rows(path: Path):
    with path.open(encoding="utf-8") as f:
        yield from csv.DictReader(f)


def main() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)

    stats = [(int(r["year"]), r["grade"], r["disease_code"], r["disease"],
              r["region"], int(r["cases"]))
             for r in _rows(STATS_CSV)]
    con.executemany("INSERT INTO infectious_stats VALUES (?,?,?,?,?,?)", stats)

    hub = [(r["disease"], r["stat_code"], r["stat_grade"], r["law_grade"],
            r["surveillance"], r["matched"]) for r in _rows(HUB_CSV)]
    con.executemany("INSERT INTO disease_hub VALUES (?,?,?,?,?,?)", hub)
    con.commit()

    # 검증: 이중집계 함정과 조인 무결성을 실제 쿼리로 확인한다
    cur = con.cursor()
    n_stats = cur.execute("SELECT COUNT(*) FROM infectious_stats").fetchone()[0]
    n_hub = cur.execute("SELECT COUNT(*) FROM disease_hub").fetchone()[0]

    # 전국 == 지역합 (수두 2023). region != '전국' AND region != '기타' 는 시도합, +기타 = 전국
    nat = cur.execute("SELECT cases FROM infectious_stats "
                      "WHERE disease='수두' AND year=2023 AND region='전국'").fetchone()[0]
    reg = cur.execute("SELECT SUM(cases) FROM infectious_stats "
                      "WHERE disease='수두' AND year=2023 AND region!='전국'").fetchone()[0]
    assert nat == reg, f"이중집계 검증 실패: 전국 {nat} != 지역합 {reg}"

    # 조인: 모든 통계 질병코드가 허브에 있는가
    orphan = cur.execute(
        "SELECT COUNT(DISTINCT s.disease_code) FROM infectious_stats s "
        "LEFT JOIN disease_hub h ON s.disease_code=h.stat_code WHERE h.stat_code IS NULL"
    ).fetchone()[0]
    assert orphan == 0, f"조인 실패: 허브에 없는 질병코드 {orphan}개"

    matched = cur.execute("SELECT COUNT(*) FROM disease_hub WHERE matched='Y'").fetchone()[0]

    # 샘플 COMPOSITE 소재: 2023 발생 상위 5
    top = cur.execute(
        "SELECT disease, cases FROM infectious_stats "
        "WHERE year=2023 AND region='전국' ORDER BY cases DESC LIMIT 5"
    ).fetchall()

    con.close()
    print(f"infectious_stats {n_stats}행 · disease_hub {n_hub}행 (법매칭 {matched})")
    print(f"이중집계 검증 PASS (수두2023 전국={nat}=지역합)")
    print(f"조인 무결성 PASS (고아 코드 0)")
    print(f"2023 발생 상위5(전국): {top}")
    print(f"→ {DB.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
