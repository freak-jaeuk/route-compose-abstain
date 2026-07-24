"""질병관리청 감염병포털 전수감시 지역별 발생통계 수집.

data.go.kr 15053802는 실제 파일이 아니라 감염병포털(dportal.kdca.go.kr) 링크만 제공한다.
실데이터는 포털의 chart Ajax(`selectSimpleAreaStatsChartListAjax.do`)가 JSON으로 준다.
xaxis=지역, yaxis2=발생수. 전국 합계는 list Ajax의 DATAARRTXT[0]에서 가져온다.

서버 예의: 요청 간 delay(기본 1.2초), 순차. 병렬·폭주 금지.
재실행 안전: 이미 받은 (질병,연도)는 건너뛴다.

출력: data/raw/kdca_infectious_region.csv
  컬럼: year, grade, disease_code, disease, region, cases

용례:
  python scripts/fetch_kdca.py                # 2015~2026 전량
  python scripts/fetch_kdca.py --years 2020 2023
  python scripts/fetch_kdca.py --smoke        # 2급 3종 × 2년만 (배선 점검)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/raw/kdca_infectious_region.csv"
CODE_GRADE = ROOT / "data/raw/code_grade.json"  # {code: {grade, name}}

BASE = "https://dportal.kdca.go.kr/pot/is"
LIST_EP = f"{BASE}/selectSimpleAreaStatsListAjax.do"
CHART_EP = f"{BASE}/selectSimpleAreaStatsChartListAjax.do"
RGIN = f"{BASE}/rgin.do"
FIELDS = ["year", "grade", "disease_code", "disease", "region", "cases"]


def _opener() -> urllib.request.OpenerDirector:
    """세션 쿠키를 들고 다니는 opener. 포털이 첫 방문 쿠키를 요구한다."""
    from http.cookiejar import CookieJar

    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    op.addheaders = [
        ("User-Agent", "Mozilla/5.0 (research; contact via repo)"),
        ("X-Requested-With", "XMLHttpRequest"),
        ("Referer", RGIN),
    ]
    op.open(RGIN, timeout=30).read()  # 쿠키 획득
    return op


def _post(op, url: str, params: dict, retries: int = 3) -> dict:
    body = urllib.parse.urlencode(params).encode()
    last = None
    for attempt in range(retries):
        try:
            with op.open(urllib.request.Request(url, data=body), timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - 네트워크는 재시도
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"POST 실패 {url} {params}: {last}")


def _norm_cases(v) -> str:
    """발생수 정규화. chart는 float(3135.0), list는 정수문자열, '-'는 신고대상 아님(보존).

    '-' 와 '0' 은 의미가 다르다 (신고대상 아님 vs 발생 없음) — 섞지 않는다.
    """
    if v is None:
        return ""
    s = str(v).strip().replace(",", "")
    if s in ("-", ""):
        return "-"
    return str(int(float(s)))  # "3135.0" → "3135"


def fetch_one(op, code: str, grade: str, year: int) -> list[dict]:
    """한 (질병, 연도)의 지역별 발생수. chart=지역별, list=전국 합계."""
    p = {"frmNm": "simpleAreaFrm", "searchType": "1",
         "icdgrpCd": grade, "icdCd": code, "startDt": str(year)}

    chart = _post(op, CHART_EP, p)
    series = chart.get("value") or []
    if not series:
        return []
    disease = series[0].get("label", "")
    # chart는 지역 라벨을 명시적으로 준다(서울~세종, 전남광주). 단 검역 등 일부 지역이 빠져
    # 지역합 < 전국일 수 있으므로, 차액을 "기타"로 묶어 지역합 == 전국 정합성을 보존한다.
    region_rows = [{"year": year, "grade": grade, "disease_code": code, "disease": disease,
                    "region": pt["xaxis"], "cases": _norm_cases(pt.get("yaxis2"))}
                   for pt in series[0].get("value", [])]

    lst = _post(op, LIST_EP, p)
    data = (lst.get("value") or {}).get("data") or []
    nat_raw = data[0].get("DATAARRTXT", "").split("`")[0] if data else None
    nat = _norm_cases(nat_raw)

    rows = [{"year": year, "grade": grade, "disease_code": code, "disease": disease,
             "region": "전국", "cases": nat}] if nat not in ("", "-") else []
    rows += region_rows

    # 기타 = 전국 - 지역합 (chart에서 누락된 지역; 값 있을 때만)
    if nat not in ("", "-"):
        reg_sum = sum(int(r["cases"]) for r in region_rows if r["cases"] not in ("-", ""))
        etc = int(nat) - reg_sum
        if etc > 0:
            rows.append({"year": year, "grade": grade, "disease_code": code,
                         "disease": disease, "region": "기타", "cases": str(etc)})
    return rows


def load_codes() -> dict:
    if not CODE_GRADE.exists():
        sys.exit(f"코드 매핑 없음: {CODE_GRADE}\n먼저 포털 옵션을 code_grade.json으로 저장하라.")
    return json.loads(CODE_GRADE.read_text(encoding="utf-8"))


def done_keys() -> set:
    """이미 수집된 (disease_code, year) — 재실행 시 건너뛴다."""
    if not OUT.exists():
        return set()
    with OUT.open(encoding="utf-8") as f:
        return {(r["disease_code"], r["year"]) for r in csv.DictReader(f)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=list(range(2015, 2027)))
    ap.add_argument("--delay", type=float, default=1.2, help="요청 간 초 (서버 예의)")
    ap.add_argument("--smoke", action="store_true", help="2급 3종 × 최근 2년만")
    args = ap.parse_args()

    codes = load_codes()
    years = args.years
    if args.smoke:
        codes = {c: v for c, v in codes.items() if v["grade"] == "02"}
        codes = dict(list(codes.items())[:3])
        years = [2022, 2023]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    done = done_keys()
    todo = [(c, v, y) for c, v in codes.items() for y in years
            if (c, str(y)) not in done]
    total = len(todo)
    print(f"수집 대상 {total}건 (질병 {len(codes)} × 연도 {len(years)}, 이미 {len(done)}건 완료)")
    if not total:
        print("모두 수집됨.")
        return

    op = _opener()
    write_header = not OUT.exists()
    ok = 0
    with OUT.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        for i, (code, v, year) in enumerate(todo, 1):
            try:
                rows = fetch_one(op, code, v["grade"], year)
                w.writerows(rows)
                f.flush()
                ok += 1
                if i % 20 == 0 or i == total:
                    print(f"  [{i}/{total}] {v['name']} {year} → {len(rows)}행 (누적 {ok})")
            except Exception as e:  # noqa: BLE001 - 한 건 실패가 전체를 막지 않는다
                print(f"  ! 실패 {v['name']} {year}: {e}", file=sys.stderr)
            time.sleep(args.delay)
    print(f"완료: {ok}/{total}건 수집 → {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
