"""법제처 국가법령정보 API로 감염병예방법 3종(법·시행령·시행규칙)을 받는다.

법령 원문은 재배포 대신 이 스크립트로 재현한다(출처: 법제처, 공공누리).
공개 저장소에서는 OC=test 대신 정식 OC 키를 환경변수로 준다.

MST(법령일련번호)는 lawSearch로 확인한 현행 기준:
  법     280445  |  시행령 285787  |  시행규칙 282387

용례:  LAW_OC=<정식키> python scripts/fetch_law.py   (미설정 시 test)
출력:  data/law/{idc_law,idc_enforce,idc_rule}.json
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/law"
OC = os.environ.get("LAW_OC", "test")   # 공개 전 정식 키로 교체
BASE = "https://www.law.go.kr/DRF/lawService.do"

TARGETS = {
    "idc_law": 280445,       # 감염병의 예방 및 관리에 관한 법률
    "idc_enforce": 285787,   # 시행령
    "idc_rule": 282387,      # 시행규칙
}


def fetch(mst: int) -> dict:
    url = f"{BASE}?OC={OC}&target=law&MST={mst}&type=JSON"
    req = urllib.request.Request(url, headers={"User-Agent": "research (repo)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, mst in TARGETS.items():
        d = fetch(mst)
        arts = d.get("법령", {}).get("조문", {}).get("조문단위", [])
        n = len(arts) if isinstance(arts, list) else 1
        (OUT / f"{name}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        print(f"{name}: 조문단위 {n} → {(OUT / f'{name}.json').relative_to(ROOT)}")
        time.sleep(1.0)   # 서버 예의
    print("출처: 법제처 국가법령정보 (공공누리). 재배포 시 출처 표기 필수.")


if __name__ == "__main__":
    main()
