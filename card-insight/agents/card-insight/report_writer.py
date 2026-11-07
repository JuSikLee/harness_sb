"""리포트 .docx 작성 + 재독 검증 (architecture.md §6)."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt

from csv_loader import PROJECT_ROOT

OUTPUT_DIR = PROJECT_ROOT / "output" / "card-report"
DISCLAIMER = (
    "본 리포트는 입력된 카드 내역에 대한 통계적 요약이며 개인 재무 자문이 아닙니다. "
    "특정 금융상품·카드·대출의 추천을 포함하지 않으며, 지출 조정 여부는 사용자의 판단에 따릅니다. "
    "분석은 로컬에서 수행되었고, 외부 AI에는 익명화·반올림된 요약 수치만 사용되었습니다."
)
FONT = "맑은 고딕"


def next_output_path(today: dt.date | None = None) -> Path:
    today = today or dt.date.today()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = today.strftime("%Y%m%d")
    n = 1
    while (OUTPUT_DIR / f"{stamp}_card_report_{n:02d}.docx").exists():
        n += 1
    return OUTPUT_DIR / f"{stamp}_card_report_{n:02d}.docx"


def _won(v) -> str:
    return f"{int(round(v)):,}원"


def _growth_text(g, basis: str) -> str:
    return "-" if g is None else f"{g:+.1f}% ({basis})"


def _table(doc, header: list[str], rows: list[list[str]]):
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    for i, h in enumerate(header):
        t.rows[0].cells[i].text = h
        for r in t.rows[0].cells[i].paragraphs[0].runs:
            r.bold = True
    for row in rows:
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = v
    return t


def write_report(stats: dict, advice: dict, intent: dict, meta: dict, path: Path | None = None) -> Path:
    path = path or next_output_path()
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = FONT
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)

    y, m = stats["month"][:4], int(stats["month"][5:])
    doc.add_paragraph(DISCLAIMER)  # 면책 (첫 단락)
    doc.add_heading(f"카드 사용 내역 분석 리포트 - {y}년 {m}월", 0)

    basis = stats["growth_basis"]
    bl = stats["base_label"]
    doc.add_heading("1. 요약", 1)
    doc.add_paragraph(f"총 사용액: {_won(stats['total'])} ({stats['count']}건)")
    doc.add_paragraph(f"평균: 일평균 {_won(stats['daily_avg'])} · 건당 {_won(stats['avg_per_txn'])} "
                      f"(자료 {stats['coverage_days']}일 기준)")
    if stats["overall_growth"] is not None:
        doc.add_paragraph(f"{bl} 대비: {stats['overall_growth']:+.1f}% ({basis} 기준, {bl} 총 {_won(stats['prev_total'])}, "
                          f"자료 {stats['prev_coverage_days']}일)")
        if basis == "일평균":
            doc.add_paragraph("※ 두 달의 자료 일수가 달라 증가율은 일평균으로 비교했습니다.")
    else:
        doc.add_paragraph(f"{bl} 대비: {bl} 자료가 없어 산출하지 못했습니다.")
    if meta["cancel_rows"]:
        doc.add_paragraph(f"※ 취소/취소전표 {meta['cancel_rows']}건(합계 {_won(meta['cancel_amount'])})은 집계에서 제외했습니다.")
    if intent["topic"]:
        doc.add_paragraph(f"강조 토픽: {intent['topic']}")

    doc.add_heading("2. 카테고리별 지출", 1)
    cats = sorted(stats["categories"].items(), key=lambda kv: kv[1]["sum"], reverse=True)
    rows = [[c, _won(v["sum"]), f"{v['share']:.1f}%", _growth_text(v["growth"], basis), f"{v['count']}건"] for c, v in cats]
    rows.append(["합계", _won(stats["total"]), "100.0%", _growth_text(stats["overall_growth"], basis), f"{stats['count']}건"])
    _table(doc, ["카테고리", "금액", "비중", f"{bl} 대비", "건수"], rows)

    doc.add_heading("3. 주 사용처 Top 5", 1)
    rows = []
    for i, mt in enumerate(stats["top_merchants"], 1):
        note = ""
        if mt["flag_1m"]:
            if mt["prev_sum"] is None:
                note = f"월 100만 원 이상 - {bl} 자료 없음(동일 패턴 여부 확인 불가)"
            elif mt["prev_sum"] >= 1_000_000:
                note = f"월 100만 원 이상 - {bl}에도 {_won(mt['prev_sum'])}로 동일 패턴"
            else:
                note = f"월 100만 원 이상 - {bl}은 {_won(mt['prev_sum'])}로 이번 달만 높음"
        rows.append([str(i), mt["merchant"], _won(mt["sum"]), f"{mt['count']}건", mt["category"], note])
    _table(doc, ["순위", "사용처", "금액", "건수", "카테고리", "비고"], rows)

    doc.add_heading("4. 과소비 Top 3", 1)
    doc.add_paragraph(f"선정 기준: {stats['top3_criterion']}")
    if not stats["top3"]:
        doc.add_paragraph("기준을 충족하는 카테고리가 없습니다.")
    for i, t in enumerate(stats["top3"], 1):
        parts = [f"{t['category']} - 이번 달 {_won(t['sum'])} (비중 {t['share']:.1f}%)"]
        if t["growth"] is not None:
            parts.append(f"{bl} 대비 {t['growth']:+.1f}% ({basis} 기준), {bl} {_won(t['prev_sum'])}")
        if t["z"] is not None:
            parts.append(f"자가 평균 대비 z-score {t['z']:+.2f}")
        doc.add_paragraph(f"{i}. " + " / ".join(parts) + ". 검토 권장.")

    doc.add_heading("5. 절약 가능 영역", 1)
    for it in advice["items"]:
        doc.add_paragraph(f"[{it['category']}] {it['text']}")
        doc.add_paragraph(f"  근거: {it['evidence']}")
        doc.add_paragraph(f"  추정: 줄이면 월 약 {_won(it['saving'])} 절감 ({it['saving_basis']} 기준)")
    if advice["items"]:
        doc.add_paragraph(f"영역별 절감 추정 합계(Top 3 기준): 월 약 {_won(sum(i['saving'] for i in advice['items'] if i['category'] in [t['category'] for t in stats['top3']]))}")

    doc.add_paragraph(DISCLAIMER)  # 면책 (마지막 단락)
    doc.save(path)
    return path


def verify_report(path: Path, stats: dict) -> list[str]:
    """생성된 .docx 를 다시 열어 점검. 문제 목록 반환(빈 리스트 = 통과)."""
    problems = []
    doc = Document(path)
    paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    if paras[0] != DISCLAIMER:
        problems.append("첫 단락이 면책 문구가 아님")
    if paras[-1] != DISCLAIMER:
        problems.append("마지막 단락이 면책 문구가 아님")
    table = doc.tables[0]  # 카테고리별 표 (첫 번째 표)
    nums = []
    for row in table.rows[1:]:
        label, amount = row.cells[0].text, row.cells[1].text
        v = int(re.sub(r"[^\d]", "", amount))
        nums.append((label, v))
    body = sum(v for k, v in nums if k != "합계")
    total_row = [v for k, v in nums if k == "합계"][0]
    if abs(body - stats["total"]) > stats["total"] * 0.001 or total_row != stats["total"]:
        problems.append(f"카테고리 합계({body:,}) ≠ 총 사용액({stats['total']:,})")
    return problems
