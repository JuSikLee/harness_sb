"""main.py - 자산 진단 + 노후 계획 에이전트 진입점.

사용: python main.py --request "자산 5억, 50세, 60세 은퇴, 월소득 700, 월지출 400, 노후 월 300"
종료 코드: 0 성공 / 2 정보 부족(질문 출력) / 3 가드레일 차단 / 4 검증 실패·시간 초과

흐름: parse_intent(가드레일) -> load_profile -> portfolio_analyzer -> retirement_calculator
      -> gap_finder -> report_writer -> .docx 자체 검증 -> 메모리 갱신
운영 한도: 요청당 60초, LLM 토큰 40,000. 자동 거래 기능은 존재하지 않는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gap_finder  # noqa: E402
import portfolio_analyzer  # noqa: E402
import profile_loader  # noqa: E402
import report_writer  # noqa: E402
import retirement_calculator  # noqa: E402
from profile_loader import ASSET_CLASSES, UNCLASSIFIED, format_won  # noqa: E402

TIME_LIMIT_SEC = 60
TOKEN_LIMIT = 40_000
MAX_RETRY = 3


class VerificationError(RuntimeError):
    pass


def with_retry(name: str, fn, validate, tries: int = MAX_RETRY):
    """계산 -> 검증, 실패 시 해당 계산만 재호출(최대 3회)."""
    last = ""
    for _ in range(tries):
        result = fn()
        ok, msg = validate(result)
        if ok:
            return result
        last = msg
    raise VerificationError(f"{name} 검증 실패({tries}회 재시도): {last}")


# ── 검증기 (retry_policy.md) ─────────────────────────────────────────
def validate_portfolio(p: dict):
    wsum = sum(p["class_weights"].values())
    if wsum != 1000:  # permille 정수 -> 오차 0
        return False, f"자산군 비중 합계 {wsum / 10:.1f}% != 100%"
    if sum(r["rec_amount"] for r in p["rows"]) != p["total"]:
        return False, "권장 금액 합계가 총 자산과 불일치"
    if p["distribution_known"] and sum(r["current_amount"] for r in p["rows"]) != p["total"]:
        return False, "현재 보유 합계가 총 자산과 불일치"
    return True, "OK"


def validate_gap(g: dict, savable: int):
    if g["recommended_monthly"] > savable:
        return False, "월 저축 권장액이 월 가용 소득을 초과"
    if sum(a["amount"] for a in g["allocation"]) != g["recommended_monthly"]:
        return False, "월 저축 분배 합계가 권장액과 불일치"
    if sum(a["weight_pm"] for a in g["allocation"]) != 1000:
        return False, "월 저축 분배 비중 합계 != 100%"
    return True, "OK"


def verify_docx(path: Path, portfolio: dict) -> list[str]:
    """생성된 .docx 재검증: 면책 첫·마지막 줄, 가정값 섹션, 비중 합계 100%, 금지 고유명사."""
    from docx import Document

    doc = Document(str(path))
    fails: list[str] = []
    paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    if not paras or paras[0] != report_writer.DISCLAIMER:
        fails.append("첫 줄 면책 문구 누락")
    if not paras or paras[-1] != report_writer.DISCLAIMER:
        fails.append("마지막 줄 면책 문구 누락")
    headings = [p.text.strip() for p in doc.paragraphs if p.style.name.startswith("Heading")]
    if "가정값" not in headings:
        fails.append("'가정값' 섹션 없음")
    else:
        full = "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
        for key in ("물가상승률", "기대수익률", "노후 기간"):
            if key not in full:
                fails.append(f"가정값 항목 누락: {key}")
    pt = next((t for t in doc.tables if t.rows[0].cells[0].text == "자산군" and t.rows[0].cells[1].text == "권장 비중"), None)
    if pt is None:
        fails.append("권장 포트폴리오 표 없음")
    else:
        pcts = [float(r.cells[1].text.rstrip("%")) for r in pt.rows[1:-1]]
        total_cell = float(pt.rows[-1].cells[1].text.rstrip("%"))
        if abs(sum(pcts) - 100.0) > 0.1 or abs(total_cell - 100.0) > 0.1:
            fails.append(f"자산군 비중 합계 {sum(pcts):.1f}% != 100%")
    if len([t for t in doc.tables]) < 4:
        fails.append("필수 표 4개 미만")
    body = "\n".join(paras) + "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
    if profile_loader._PRODUCT_RE.search(body) or profile_loader._CODE_RE.search(body):
        fails.append("구체 상품·금융회사명 탐지")
    return fails


# ── 보조 ────────────────────────────────────────────────────────────
def _snapshot_compare(profile: dict, prev: dict | None) -> dict | None:
    if not prev:
        return None
    cur_known = profile["distribution_known"]
    if cur_known and prev.get("current_assets"):
        rows = []
        for cls in ASSET_CLASSES + [UNCLASSIFIED]:
            a, b = prev["current_assets"].get(cls, 0), profile["current_assets"].get(cls, 0)
            if a or b:
                rows.append([cls, format_won(a), format_won(b), ("+" if b - a >= 0 else "") + format_won(b - a)])
    else:
        rows = []
    a, b = prev["total_assets"], profile["total_assets"]
    rows.append(["총 자산", format_won(a), format_won(b), ("+" if b - a >= 0 else "") + format_won(b - a)])
    return {"date": prev["date"], "rows": rows}


def _assumption_change(assum: dict, gap_now: float, confirm: bool) -> tuple[str | None, bool]:
    """캐시된 가정값과 비교. 반환: (안내 문구, 캐시 갱신 여부)."""
    cache = profile_loader.load_recent_assumptions()
    if not cache:
        return None, True
    keys = ("profile", "inflation", "duration_years", "returns")
    diffs = [k for k in keys if cache.get(k) != assum[k]]
    if not diffs:
        return None, True
    msg = (f"가정값 변경 감지({', '.join(diffs)}): 이전 진단의 갭 {format_won(cache.get('last_gap', 0))} → 이번 {format_won(gap_now)} "
           f"(변동 {format_won(gap_now - cache.get('last_gap', 0))}). ")
    msg += "변경된 가정값을 확정하여 캐시를 갱신했습니다." if confirm else \
        "캐시는 갱신하지 않았습니다(확정하려면 --confirm-assumptions 옵션으로 다시 실행)."
    return msg, confirm


def _summary_lines(port: dict, ret: dict, gap: dict) -> list[str]:
    cw, bw = port["class_weights"], port["bucket_weights"]
    l1 = (f"1) 권장 포트폴리오: 주식 {bw['주식'] / 10:.1f}%(국내 {cw['국내 주식'] / 10:.1f}%·해외 {cw['해외 주식'] / 10:.1f}%) · "
          f"채권 {bw['채권'] / 10:.1f}% · 현금성 {bw['현금'] / 10:.1f}% · 대체 {bw['대체'] / 10:.1f}% (위험 성향 {port['risk']})")
    word = "부족" if ret["gap"] > 0 else "초과"
    l2 = (f"2) 노후 부족액: 필요 자금 {format_won(ret['need'])} - 예상 자산 {format_won(ret['expected'])} = "
          f"{word} {format_won(abs(ret['gap']))}")
    if gap["feasible"]:
        l3 = f"3) 월 저축 권장액: {format_won(gap['recommended_monthly'])} (월 가용 소득 {format_won(gap['savable'])} 이내, 갭 해소 가능)"
    else:
        need = "산출 불가" if gap["required_total_monthly"] is None else format_won(gap["required_total_monthly"])
        l3 = (f"3) 월 저축 권장액: {format_won(gap['recommended_monthly'])} (가용 소득 한도, 갭 해소에 필요한 월 저축 {need} → "
              "은퇴 연기·노후 지출 조정 시나리오는 보고서 참고)")
    return [l1, l2, l3]


def run(request: str, confirm_assumptions: bool = False) -> int:
    t0 = time.monotonic()

    def check_time(stage: str) -> None:
        if time.monotonic() - t0 > TIME_LIMIT_SEC:
            raise TimeoutError(f"시간 한도 {TIME_LIMIT_SEC}초 초과 ({stage})")

    # 0) 가드레일: LLM·계산 이전에 차단
    intent = profile_loader.parse_intent(request)
    if intent["blocked"]:
        print(intent["message"])
        print(f"(차단 사유: {intent['reason']})")
        return 3

    # 1) 프로필 파싱 (+메모리 보충)
    profile = profile_loader.load_profile(request, llm_timeout=min(profile_loader.LLM_TIMEOUT_SEC, TIME_LIMIT_SEC / 3))
    if profile["llm_tokens"] > TOKEN_LIMIT:
        raise VerificationError("토큰 한도(40,000) 초과")
    if profile["missing"]:
        print("추가 정보가 필요합니다. 아래 항목을 알려주세요:")
        for m in profile["missing"]:
            print(f" - {m}")
        return 2
    check_time("parse")

    today = dt.date.today()
    assum = portfolio_analyzer.get_assumptions()

    # 2~4) 계산 (검증 실패 시 해당 단계만 최대 3회 재시도)
    portfolio = with_retry("포트폴리오", lambda: portfolio_analyzer.analyze(profile, assum), validate_portfolio)
    check_time("portfolio")
    retirement = with_retry(
        "노후 계산", lambda: retirement_calculator.compute(profile, assum, portfolio["weighted_return"]),
        retirement_calculator.crosscheck)
    check_time("retirement")
    gap = with_retry("갭·저축", lambda: gap_finder.find_gap(profile, portfolio, retirement, assum),
                     lambda g: validate_gap(g, profile["monthly_savable"]))
    check_time("gap")

    change_msg, update_cache = _assumption_change(assum, retirement["gap"], confirm_assumptions)
    data = {
        "today": today, "profile": profile, "portfolio": portfolio, "retirement": retirement, "gap": gap,
        "assumptions": assum, "assumption_change": change_msg,
        "previous_snapshot_compare": _snapshot_compare(profile, profile_loader.load_previous_snapshot()),
    }

    # 5) 리포트 작성 + .docx 재검증 (실패 시 재생성 최대 3회)
    out_path = report_writer.next_output_path(today)
    for _ in range(MAX_RETRY):
        report_writer.write_report(data, out_path)
        fails = verify_docx(out_path, portfolio)
        if not fails:
            break
    else:
        raise VerificationError(f"리포트 검증 실패: {fails}")
    check_time("report")

    # 6) 메모리 갱신 (검증 통과 후)
    changed = profile_loader.save_profile(profile, today)
    profile_loader.save_goal_history(profile, today)
    profile_loader.save_asset_snapshot(profile, today)
    if update_cache:
        profile_loader.save_recent_assumptions({
            "profile": assum["profile"], "inflation": assum["inflation"], "duration_years": assum["duration_years"],
            "returns": assum["returns"], "last_gap": retirement["gap"], "updated": today.isoformat()})

    print(f"결과 파일: {out_path}")
    for line in _summary_lines(portfolio, retirement, gap):
        print(line)
    for w in portfolio["warnings"]:
        print(f"※ {w}")
    if changed:
        print(f"※ 프로필 변경 기록: {', '.join(changed)}")
    if change_msg:
        print(f"※ {change_msg}")
    print(f"(소요 {time.monotonic() - t0:.1f}초 / LLM 토큰 {profile['llm_tokens']})")
    print(report_writer.DISCLAIMER)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="개인 자산 진단 + 노후 계획 에이전트")
    ap.add_argument("--request", required=True, help="자연어 요청")
    ap.add_argument("--confirm-assumptions", action="store_true", help="변경된 가정값으로 캐시 갱신을 확정")
    args = ap.parse_args()
    try:
        return run(args.request, args.confirm_assumptions)
    except (VerificationError, TimeoutError) as e:
        print(f"처리 실패: {e}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
