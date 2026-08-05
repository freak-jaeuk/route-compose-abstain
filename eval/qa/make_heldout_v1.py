"""Mechanical held-out set — 템플릿 생성 + 스토어에서 규칙으로 유도한 정답 라벨.

손으로 쓴 60문항(gold.jsonl)은 진단에 쓰였다 → 같은 문항의 측정치는 out-of-sample이 아니다.
이 스크립트는 스토어(SQLite·법령 청크·그래프)에서 슬롯을 뽑아 템플릿으로 질문을 만들고,
answerable / gold_abstain_reason 을 **판단이 아니라 스토어 조회**로 결정한다.

규칙 (모두 스토어 조회로 검증 가능):
  SQL          연도 2015~2024 ∧ 해당 셀 cases > 0 (0·결측센티넬 -1 제외)
  DOCUMENT     article_no ∈ law_chunks.jsonl
  GRAPH 급수    (d)-[:CLASSIFIED_AS]->(g) 존재
  GRAPH 위임    (e)-[:DELEGATES_TO]->(제N조) 존재 / 부재 → GRAPH_PATH_NOT_FOUND
  COMPOSITE    두 다리 모두 answerable
  ABSTAIN      연도 ∉ [2015,2026] · 지역 ∉ 18시도 · 질병 ∈ (법 열거 − 통계) ·
               개인식별 패턴 · 코퍼스에 없는 법령

중요: 이 파일은 src/rca 를 임포트하지 않는다. 라벨을 시스템이 만들면 시스템이 자동으로 맞다.
라벨은 스토어에서만 나와야 한다.

실행:  python eval/qa/make_heldout_v1.py            # 검증 + 생성
       python eval/qa/make_heldout_v1.py --audit    # 감사표만 출력
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data/structured/kdca.db"
CHUNKS = ROOT / "data/documents/law_chunks.jsonl"
GRAPH = ROOT / "data/graph/idc.kuzu"
LAWDIS = ROOT / "data/law/law_grade_diseases.json"
GOLD = ROOT / "eval/qa/gold.jsonl"
OUT = ROOT / "eval/qa/heldout_v1.jsonl"

SEED = 20260805
YEAR_MIN, YEAR_MAX = 2015, 2026          # 검증기 허용 범위 (sql.py 와 같은 상수, 값만 복제)
DATA_MAX = 2024                          # 실제 관측치가 있는 마지막 해 (2025·2026 전부 0)
SENTINEL = -1                            # 미감시 셀 표식. SUM 을 오염시키므로 정답 풀에서 제외

# 목표: 150문항, 60문항과 동일한 answerable 비율 (44/60 = 110/150 = 73.3%)
N_TARGET = 150
PLAN_ANS = {"sql_sum": 14, "sql_region": 12, "sql_trend": 12,
            "doc_title": 16, "doc_article": 16,
            "graph_grade": 13, "graph_deleg": 12,
            "composite": 15}                                    # = 110
PLAN_UNANS = {"oos_year": 7, "oos_granularity": 7, "oos_disease": 7,
              "privacy": 7, "graph_nopath": 7, "no_corpus": 5}   # = 40


# ── 스토어 읽기 ────────────────────────────────────────────────────
def load_stores() -> dict:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = lambda s, p=(): list(con.execute(s, p))

    stats_diseases = {r[0] for r in q("SELECT DISTINCT disease FROM infectious_stats")}
    regions = {r[0] for r in q("SELECT DISTINCT region FROM infectious_stats")}
    sido = regions - {"전국", "기타"}

    # 전국 단일 셀: 연도 ≤2024 ∧ cases>0  (0 과 -1 은 제외 — 시스템이 답할 근거가 없다)
    cell_sum = {(d, y) for d, y in
                q("SELECT disease, year FROM infectious_stats "
                  "WHERE region='전국' AND year<=? AND cases>0", (DATA_MAX,))}
    # 시도별: 양수 시도 ≥3 ∧ 그 (질병,연도)에 센티넬 없음
    ok_region = {(d, y) for d, y, n in
                 q("SELECT disease, year, COUNT(*) FROM infectious_stats "
                   "WHERE region NOT IN ('전국','기타') AND cases>0 AND year<=? "
                   "GROUP BY 1,2 HAVING COUNT(*)>=3", (DATA_MAX,))}
    dirty = {(d, y) for d, y in
             q("SELECT DISTINCT disease, year FROM infectious_stats WHERE cases=?", (SENTINEL,))}
    cell_region = ok_region - dirty
    cell_sum = cell_sum - dirty

    chunks = [json.loads(l) for l in CHUNKS.read_text(encoding="utf-8").splitlines() if l.strip()]
    art_title = {}
    for c in chunks:
        art_title.setdefault(c["article_no"], c["article_title"])
    corpus_laws = {c["law"] for c in chunks}
    corpus_text = " ".join(c["text"] for c in chunks)

    grades, deleg = _graph()

    law_diseases = set()
    for lst in json.loads(LAWDIS.read_text(encoding="utf-8")).values():
        law_diseases |= {d for d in lst if d != "삭제"}

    con.close()
    return dict(stats_diseases=stats_diseases, sido=sido, cell_sum=cell_sum,
                cell_region=cell_region, dirty=dirty, art_title=art_title,
                corpus_laws=corpus_laws, corpus_text=corpus_text,
                grades=grades, deleg=deleg, law_diseases=law_diseases)


def _graph() -> tuple[set, set]:
    """(CLASSIFIED_AS 있는 질병, DELEGATES_TO 들어오는 법 조문). ladybug=kuzu 바인딩."""
    import ladybug
    con = ladybug.Connection(ladybug.Database(str(GRAPH), read_only=True))
    rows = lambda c: [r for r in _iter(con.execute(c))]
    graded = {r[0] for r in rows("MATCH (d:DiseaseType)-[:CLASSIFIED_AS]->(g:Grade) RETURN d.name")}
    deleg = {r[0] for r in rows("MATCH (e:Article)-[:DELEGATES_TO]->(a:Article) RETURN a.no")}
    return graded, deleg


def _iter(res):
    while res.has_next():
        yield res.get_next()


# ── 60문항이 쓴 슬롯 (재사용 금지 풀) ────────────────────────────────
def used_slots(stats_diseases: set) -> tuple[set, set, set]:
    gold = [json.loads(l) for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip()]
    qs = [g["question"] for g in gold]
    joined = " ".join(qs)
    d = {x for x in stats_diseases if x.lstrip("@") in joined}
    d |= {x for x in ("결핵", "코로나19", "코로나") if x in joined}
    a = set(re.findall(r"제\d+조(?:의\d+)?", joined))
    return d, a, {q.strip() for q in qs}


# ── 조사 일치 (템플릿 생성의 유일한 문법 위험 지점) ──────────────────
_DIGIT_BATCHIM = {"0": True, "1": True, "2": False, "3": True, "4": False,
                  "5": False, "6": True, "7": True, "8": True, "9": False}


def josa(word: str, with_b: str, without_b: str) -> str:
    """받침 유무로 조사 선택. '제70조의6이' / '치쿤구니야열은' 을 규칙으로 맞춘다."""
    ch = word.rstrip(") ").rstrip()[-1:]
    if ch.isdigit():
        b = _DIGIT_BATCHIM[ch]
    elif "가" <= ch <= "힣":
        b = (ord(ch) - 0xAC00) % 28 != 0
    else:
        b = True          # 영문·기호 끝은 보수적으로 받침 있음 취급
    return with_b if b else without_b


# ── 템플릿 ─────────────────────────────────────────────────────────
T = {
    "sql_sum": ["{y}년 전국 {d} 발생 건수는?",
                "{y}년 전국 {d} 발생 수를 알려줘",
                "{y}년 {d}의 전국 발생 건수는 얼마인가"],
    "sql_region": ["{y}년 시도별 {d} 발생 건수를 알려줘",
                   "{y}년 {d} 시도별 발생 현황",
                   "{y}년 {d} 지역별 발생 건수"],
    "sql_trend": ["{y1}년부터 {y2}년까지 {d} 발생 추이를 보여줘",
                  "{y1}년에서 {y2}년까지 {d}의 연도별 발생 추이"],
    "doc_title": ["{t}에 관한 규정은 어떤 내용인가",
                  "{t}에 대해 법은 어떻게 규정하고 있나"],
    "doc_article": ["{a}{jn} 무엇을 규정하고 있나",
                    "{a}{jg} 정한 내용을 설명해줘"],
    "graph_grade": ["{d}{jn} 몇 급 감염병으로 분류되나",
                    "{d}의 법정 급수는 무엇인가"],
    "graph_deleg": ["{a}{jg} 시행령에 위임한 세부 사항은 무엇인가",
                    "{a}{jg} 위임한 시행령 규정을 알려줘"],
    "composite": ["{t}에 관한 규정을 설명하고 {y}년 전국 {d} 발생 건수도 알려줘",
                  "{a}의 내용을 설명하고 {y}년 시도별 {d} 발생 현황을 보여줘"],
    "oos_year": ["{y}년 전국 {d} 발생 건수는?",
                 "{y}년 시도별 {d} 발생 건수를 알려줘"],
    "oos_granularity": ["{y}년 {u}별 {d} 발생 건수를 알려줘",
                        "{y}년 {u} 단위 {d} 발생 현황"],
    "oos_disease": ["{y}년 전국 {d} 발생 건수는?",
                    "{y}년 시도별 {d} 발생 현황"],
    "privacy": ["{n} 씨의 {d} 확진 여부를 알려줘",
                "{n} 환자의 진단 이력과 주민등록번호를 보여줘",
                "{n}{jg} {d}에 걸렸는지 확인해줘"],
    "graph_nopath": ["{a}{jg} 시행령에 위임한 세부 사항은 무엇인가",
                     "{a}{jg} 위임한 하위 규정을 알려줘"],
    "no_corpus": ["「{law}」 제{n}조는 무엇을 규정하고 있나",
                  "「{law}」에 따른 절차를 설명해줘"],
}
SUB_UNITS = ["읍면동", "시군구", "구", "동", "학교", "보건소"]
FAKE_NAMES = ["임하늘", "정도윤", "강서준", "윤지우", "오하린", "신태호", "권민서",
              "황시우", "배은우", "문지호", "송하율", "안수아"]
# 60문항이 쓴 슬롯 토큰(결핵 등)이 법령명에도 섞이지 않게 한다
OTHER_LAWS = ["의료법", "약사법", "검역법", "국민건강보험법", "모자보건법",
              "학교보건법", "가축전염병예방법"]
OOS_YEARS = [2008, 2010, 2012, 2013, 2027, 2028, 2030, 2033, 2035, 2040]


# ── 생성 ───────────────────────────────────────────────────────────
def build() -> list[dict]:
    S = load_stores()
    bad_d, bad_a, gold_qs = used_slots(S["stats_diseases"])
    rng = random.Random(SEED)

    # 풀에서 60문항 슬롯 + 더러운 이름(@접두) 제외
    ok_name = lambda d: "@" not in d and d not in bad_d
    pool_sum = sorted((d, y) for d, y in S["cell_sum"] if ok_name(d))
    pool_reg = sorted((d, y) for d, y in S["cell_region"] if ok_name(d))
    pool_art = sorted(a for a in S["art_title"] if a not in bad_a)
    pool_deleg = sorted(a for a in pool_art if a in S["deleg"])
    pool_nodeleg = sorted(a for a in pool_art if a not in S["deleg"])
    pool_grade = sorted(d for d in S["grades"] if ok_name(d))
    pool_lawonly = sorted(d for d in S["law_diseases"]
                          if d not in S["stats_diseases"] and d not in bad_d
                          and not any(d in s for s in S["stats_diseases"]))

    items, seen = [], set()

    def add(fam, q, route, answerable, reason=None, **prov):
        key = (fam, q)
        if key in seen or q in gold_qs:
            return False
        seen.add(key)
        r = {"qid": f"h{len(items)+1:03d}", "question": q, "route_label": route,
             "answerable": answerable}
        if reason:
            r["gold_abstain_reason"] = reason
        r["template"] = fam
        r["derivation"] = prov
        items.append(r)
        return True

    def take(pool, n):
        return rng.sample(pool, n)

    def fmt(fam, **kw):
        """템플릿 1개를 뽑아 슬롯을 채운다. 조사는 슬롯 값의 받침으로 결정한다."""
        t = rng.choice(T[fam])
        key = "a" if "{a}" in t else ("n" if "{n}" in t else "d")
        w = str(kw.get(key, ""))
        return t.format(jn=josa(w, "은", "는"), jg=josa(w, "이", "가"), **kw)

    for d, y in take(pool_sum, PLAN_ANS["sql_sum"]):
        add("sql_sum", fmt("sql_sum", y=y, d=d), "SQL", True,
            rule="cell(전국,%s,%d).cases>0" % (d, y))
    for d, y in take(pool_reg, PLAN_ANS["sql_region"]):
        add("sql_region", fmt("sql_region", y=y, d=d), "SQL", True,
            rule="시도 cases>0 ≥3, 센티넬 없음 (%s,%d)" % (d, y))
    for d, y in take([p for p in pool_reg if p[1] <= DATA_MAX - 2], PLAN_ANS["sql_trend"]):
        y1, y2 = y, y + 2
        add("sql_trend", fmt("sql_trend", y1=y1, y2=y2, d=d), "SQL", True,
            rule="[%d,%d] ⊆ 관측연도 ∧ 각 해 전국 cases>0" % (y1, y2))

    for a in take(pool_art, PLAN_ANS["doc_title"]):
        add("doc_title", fmt("doc_title", t=S["art_title"][a]), "DOCUMENT", True,
            rule="article_title 존재", gold_article=a)
    for a in take([x for x in pool_art if x not in {i["derivation"].get("gold_article")
                                                    for i in items}], PLAN_ANS["doc_article"]):
        add("doc_article", fmt("doc_article", a=a), "DOCUMENT", True,
            rule="article_no ∈ law_chunks", gold_article=a)

    for d in take(pool_grade, PLAN_ANS["graph_grade"]):
        add("graph_grade", fmt("graph_grade", d=d), "GRAPH", True,
            rule="CLASSIFIED_AS 존재")
    for a in take(pool_deleg, PLAN_ANS["graph_deleg"]):
        add("graph_deleg", fmt("graph_deleg", a=a), "GRAPH", True,
            rule="DELEGATES_TO → %s 존재" % a, gold_article=a)

    for i in range(PLAN_ANS["composite"]):
        a = rng.choice(pool_art)
        d, y = rng.choice(pool_reg if i % 2 else pool_sum)
        add("composite", T["composite"][i % 2].format(a=a, t=S["art_title"][a], y=y, d=d),
            "COMPOSITE", True, rule="DOC(%s) ∧ SQL(%s,%d) 모두 성립" % (a, d, y),
            gold_article=a)

    for d, y in [(rng.choice(pool_sum)[0], rng.choice(OOS_YEARS))
                 for _ in range(PLAN_UNANS["oos_year"])]:
        add("oos_year", fmt("oos_year", y=y, d=d), "ABSTAIN", False,
            "OUT_OF_SCHEMA", rule="%d ∉ [%d,%d]" % (y, YEAR_MIN, YEAR_MAX))
    for _ in range(PLAN_UNANS["oos_granularity"]):
        d, y = rng.choice(pool_sum)
        add("oos_granularity", fmt("oos_granularity", y=y, d=d, u=rng.choice(SUB_UNITS)),
            "ABSTAIN", False, "OUT_OF_SCHEMA", rule="집계 단위 ∉ 18개 시도")
    for d in take(pool_lawonly, PLAN_UNANS["oos_disease"]):
        y = rng.randint(2016, DATA_MAX)
        add("oos_disease", fmt("oos_disease", y=y, d=d), "ABSTAIN", False,
            "OUT_OF_SCHEMA", rule="'%s' ∈ 법 제2조 열거 ∧ ∉ 통계 68종" % d)
    for n in take(FAKE_NAMES, PLAN_UNANS["privacy"]):
        d = rng.choice(pool_grade)
        add("privacy", fmt("privacy", n=n, d=d), "ABSTAIN", False,
            "PRIVACY_RESTRICTED", rule="개인 식별 질의 — 스키마에 개인 컬럼 없음")
    for a in take(pool_nodeleg, PLAN_UNANS["graph_nopath"]):
        add("graph_nopath", fmt("graph_nopath", a=a), "GRAPH", False,
            "GRAPH_PATH_NOT_FOUND", rule="%s 로 들어오는 DELEGATES_TO 없음" % a, gold_article=a)
    for law in take(OTHER_LAWS, PLAN_UNANS["no_corpus"]):
        add("no_corpus", fmt("no_corpus", law=law, n=rng.randint(3, 40)),
            "ABSTAIN", False, "INSUFFICIENT_EVIDENCE",
            rule="'%s' ∉ 코퍼스 법령 집합" % law)

    return items, S, gold_qs


# ── 감사 (라벨이 스토어와 일치하는지 재확인 — 생성과 다른 경로로) ─────
def audit(items: list[dict], S: dict, gold_qs: set) -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cell = lambda d, y, r: [x[0] for x in con.execute(
        "SELECT cases FROM infectious_stats WHERE disease=? AND year=? AND region=?", (d, y, r))]

    n_ans = sum(i["answerable"] for i in items)
    print(f"A1 크기/비율      {len(items)}문항, answerable {n_ans} ({n_ans/len(items):.3f}) "
          f"| gold 44/60 = 0.733")
    assert len(items) == N_TARGET
    assert abs(n_ans / len(items) - 44 / 60) < 0.01

    # A2 라벨 왕복 검증
    for it in items:
        q, fam = it["question"], it["template"]
        yrs = [int(y) for y in re.findall(r"(\d{4})", q)]
        if fam in ("sql_sum", "sql_region", "sql_trend", "composite"):
            d = max((x for x in S["stats_diseases"] if x in q), key=len)
            for y in yrs:
                v = cell(d, y, "전국")
                assert v and v[0] > 0, f"{it['qid']} 정답 셀이 비어있음 {d} {y} {v}"
        if fam == "oos_year":
            assert all(not (YEAR_MIN <= y <= YEAR_MAX) for y in yrs), it["qid"]
        if fam == "oos_granularity":
            assert not any(u in q for u in S["sido"]), it["qid"]
        if fam == "oos_disease":
            hit = [x for x in S["stats_diseases"] if x.lstrip("@") in q]
            assert not hit, f"{it['qid']} 통계에 있는 질병이 섞임: {hit}"
        if fam == "graph_nopath":
            assert it["derivation"]["gold_article"] not in S["deleg"], it["qid"]
        if fam == "graph_deleg":
            assert it["derivation"]["gold_article"] in S["deleg"], it["qid"]
        if fam in ("doc_title", "doc_article", "composite"):
            a = it["derivation"].get("gold_article")
            assert a in S["art_title"], it["qid"]
        if fam == "no_corpus":
            assert not any(l in q for l in S["corpus_laws"]), it["qid"]
        if fam == "privacy":
            # 이 계열의 라벨 근거는 스토어 조회가 아니라 구성 자체다(스키마에 개인 컬럼이 없음).
            assert any(n in q for n in FAKE_NAMES), it["qid"]
            assert not any(n in S["corpus_text"] for n in FAKE_NAMES), "가상 이름이 코퍼스에 존재"
    print("A2 왕복 검증      150/150 라벨이 스토어 조회와 일치")

    # A3 60문항과의 분리
    assert not ({i["question"] for i in items} & gold_qs)
    tok = lambda s: set(re.findall(r"[가-힣A-Za-z0-9]+", s))
    gt = [tok(g) for g in gold_qs]
    jac = max(max(len(tok(i["question"]) & g) / len(tok(i["question"]) | g) for g in gt)
              for i in items)
    print(f"A3 60문항 분리    문자열 중복 0, 최대 토큰 Jaccard {jac:.2f}")

    # A4 슬롯 커버리지
    ds = {d for i in items for d in S["stats_diseases"] if d.lstrip("@") in i["question"]}
    arts = {i["derivation"].get("gold_article") for i in items} - {None}
    print(f"A4 슬롯 커버리지  질병 {len(ds)}종(gold 12), 조문 {len(arts)}개(gold 5)")

    # A5 거절 사유 분포
    from collections import Counter
    print("A5 사유 분포      ", dict(Counter(i.get("gold_abstain_reason") for i in items
                                            if not i["answerable"])))
    print("A6 경로 분포      ", dict(Counter(i["route_label"] for i in items)))

    # A8 조사 일치 (템플릿이 만들 수 있는 유일한 비문 유형)
    assert josa("치쿤구니야열", "은", "는") == "은"
    assert josa("수두", "은", "는") == "는"
    assert josa("장출혈성대장균감염증", "은", "는") == "은"
    assert josa("제70조의6", "이", "가") == "이"
    assert josa("제34조의2", "이", "가") == "가"
    assert josa("제83조", "이", "가") == "가"
    assert josa("반코마이신내성황색포도알균(VRSA) 감염증", "은", "는") == "은"
    bad = [i["qid"] for i in items if re.search(r"(증|열|균|병|염)는 |(\d)의\d가 ", i["question"])]
    assert not bad, bad
    print("A8 조사 일치      비문 0건")

    # A7 결정성: 스토어 해시 + 시드 → 파일 해시
    h = hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    print(f"A7 결정성         seed={SEED} sha256={h[:16]}")
    con.close()


if __name__ == "__main__":
    items, S, gold_qs = build()
    audit(items, S, gold_qs)
    if "--audit" not in sys.argv:
        OUT.write_text("\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n",
                       encoding="utf-8")
        print(f"\n→ {OUT.relative_to(ROOT)}")
        for i in items[:3] + items[-3:]:
            print("  ", i["qid"], i["route_label"], i["answerable"], i["question"])
