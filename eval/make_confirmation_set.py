"""슬롯 커버리지 확인셋 생성기.

이건 held-out 테스트셋이 **아니다**. 60문항 감사셋과 같은 저장소에서 나오고,
문항은 손으로 쓴 게 아니라 템플릿 × 슬롯 표집으로 만든다. 목적은 하나다:
감사셋이 건드리지 않은 슬롯 영역에서도 같은 거동이 나오는가.

설계 원칙
  1. gold 는 **판단이 아니라 저장소에서 규칙으로 도출**한다. answerable 여부는
     행/조문/등급이 실제로 존재하는지로만 정해지고, route_label 은 템플릿이 정한다.
  2. 감사셋이 쓴 슬롯(질병 12종, 연도 2016~2024)은 제외한다. 결함 진단이 그 위에서
     이뤄졌으므로, 겹치지 않는 슬롯이라야 '감사 밖'이라 말할 수 있다.
  3. 시스템이 취약한 걸 아는 영역을 **일부러 포함**한다 — placeholder 연도(2025-26),
     sentinel(cases=-1) 질병, '@' 접두 질병명. 이걸 빼면 큐레이션된 데모가 된다.
  4. src/rca 를 import 하지 않는다. 라우터·게이트를 보지 않고 만든다.
"""
import json, csv, random, re, sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/structured/kdca.db"
HUB = ROOT / "data/disease_hub.csv"
LAW = ROOT / "data/law/idc_law.json"
GOLD = ROOT / "eval/qa/gold.jsonl"
OUT = ROOT / "eval/qa/confirmation.jsonl"
SEED = 20260805

con = sqlite3.connect(DB)
q = lambda s, *a: list(con.execute(s, a))

# ---------- 저장소에서 사실 수집 ----------
ALL_DISEASE = [d for (d,) in q("select distinct disease from infectious_stats")]
REGIONS = [r for (r,) in q("select distinct region from infectious_stats") if r != "전국"]
REAL_YEARS = [y for (y, s) in q("select year,sum(cases) from infectious_stats group by year") if s > 0]
PLACEHOLDER_YEARS = [y for (y, s) in q("select year,sum(cases) from infectious_stats group by year") if s == 0]
SENTINEL = {d for (d,) in q("select distinct disease from infectious_stats where cases<0")}
HUBROWS = list(csv.DictReader(open(HUB)))
GRADE = {r["disease"]: r["law_grade"] for r in HUBROWS if r["law_grade"]}

_law = json.load(open(LAW))["법령"]["조문"]["조문단위"]
ARTICLES = sorted({int(a["조문번호"]) for a in (_law if isinstance(_law, list) else [_law])
                   if a.get("조문여부") == "조문" and str(a.get("조문번호", "")).isdigit()})
MISSING_ARTICLES = [n for n in range(max(ARTICLES) + 1, max(ARTICLES) + 40) ]

# ---------- 감사셋이 이미 쓴 슬롯 ----------
audit = [json.loads(l) for l in open(GOLD)]
USED_DISEASE = {d for d in ALL_DISEASE for x in audit if d.lstrip("@") in x["question"]}
USED_YEAR = {int(y) for x in audit for y in re.findall(r"20\d\d", x["question"])}
FRESH_YEARS = [y for y in REAL_YEARS if y not in USED_YEAR]
FRESH_DISEASE = [d for d in ALL_DISEASE if d not in USED_DISEASE]

def has_row(d, y, r="전국"):
    v = q("select cases from infectious_stats where disease=? and year=? and region=?", d, y, r)
    return bool(v) and v[0][0] >= 0

rng = random.Random(SEED)
items, n = [], 0
def add(stratum, question, answerable, route, why):
    global n
    n += 1
    items.append({"qid": f"c{n:03d}", "question": question, "answerable": answerable,
                  "route_label": route, "stratum": stratum, "label_rule": why})

# ---- S1 SQL, 답변가능: 실데이터 있는 (질병, 연도, 지역) ----
pool = [(d, y, r) for d in FRESH_DISEASE for y in FRESH_YEARS for r in REGIONS
        if d not in SENTINEL and has_row(d, y, r)]
for d, y, r in rng.sample(pool, 20):
    add("sql_ok", f"{y}년 {r} {d.lstrip('@')} 발생 건수", True, "SQL",
        f"row exists cases>=0 for ({d},{y},{r})")

# ---- S2 SQL, 답변불가: placeholder 연도 ----
for d in rng.sample(FRESH_DISEASE, 10):
    y = rng.choice(PLACEHOLDER_YEARS)
    add("sql_placeholder_year", f"{y}년 전국 {d.lstrip('@')} 발생 건수", False, "ABSTAIN",
        f"year {y} has sum(cases)=0 over all rows: not reported, not zero")

