"""문서 스토어 청킹 — 감염병예방법 JSON을 조문 단위 청크로.

법령은 조·항·호·목이 JSON 계층으로 오므로 청킹은 파싱이 아니라 필드 매핑이다.
단, 실제 구조에는 함정이 있어 워크플로우 감사가 지적한 3가지를 처리한다:
  - 조문가지번호: 제8조와 제8조의2가 같은 키로 충돌하지 않게 (56개 존재)
  - 항/호/목이 dict 또는 list로 혼재 → 정규화
  - 항내용이 빈 문자열이고 호만 있는 조문(제2조 정의) → 호부터 이어붙임
  - 조문여부 != '조문'인 장 제목·전문(12개)은 제외

조 단위 1청크. 단 조가 너무 길면(제2조 정의는 88종 나열) 호 단위로 분할한다.

입력:  data/law/idc_law.json
출력:  data/documents/law_chunks.jsonl
       {chunk_id, law, article_no, article_title, effective_date, text, n_chars}

용례:  python scripts/build_document_chunks.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAW = ROOT / "data/law/idc_law.json"
OUT = ROOT / "data/documents/law_chunks.jsonl"

MAX_CHARS = 1200        # 이보다 길면 호 단위로 쪼갠다


def _as_list(x) -> list:
    """법제처 응답은 항목이 1개면 dict, 여럿이면 list로 준다. list로 통일."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _clean(s: str) -> str:
    """선행 공백과 <개정 ...>·<신설 ...> 마커를 정리한다."""
    s = re.sub(r"<(개정|신설|본조신설|본항신설)[^>]*>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def article_id(a: dict) -> tuple[str, str]:
    """(조 식별자, 표시명). 가지번호를 반영해 제8조의2를 제8조와 구분한다."""
    no = a.get("조문번호", "")
    branch = a.get("조문가지번호")
    key = f"{no}-{branch}" if branch else no
    label = f"제{no}조의{branch}" if branch else f"제{no}조"
    return key, label


def ho_texts(article: dict) -> list[str]:
    """조문의 호(1. 2. …) 단위 텍스트 목록. 각 호는 하위 목(가. 나. …)까지 이어붙인다."""
    out = []
    for hang in _as_list(article.get("항")):
        for ho in _as_list(hang.get("호")):
            parts = [_clean(str(ho.get("호내용", "")))]
            parts += [_clean(str(m.get("목내용", ""))) for m in _as_list(ho.get("목"))]
            t = " ".join(p for p in parts if p)
            if t:
                out.append(t)
    return out


def full_text(article: dict) -> str:
    """조문내용(헤더) + 항내용 + 호/목 전체를 한 덩어리로."""
    parts = [_clean(str(article.get("조문내용", "")))]
    for hang in _as_list(article.get("항")):
        parts.append(_clean(str(hang.get("항내용", ""))))
    parts += ho_texts(article)
    return " ".join(p for p in parts if p)


def main() -> None:
    d = json.loads(LAW.read_text(encoding="utf-8"))
    law = d["법령"]
    law_name = law.get("기본정보", {}).get("법령명_한글") or "감염병의 예방 및 관리에 관한 법률"
    eff = str(law.get("기본정보", {}).get("시행일자", ""))
    arts = _as_list(law["조문"]["조문단위"])

    chunks = []
    for a in arts:
        if a.get("조문여부") != "조문":       # 장 제목·전문 제외
            continue
        key, label = article_id(a)
        title = _clean(str(a.get("조문제목", "")))
        eff_a = str(a.get("조문시행일자", eff))
        text = full_text(a)
        if not text:
            continue

        base = {"law": law_name, "article_no": label, "article_title": title,
                "effective_date": eff_a}
        if len(text) <= MAX_CHARS:
            chunks.append({**base, "chunk_id": f"idc:{key}", "text": text,
                           "n_chars": len(text)})
        else:
            # 긴 조문(정의 등)은 헤더 + 호 단위로 분할. 각 청크에 조 헤더를 붙여 문맥 보존.
            header = f"{label}({title})" if title else label
            for i, ht in enumerate(ho_texts(a), 1):
                t = f"{header} {ht}"
                chunks.append({**base, "chunk_id": f"idc:{key}#{i}", "text": t,
                               "n_chars": len(t)})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    lens = [c["n_chars"] for c in chunks]
    n_art = len({c["article_no"] for c in chunks})
    print(f"{len(chunks)}청크 / {n_art}개 조문 (분할 조문 포함)")
    print(f"청크 길이: min {min(lens)} · 중앙 {sorted(lens)[len(lens)//2]} · max {max(lens)}")
    print(f"가지번호 조문 예: {[c['article_no'] for c in chunks if '의' in c['article_no']][:3]}")
    print(f"→ {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
