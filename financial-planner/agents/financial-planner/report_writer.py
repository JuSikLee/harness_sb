"""report_writer.py - output/financial-plan/{YYYYMMDD}_plan_{NN}.docx 작성 (python-docx).

규칙: 첫 줄·마지막 줄 면책 문구 / 첫 페이지 '가정값' 섹션 / 4개 표 필수 /
구체 상품명·종목코드·금융회사명·세금 단정 수치 금지. 모든 수치는 호출자가 파이썬으로 계산해 넘긴다.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt

from profile_loader import ROOT, format_won

DISCLAIMER = "본 자료는 정보 제공 목적이며 투자 권유나 재무 자문이 아닙니다. 정확한 자문은 면허 받은 재무설계사·세무사에게 문의하세요"
OUTPUT_DIR = ROOT / "output" / "financial-plan"
FONT = "맑은 고딕"
CITE_PORTFOLIO = "[portfolio_analyzer.py 결과]"
CITE_RETIREMENT = "[retirement_calculator.py 결과]"
CITE_GAP = "[gap_finder.py 결과]"


def next_output_path(today: dt.date, out_dir: Path = OUTPUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = today.strftime("%Y%m%d")
    existing = [int(p.stem.rsplit("_", 1)[1]) for p in out_dir.glob(f"{stamp}_plan_*.docx") if p.stem.rsplit("_", 1)[1].isdigit()]
    return out_dir / f"{stamp}_plan_{(max(existing) + 1 if existing else 1):02d}.docx"


def _pct(pm: int) -> str:
    return f"{pm / 10:.1f}%"


def _style_doc(doc: Document) -> None:
    st = doc.styles["Normal"]
    st.font.name = FONT
    st.font.size = Pt(10)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    for name in ("Title", "Heading 1", "Heading 2"):
        s = doc.styles[name]
        s.font.name = FONT
        s.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)


def _table(doc: Document, header: list[str], rows: list[list[str]], bold_last: bool = False) -> None:
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    for i, h in enumerate(header):
        cell = t.rows[0].cells[i]
        cell.text = ""
        cell.paragraphs[0].add_run(h).bold = True
    for ri, row in enumerate(rows):
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = ""
            run = cells[i].paragraphs[0].add_run(str(v))
            run.bold = bold_last and ri == len(rows) - 1
    doc.add_paragraph()


def _won(v: float | None) -> str:
    return "산출 불가" if v is None else format_won(v)


def write_report(data: dict, path: Path) -> Path:
    prof, port, ret, gap, assum = data["profile"], data["portfolio"], data["retirement"], data["gap"], data["assumptions"]
    today: dt.date = data["today"]
    doc = Document()
    _style_doc(doc)

    # ── 첫 줄: 면책 ──────────────────────────────────────────────
    p = doc.add_paragraph()
    p.add_run(DISCLAIMER).bold = True
    doc.add_heading("개인 자산 진단 및 노후 계획 보고서", level=0)
    doc.add_paragraph(f"작성일: {today.isoformat()}  |  자동 거래·상품 추천 없음 (자산군별 가이드만 제공)")

    # ── 가정값 (첫 페이지 의무) ───────────────────────────────────
    doc.add_heading("가정값", level=1)
    doc.add_paragraph(f"가정 프로필: {assum['profile']} (환경변수 ASSUMPTION_PROFILE로 보수/표준/낙관 전환)")
    rows = [
        ["물가상승률", f"연 {assum['inflation'] * 100:.1f}%", "노후 필요 자금의 인플레이션 누적계수에 사용"],
        ["노후 기간", f"{assum['duration_years']}년", f"{prof['retirement_age']}세 은퇴 시 {prof['retirement_age'] + assum['duration_years']}세까지"],
        ["은퇴까지 기간", f"{ret['years_to_retire']}년", f"{prof['age']}세 → {prof['retirement_age']}세"],
    ]
    for cls, r in assum["returns"].items():
        extra = " (인플레이션 헤지 가정)" if cls == "부동산" else ""
        rows.append([f"기대수익률 - {cls}", f"연 {r * 100:.1f}%", f"자산군 기대수익률{extra}"])
    rows.append(["가중평균 기대수익률", f"연 {port['weighted_return'] * 100:.3f}%", "권장 포트폴리오 비중 기준 (권장안으로 재배분했다고 가정)"])
    _table(doc, ["항목", "값", "비고"], rows)
    doc.add_paragraph(f"{CITE_RETIREMENT} {CITE_PORTFOLIO}")
    for s in (
        "단순화: 은퇴 후 운용수익·세금·공적연금 수령액은 반영하지 않은 보수적 단순 모형입니다.",
        "연금 계좌 자산은 채권형으로, '주식'·'채권'만 입력한 경우 국내 60:해외 40 / 국내 70:해외 30으로 나눈 것으로 가정합니다.",
        "대체 자산군은 부동산 50% / 기타 대체투자 50%로 나눕니다. 현재 월 저축액을 입력하지 않으면 0원으로 가정합니다.",
    ):
        doc.add_paragraph(s, style="List Bullet")
    if assum.get("note"):
        doc.add_paragraph(assum["note"])
    doc.add_heading("가정 변경 시 결과 변동", level=2)
    sens_rows = [[s["label"], _won(s["gap"]), ("+" if s["delta"] >= 0 else "") + format_won(s["delta"])] for s in ret["sensitivity"]]
    _table(doc, ["가정 변경", "노후 부족액(갭)", "기준 대비 변동"], sens_rows)
    doc.add_paragraph(f"{CITE_RETIREMENT} 가정값을 바꾸면 위와 같이 결과가 크게 달라질 수 있으므로 참고용으로만 보세요.")
    chg = data.get("assumption_change")
    if chg:
        doc.add_paragraph(chg)

    # ── 1. 입력 정보 ──────────────────────────────────────────────
    doc.add_heading("1. 입력 정보", level=1)
    src = prof["sources"]
    assets_txt = (" / ".join(f"{k} {format_won(v)}" for k, v in prof["current_assets"].items())
                  if prof["distribution_known"] else "자산군별 분포 미입력")
    in_rows = [
        ["현재 나이", f"{prof['age']}세", src.get("age", "")],
        ["은퇴 목표 나이", f"{prof['retirement_age']}세", src.get("retirement_age", "")],
        ["총 자산", format_won(prof["total_assets"]), src.get("total_assets", src.get("current_assets", ""))],
        ["자산 구성", assets_txt, src.get("current_assets", "")],
        ["월소득", _won(prof.get("monthly_income")) if prof.get("monthly_income") is not None else "-", src.get("monthly_income", "")],
        ["월지출", _won(prof.get("monthly_expense")) if prof.get("monthly_expense") is not None else "-", src.get("monthly_expense", "")],
        ["월 저축 가능액(월소득-월지출)", format_won(prof["monthly_savable"]), src.get("monthly_savable", "계산")],
        ["현재 월 저축액", format_won(prof["monthly_saving_current"]), src.get("monthly_saving_current", "")],
        ["노후 월 지출 목표(현재 가치)", format_won(prof["retirement_monthly_expense"]), src.get("retirement_monthly_expense", "")],
        ["위험 성향", prof["risk_tolerance"], src.get("risk_tolerance", "")],
    ]
    _table(doc, ["항목", "값", "출처"], in_rows)
    for n in prof.get("notes", []):
        doc.add_paragraph(n, style="List Bullet")

    # ── 2. 권장 포트폴리오 ────────────────────────────────────────
    doc.add_heading("2. 권장 포트폴리오 (자산군 비중 가이드)", level=1)
    p_rows = []
    for r in port["rows"]:
        diff = "" if not port["distribution_known"] else f" ({'+' if r['diff'] >= 0 else ''}{format_won(r['diff'])})"
        p_rows.append([r["asset_class"], _pct(r["weight_pm"]), format_won(r["rec_amount"]),
                       format_won(r["current_amount"]) if port["distribution_known"] else "-", r["direction"] + diff])
    p_rows.append(["합계", _pct(sum(r["weight_pm"] for r in port["rows"])), format_won(sum(r["rec_amount"] for r in port["rows"])),
                   format_won(sum(r["current_amount"] for r in port["rows"])) if port["distribution_known"] else "-", ""])
    _table(doc, ["자산군", "권장 비중", "권장 금액", "현재 보유", "조정 방향 (실제 거래 아님)"], p_rows, bold_last=True)
    doc.add_paragraph(f"{CITE_PORTFOLIO} 위험 성향 '{port['risk']}', 은퇴까지 {port['years_to_retire']}년 기준. "
                      "조정 방향은 비중 가이드이며 매수·매도 지시가 아닙니다.")
    for w in port["warnings"]:
        doc.add_paragraph(w, style="List Bullet")
    prev = data.get("previous_snapshot_compare")
    if prev:
        doc.add_heading("이전 진단 대비 자산군 변화", level=2)
        _table(doc, ["자산군", f"이전({prev['date']})", "이번", "변화"], prev["rows"])

    # ── 3. 자산군 내 분류 가이드 ───────────────────────────────────
    doc.add_heading("3. 자산군 내 분류 가이드 (일반론)", level=1)
    g_rows = [[g["asset_class"], format_won(g["amount"]), g["guide"], g["detail"]] for g in port["sub_guides"]]
    _table(doc, ["자산군", "권장 금액", "일반 분류 가이드", "세부 배분"], g_rows)
    doc.add_paragraph(f"{CITE_PORTFOLIO} 특정 펀드·종목·금융회사는 제시하지 않으며, 구체 상품 선택은 면허 받은 재무설계사와 상담하세요.")

    # ── 4. 월 저축 운영 + 노후 갭 분석 ─────────────────────────────
    doc.add_heading("4. 월 저축 운영 및 노후 갭 분석", level=1)
    doc.add_heading("월 저축 운영", level=2)
    a_rows = [[a["label"], _pct(a["weight_pm"]), format_won(a["amount"])] for a in gap["allocation"]]
    a_rows.append(["합계 (월 저축 권장액)", "100.0%", format_won(gap["recommended_monthly"])])
    _table(doc, ["배분 대상", "비중", "월 금액"], a_rows, bold_last=True)
    doc.add_paragraph(f"{CITE_GAP} 월 가용 소득 {format_won(gap['savable'])} 이내로 제한. 필요 월 저축액: "
                      f"{_won(gap['required_total_monthly'])}.")
    doc.add_heading("노후 갭 분석", level=2)
    g_rows2 = [
        ["노후 월 지출 목표(현재 가치)", format_won(prof["retirement_monthly_expense"])],
        ["인플레이션 누적계수", f"{ret['inflation_factor']:.4f} (물가 {assum['inflation'] * 100:.1f}% x {ret['years_to_retire']}년)"],
        ["노후 필요 자금(FV)", format_won(ret["need"])],
        ["현재 자산 {n}년 후 가치".format(n=ret["years_to_retire"]), format_won(ret["asset_fv"])],
        ["현재 월 저축 누적 가치", format_won(ret["saving_fv"])],
        ["예상 자산 합계", format_won(ret["expected"])],
        ["갭 (필요 자금 - 예상 자산)", ("부족 " if ret["gap"] > 0 else "초과 ") + format_won(abs(ret["gap"]))],
        ["갭 해소 추가 월 저축액", _won(ret["extra_monthly"])],
        ["월 저축 권장액 (가용 소득 이내)", format_won(gap["recommended_monthly"])],
        ["가용 소득 내 해소 가능 여부", "가능" if gap["feasible"] else "불가 - 아래 시나리오 검토"],
    ]
    _table(doc, ["항목", "값"], g_rows2)
    doc.add_paragraph(f"{CITE_RETIREMENT} {CITE_GAP}")
    for n in gap["notes"]:
        doc.add_paragraph(n, style="List Bullet")
    sc = gap["scenarios"]
    if sc["delay"]:
        doc.add_heading("시나리오 A: 은퇴 연기", level=2)
        d_rows = [[f"{d['years']}년 연기 ({d['retirement_age']}세)", format_won(d["need"]), format_won(d["expected"]),
                   ("부족 " if d["gap"] > 0 else "초과 ") + format_won(abs(d["gap"])), _won(d["required_total_monthly"]),
                   "가능" if d["feasible"] else "여전히 불가"] for d in sc["delay"]]
        _table(doc, ["시나리오", "필요 자금", "예상 자산", "갭", "필요 월 저축액", "가용 소득 내"], d_rows)
        s = sc["spending"]
        doc.add_heading("시나리오 B: 노후 지출 조정", level=2)
        _table(doc, ["항목", "값"], [
            ["목표 노후 월 지출(현재 가치)", format_won(s["target"])],
            [f"월 가용 소득 {format_won(gap['savable_cap'])} 전액 저축 시 감당 가능한 월 지출", format_won(s["affordable"])],
            ["필요 조정폭", f"{format_won(s['reduction'])} ({s['reduction_pct']:.1f}%)"],
        ])
        doc.add_paragraph(f"{CITE_GAP}")

    # ── 세금·연금·상속 안내 + 마지막 줄 면책 ───────────────────────
    doc.add_heading("세금·연금·상속 안내", level=1)
    doc.add_paragraph("세금·공적연금·상속·증여와 관련한 수치는 본 보고서에서 단정하지 않습니다. "
                      "세무사·면허 재무설계사에게 문의하세요.")
    doc.add_paragraph(DISCLAIMER).runs[0].bold = True  # 마지막 줄

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path