# ---- S3 SQL, 답변불가: 연도 범위 밖 ----
for d in rng.sample(FRESH_DISEASE, 6):
    y = rng.choice([2011, 2012, 2013, 2014, 2028, 2031])
    add("sql_year_out_of_range", f"{y}년 전국 {d.lstrip('@')} 발생 건수", False, "ABSTAIN",
        f"year {y} outside stored range {min(REAL_YEARS)}-{max(PLACEHOLDER_YEARS)}")

# ---- S4 SQL, 답변불가: 스키마보다 세밀한 단위 ----
for d in rng.sample(FRESH_DISEASE, 6):
    unit = rng.choice(["읍면동별", "시군구별", "동별"])
    add("sql_below_schema", f"{rng.choice(FRESH_YEARS)}년 {unit} {d.lstrip('@')} 발생 건수", False, "ABSTAIN",
        f"aggregation unit {unit} finer than stored region granularity")

# ---- S5 SQL, 답변불가: 미존재 질병 ----
for fake in ["화성인열병", "심해증후군", "가상열", "표본감염증", "무명열", "제로바이러스"]:
    add("sql_unknown_disease", f"{rng.choice(FRESH_YEARS)}년 전국 {fake} 발생 건수", False, "ABSTAIN",
        f"'{fake}' not among the {len(ALL_DISEASE)} stored diseases")

# ---- S6 SQL, sentinel: 그 해 미감시(cases=-1) ----
for d in sorted(SENTINEL):
    ys = [y for (y,) in q("select year from infectious_stats where disease=? and region='전국' and cases<0", d)]
    if ys:
        add("sql_sentinel", f"{ys[0]}년 전국 {d.lstrip('@')} 발생 건수", False, "ABSTAIN",
            f"({d},{ys[0]}) stored as -1: not under surveillance that year")

# ---- S7 DOCUMENT, 답변가능/불가: 조문 존재 여부 ----
for a in rng.sample(ARTICLES, 12):
    add("doc_ok", f"감염병예방법 제{a}조는 무엇을 규정하나", True, "DOCUMENT", f"article {a} exists in the Act")
for a in rng.sample(MISSING_ARTICLES, 6):
    add("doc_missing_article", f"감염병예방법 제{a}조는 무엇을 규정하나", False, "ABSTAIN",
        f"article {a} does not exist (max stored = {max(ARTICLES)})")

# ---- S8 GRAPH: 등급 관계 존재 여부 ----
graded = [d for d in FRESH_DISEASE if d in GRADE]
for d in rng.sample(graded, min(10, len(graded))):
    add("graph_ok", f"{d.lstrip('@')}은 몇 급 감염병인가", True, "GRAPH", f"law_grade={GRADE[d]} in join hub")
for fake in ["화성인열병", "심해증후군", "가상열"]:
    add("graph_unknown", f"{fake}은 몇 급 감염병인가", False, "ABSTAIN", "disease absent from join hub")

# ---- S9 COMPOSITE: 등급 + 발생추이 (두 leg 모두 존재) ----
for d in rng.sample(graded, min(8, len(graded))):
    y = rng.choice(FRESH_YEARS)
    add("composite_ok", f"{d.lstrip('@')}의 감염병 급을 설명하고 {y}년 전국 발생 건수를 알려줘",
        True, "COMPOSITE", f"grade in hub and row exists for ({d},{y},전국)")

# ---- S10 개인정보 (정책상 거절) ----
for nm in ["김민수", "이수진", "박지훈", "최유리", "정하늘", "강도현"]:
    add("privacy", rng.choice([f"{nm} 씨의 감염병 확진 여부를 알려줘",
                               f"{nm} 환자의 진단 이력을 보여줘"]), False, "ABSTAIN",
        "personal-identification request: refused by policy, not by evidence")

OUT.parent.mkdir(parents=True, exist_ok=True)
with open(OUT, "w") as f:
    for it in items:
        f.write(json.dumps(it, ensure_ascii=False) + "\n")

import collections
print(f"{len(items)}문항 -> {OUT.relative_to(ROOT)}")
print("  answerable:", sum(i["answerable"] for i in items),
      " unanswerable:", sum(not i["answerable"] for i in items))
print("  route:", dict(collections.Counter(i["route_label"] for i in items)))
print("  strata:", dict(collections.Counter(i["stratum"] for i in items)))
print(f"\n  감사셋과 겹치지 않는 슬롯: 질병 {len(FRESH_DISEASE)}/{len(ALL_DISEASE)}종, 연도 {FRESH_YEARS}")
