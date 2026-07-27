"""논문 그림 생성 — risk–coverage 곡선과 거절 사유 분포.

trace JSONL만 읽는다(시스템 재실행 없음). 출력은 paper/figs/*.pdf.
UncertaiNLP 워크숍의 공용어가 risk–coverage이므로 이 곡선이 논문의 중심 그림이다.

용례:  python eval/make_figures.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rca.trace import read_traces  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "eval/qa/gold.jsonl"
RUNS = ROOT / "eval/runs"
FIGS = ROOT / "paper/figs"

# 논문용 기본 스타일 — 흑백 인쇄에도 구분되도록 마커·선종류를 다르게
plt.rcParams.update({
    "font.size": 9, "axes.labelsize": 9, "legend.fontsize": 8,
    "xtick.labelsize": 8, "ytick.labelsize": 8,
    "axes.spines.top": False, "axes.spines.right": False,
    "figure.dpi": 150,
})

STYLE = {
    "proposed": {"label": "Abstention ON (proposed)", "color": "#1f4e9c", "ls": "-", "marker": "o"},
    "no_abstain": {"label": "Abstention OFF", "color": "#c0392b", "ls": "--", "marker": "s"},
}


def load():
    gold = {}
    for line in GOLD.read_text(encoding="utf-8").splitlines():
        if line.strip():
            g = json.loads(line)
            gold[g["qid"]] = g
    rows, corrupt = read_traces(RUNS)
    if corrupt:
        print(f"⚠ 깨진 줄 {len(corrupt)}")
    by = defaultdict(list)
    for r in rows:
        if r.get("tool") == "_run":
            by[r["system"]].append(r)
    return gold, by


def would_be_correct(r, gold) -> bool:
    """이 질의에 **답했다면** 맞았을까 — answerability 기준(selective prediction 표준).

    답변불가 문항은 답하는 순간 오답이다(근거가 없다). 답변가능 문항은 답해도 된다.
    라우팅 정확도를 여기 섞지 않는 이유: 거절 OFF 조건은 라우터가 ABSTAIN을 낼 때
    DOCUMENT로 강제하므로 route_pred가 조건마다 달라진다. 그것까지 정답 정의에 넣으면
    "답할지 말지"의 품질이 아니라 라우팅 차이를 재게 된다. 라우팅은 route F1로 따로 본다.
    """
    return bool(gold[r["qid"]]["answerable"])


def answered_correct(r, gold) -> bool:
    """실제 동작점 채점: 답했고 맞았거나, 답변불가를 거절했거나."""
    g = gold[r["qid"]]
    if not g["answerable"]:
        return r.get("verdict") == "ABSTAIN"
    return r.get("verdict") == "ANSWER" and r.get("route_pred") == g["route_label"]


def risk_coverage(runs, gold):
    """신뢰도 내림차순으로 커버리지를 넓히며 위험(오답률)을 기록한다.

    AURC는 **커버리지 가중 평균**이다: 곡선 위 점을 그냥 평균하면 동점 블록이 클수록
    적게 세어져(블록 하나가 1표) 값이 왜곡된다. 표준 정의대로 예시 단위로 평균한다
    \\citep{geifman2019bias}. 동점 구간은 임계값으로 나눌 수 없으므로 한 덩어리로
    편입하고 구간 끝의 위험을 공유하되, 가중치는 구간 크기만큼 준다.
    """
    scored = sorted(((r.get("answer_confidence") or 0.0, would_be_correct(r, gold)) for r in runs),
                    key=lambda x: -x[0])
    cov, risk, per_example = [], [], []
    wrong = i = 0
    n = len(scored)
    while i < n:
        j = i
        while j < n and scored[j][0] == scored[i][0]:
            wrong += not scored[j][1]
            j += 1
        cov.append(j / n)
        risk.append(wrong / j)
        per_example += [wrong / j] * (j - i)   # 커버리지 가중
        i = j
    aurc = sum(per_example) / len(per_example) if per_example else 0.0
    return cov, risk, aurc


def fig_risk_coverage(gold, by):
    fig, ax = plt.subplots(figsize=(3.4, 2.5))
    for sysname in ("proposed", "no_abstain"):
        runs = by.get(sysname)
        if not runs:
            continue
        cov, risk, aurc = risk_coverage(runs, gold)
        st = STYLE[sysname]
        ax.plot(cov, risk, color=st["color"], ls=st["ls"], marker=st["marker"],
                ms=3, lw=1.4, label=f"{st['label']} (AURC={aurc:.3f})")
        # 시스템이 실제로 동작한 지점(operating point) 표시
        answered = [r for r in runs if r.get("verdict") == "ANSWER"]
        if answered:
            c = len(answered) / len(runs)
            e = sum(not would_be_correct(r, gold) for r in answered) / len(answered)
            ax.scatter([c], [e], color=st["color"], marker="*", s=90, zorder=5,
                       edgecolors="white", linewidths=0.6)
    ax.set_xlabel("Coverage")
    ax.set_ylabel("Risk (error rate)")
    ax.set_xlim(0, 1.02)
    ax.set_ylim(0, None)
    ax.legend(loc="upper left", frameon=False)
    ax.grid(alpha=0.25, lw=0.5)
    fig.tight_layout(pad=0.3)
    out = FIGS / "risk_coverage.pdf"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def fig_abstain_reasons(by):
    runs = by.get("proposed", [])
    dist = defaultdict(int)
    for r in runs:
        if r.get("abstain_reason"):
            dist[r["abstain_reason"]] += 1
    if not dist:
        return None
    items = sorted(dist.items(), key=lambda x: x[1])
    labels = [k.replace("_", "\n") for k, _ in items]
    vals = [v for _, v in items]
    fig, ax = plt.subplots(figsize=(3.4, 2.0))
    ax.barh(range(len(vals)), vals, color="#1f4e9c", height=0.6)
    ax.set_yticks(range(len(vals)))
    ax.set_yticklabels(labels, fontsize=7)
    ax.set_xlabel("Abstentions")
    for i, v in enumerate(vals):
        ax.text(v + 0.2, i, str(v), va="center", fontsize=7)
    ax.set_xlim(0, max(vals) * 1.15)
    ax.grid(axis="x", alpha=0.25, lw=0.5)
    fig.tight_layout(pad=0.3)
    out = FIGS / "abstain_reasons.pdf"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def main() -> None:
    FIGS.mkdir(parents=True, exist_ok=True)
    gold, by = load()
    if not by:
        sys.exit("trace 없음 — eval/run_eval.py 먼저")
    for name, runs in sorted(by.items()):
        cov, risk, aurc = risk_coverage(runs, gold)
        ans = [r for r in runs if r.get("verdict") == "ANSWER"]
        print(f"{name:11s} n={len(runs)} AURC={aurc:.3f} "
              f"operating(cov={len(ans)/len(runs):.3f}, "
              f"risk={sum(not would_be_correct(r, gold) for r in ans)/len(ans):.3f})")
    print("→", fig_risk_coverage(gold, by).relative_to(ROOT))
    r = fig_abstain_reasons(by)
    if r:
        print("→", r.relative_to(ROOT))


if __name__ == "__main__":
    main()
