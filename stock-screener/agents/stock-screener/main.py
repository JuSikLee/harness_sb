#!/usr/bin/env python3
"""주식 종목 추천 에이전트 진입점.

사용: python3 main.py --request "오늘 추천 10개"
추천만 하고 거래는 실행하지 않는다. (specs/guardrails.md · architecture.md 적용)
"""
import os
import sys
from pathlib import Path

# 시스템 python3로 실행돼도 프로젝트 venv(.venv)의 파이썬으로 자동 전환
_VENV_PY = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
if _VENV_PY.exists() and Path(sys.prefix).resolve() != _VENV_PY.parents[1].resolve():
    os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])

import argparse
import json
import re
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import candidate_filter
import news_analyzer
import report_writer
import sustainability_validator as validator
import trend_classifier

ROOT = Path(__file__).resolve().parents[2]
MEM_LONG = ROOT / "memory" / "stock" / "long_term"
HISTORY = MEM_LONG / "recommendation_history.json"
SECTOR_TRENDS = MEM_LONG / "sector_trends.md"
TIME_LIMIT = 60.0
TOP = 10

REFUSAL = ("이 요청은 자동 거래 또는 비공개 정보를 포함합니다. 추천 에이전트는 거래 실행/내부 정보 제공이 불가합니다. "
           "정보성 분석이 필요하면 요청을 다시 표현해주세요.")

# ---------------------------------------------------------------- 가드레일 (specs/guardrails.md)
_TRADE = re.compile(
    r"자동\s*(매수|매도|매매|거래|주문)|(매수|매도|주문)\s*(해\s*줘|해\s*주세요|실행|넣어)|(사|팔아)\s*줘|"
    r"(auto\w*|automatic\w*)\s*(trad|buy|sell|order)|place\s+(an?\s+)?order|(buy|sell)\s+\S+\s+for\s+me|"
    r"트레이딩\s*봇|trading\s*bot|open\s*api|openapi|ibkr|매매\s*(프로그램|봇)", re.I)
_INSIDER = re.compile(r"미공개|비공개\s*(정보|실적|자료)|내부자|내부\s*정보|insider|non-?public|mnpi", re.I)
_CONCENTRATE = re.compile(r"올인|몰빵|단일\s*종목|한\s*종목(만|에)|(\d{2,3})\s*%\s*(이상\s*)?(집중|비중|몰아)", re.I)


def guardrail(request: str) -> tuple[str, str]:
    """('block'|'confirm'|'ok', 사유)."""
    if _TRADE.search(request):
        return "block", "자동 거래"
    if _INSIDER.search(request):
        return "block", "비공개 정보"
    m = _CONCENTRATE.search(request)
    if m:
        pct = re.search(r"(\d{2,3})\s*%", m.group(0))
        if not pct or int(pct.group(1)) >= 30:
            return "confirm", "단일 종목 30% 이상 집중"
    return "ok", ""


def parse_request(request: str) -> dict:
    f = {"theme": None, "small_mid": bool(re.search(r"중소형|소형주|중형주|small|mid", request, re.I)), "market": None}
    themes = {"AI": r"\bAI\b|인공지능", "반도체": r"반도체|semiconductor", "EV 2차전지": r"2차\s*전지|이차전지|전기차|배터리|\bEV\b",
              "금리": r"금리", "바이오": r"바이오|제약|신약", "방산": r"방산|방위", "클라우드": r"클라우드|cloud", "핀테크": r"핀테크|fintech"}
    for name, pat in themes.items():
        if re.search(pat, request, re.I):
            f["theme"] = name
            break
    if re.search(r"한국(만|주식)?|국내(만|주)?|코스피|코스닥|\bKR\b", request) and not re.search(r"미국|해외|나스닥", request):
        f["market"] = "KR"
    elif re.search(r"미국(만|주식)?|해외(만|주)?|나스닥|\bUS\b|S&P", request) and not re.search(r"한국|국내", request):
        f["market"] = "US"
    return f


# ---------------------------------------------------------------- 메모리 (specs/memory_policy.md)
def load_history() -> list[dict]:
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def update_outcomes(history: list[dict], deadline: float) -> None:
    """과거 추천의 1주/1개월 후 실제 등락 채우기 (yfinance)."""
    import yfinance as yf
    today = datetime.now().date()
    todo = []
    for h in history:
        age = (today - datetime.strptime(h["date"], "%Y-%m-%d").date()).days
        if (h.get("ret_1w") is None and age >= 7) or (h.get("ret_1m") is None and age >= 30):
            todo.append((h, age))
    if not todo or time.time() > deadline:
        return

    def price_after(ticker, start, days):
        d0 = datetime.strptime(start, "%Y-%m-%d") + timedelta(days=days)
        df = yf.Ticker(ticker).history(start=d0.strftime("%Y-%m-%d"), end=(d0 + timedelta(days=6)).strftime("%Y-%m-%d"))
        return float(df["Close"].iloc[0]) if not df.empty else None

    def fill(item):
        h, age = item
        try:
            for key, days in (("ret_1w", 7), ("ret_1m", 30)):
                if h.get(key) is None and age >= days:
                    p = price_after(h["ticker"], h["date"], days)
                    if p and h.get("price"):
                        h[key] = round(p / h["price"] - 1, 4)
        except Exception:
            pass

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(fill, todo[:30]))


def track_record(history: list[dict], ticker: str) -> str | None:
    past = [h for h in history if h["ticker"] == ticker]
    if not past:
        return None
    parts = [f"과거 {len(past)}회 추천"]
    for key, label in (("ret_1w", "1주"), ("ret_1m", "1개월")):
        done = [h[key] for h in past if h.get(key) is not None]
        parts.append(f"{label} 후 상승 {sum(r > 0 for r in done)}/{len(done)}회" if done else f"{label} 결과 미집계")
    return " · ".join(parts)


def save_history(history: list[dict], picks: list[dict]) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    history = [h for h in history if not (h["date"] == today and h["ticker"] in {p["ticker"] for p in picks})]
    for p in picks:
        history.append({"date": today, "ticker": p["ticker"], "name": p["name"], "reason": p["analysis"]["reason"],
                        "price": p["last_close"], "currency": p["currency"], "ret_1w": None, "ret_1m": None})
    MEM_LONG.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")


def update_sector_trends(picks: list[dict]) -> list[str]:
    """트렌드 누적 기록. 같은 트렌드가 5회 이상 등장하면 '지속 트렌드' 후보로 등록. 새로 등록된 트렌드명 반환."""
    counts, last = {}, {}
    if SECTOR_TRENDS.exists():
        for line in SECTOR_TRENDS.read_text(encoding="utf-8").splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 3 and cells[1].isdigit():
                counts[cells[0]], last[cells[0]] = int(cells[1]), cells[2]
    before = {t for t, n in counts.items() if n >= 5}
    today = datetime.now().strftime("%Y-%m-%d")
    for p in picks:
        for t in p["trends"]:
            counts[t["theme"]] = counts.get(t["theme"], 0) + 1
            last[t["theme"]] = today
    lines = ["# 산업 트렌드 누적", "", "같은 트렌드가 5회 이상 등장하면 '지속 트렌드' 후보로 자동 등록한다.", "",
             "| 트렌드 | 누적 등장 | 마지막 등장 | 상태 |", "|---|---|---|---|"]
    for t, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {t} | {n} | {last[t]} | {'지속 트렌드 후보' if n >= 5 else '관찰'} |")
    MEM_LONG.mkdir(parents=True, exist_ok=True)
    SECTOR_TRENDS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sorted({t for t, n in counts.items() if n >= 5} - before)


# ---------------------------------------------------------------- 파이프라인
def run(request: str) -> int:
    t0 = time.time()
    el = lambda: time.time() - t0
    flt = parse_request(request)
    budget = news_analyzer.TokenBudget()
    notes, excluded = [], []
    trend_classifier.preload()  # 임베딩 모델 로딩을 뒤 단계와 겹쳐 진행

    cand = candidate_filter.run(flt)
    st, stocks = cand["stats"], cand["candidates"]
    srcs = ", ".join(f"{k.upper()} {v}" for k, v in st["sources"].items())
    notes.append(f"유니버스 {st['universe']}종목({srcs}) → 상승률 ≥{candidate_filter.MIN_RETURN_5D:.0%}·거래량 ≥{candidate_filter.MIN_VOLUME_RATIO}배 "
                 f"{st['after_return_volume']}개 → 시총 필터 {st['after_mcap']}개 (시총 미확정 {st['mcap_na_dropped']}개는 fail-closed로 제외) → 시장 다양성 적용 후 후보 {len(stocks)}개")
    if st["diversity_relaxed"]:
        notes.append("시장 다양성(한국·미국 각 ≤60%): 한쪽 시장에서 필터를 통과한 후보가 없어 충족하지 못했습니다.")
    if flt["theme"]:
        notes.append(f"요청 테마 '{flt['theme']}' 일치 종목만 추천 대상으로 제한")
    if flt["small_mid"]:
        notes.append("중소형주 위주 요청: 시총 상한 적용 (KRW 10조 / USD 200억)")
    print(f"[main] 후보 {len(stocks)}개 ({el():.0f}s)")

    if stocks:
        # 뉴스 · 공시 / 업종 / 기관 수급을 병렬 수집
        with ThreadPoolExecutor(3) as ex:
            jobs = [ex.submit(news_analyzer.collect, stocks), ex.submit(trend_classifier.fetch_company_info, stocks),
                    ex.submit(validator.fetch_institutional, stocks)]
            for j in jobs:
                j.result()
        print(f"[main] 뉴스·업종·수급 수집 완료 ({el():.0f}s)")
        try:
            res = news_analyzer.analyze(stocks, budget, timeout=25.0)
        except Exception as exc:
            print(f"[main] 분석 실패: {type(exc).__name__}: {exc}")
            notes.append(f"Claude 분석 호출 실패({type(exc).__name__}) — 근거를 검증할 수 없어 추천하지 않았습니다.")
            res = {}
        for s in stocks:
            s["analysis"] = res.get(s["ticker"], {"reason": "", "risks": [], "evidence": [], "event_type": None, "nature": None})
        print(f"[main] Claude 분석 완료 ({el():.0f}s)")
        trend_classifier.classify(stocks)
        print(f"[main] 분석·트렌드 분류 완료 ({el():.0f}s, 토큰 {budget.used:,})")

        # 검증 → 실패 종목만 재분석 (최대 3회)
        attempts = {s["ticker"]: 0 for s in stocks}
        for _ in range(validator.MAX_RETRIES):
            failing = {s["ticker"]: validator.verify(s) for s in stocks}
            failing = {t: f for t, f in failing.items() if f}
            if not failing or el() > 34 or budget.left < 8000:
                if failing:
                    notes.append(f"검증 실패 {len(failing)}종목은 시간(60초)·토큰 한도 때문에 재분석하지 못하고 제외했습니다.")
                break
            sub = [s for s in stocks if s["ticker"] in failing]
            for s in sub:
                attempts[s["ticker"]] += 1
            news_analyzer.collect(sub, variant=max(attempts[s["ticker"]] for s in sub), use_cache=False)
            try:
                res = news_analyzer.analyze(sub, budget, {t: "; ".join(f) for t, f in failing.items()}, timeout=max(8.0, 52 - el()))
            except Exception as exc:
                print(f"[main] 재분석 실패: {type(exc).__name__}")
                break
            for s in sub:
                if s["ticker"] in res:
                    s["analysis"] = res[s["ticker"]]
            trend_classifier.classify(sub)
            print(f"[main] 재분석 {len(sub)}종목 ({el():.0f}s)")
        eligible = []
        for s in stocks:
            fails = validator.verify(s)
            if fails:
                excluded.append(f"{s['name']}({s['ticker']}): 검증 실패 — {'; '.join(fails)} (재시도 {attempts[s['ticker']]}회{'; 시간 한도로 생략' if not attempts[s['ticker']] else ''})")
            elif not validator.sustainable(s):
                excluded.append(f"{s['name']}({s['ticker']}): 지속 가능성 기준 0개 (구조적 이벤트·트렌드 매칭·기관/외국인 순매수 모두 없음)")
            elif flt["theme"] and flt["theme"] not in {t["theme"] for t in s["trends"]}:
                excluded.append(f"{s['name']}({s['ticker']}): 요청 테마 '{flt['theme']}'와 불일치")
            else:
                eligible.append(s)
    else:
        eligible = []

    for s in eligible:
        s["criteria"], s["grade"], s["score"] = validator.criteria(s), validator.rating(s), validator.score(s)
    eligible.sort(key=lambda s: s["score"], reverse=True)
    picks = candidate_filter.select_diverse(eligible, TOP, flt["market"] is None)
    if flt["market"] is None and picks and len({p["country"] for p in picks}) < 2:
        notes.append("최종 추천에 한국·미국 종목이 모두 포함되지 못했습니다 (한쪽 시장의 검증 통과 종목이 없음).")
    for s in eligible:
        if s not in picks:
            excluded.append(f"{s['name']}({s['ticker']}): 검증 통과했으나 Top {TOP} 순위·시장 다양성 규칙에서 밀림")
    miss = [s for s in picks if not s.get("sector")]
    if miss:
        notes.append("업종 조회 실패(트렌드 미확정 처리): " + ", ".join(s["name"] for s in miss))
    notes.append("한국 종목의 DART 공시는 DART_API_KEY가 있을 때만, 기관/외국인 순매수는 KRX 로그인(KRX_ID/KRX_PW)이 있을 때만 조회됩니다.")

    history = load_history()
    update_outcomes(history, t0 + TIME_LIMIT - 8)
    for p in picks:
        p["track_record"] = track_record(history, p["ticker"])

    meta = {"request": request, "notes": notes, "excluded": excluded, "tokens": budget.used, "elapsed": el()}
    path = report_writer.write(picks, meta)
    doc_fails = validator.verify_document(path)
    if doc_fails:  # 면책 문구 검증 실패 → 즉시 재작성
        path = report_writer.write(picks, meta, path)
        doc_fails = validator.verify_document(path)
    if doc_fails:
        print("[main] 문서 검증 실패:", doc_fails)
        return 1

    save_history(history, picks)
    promoted = update_sector_trends(picks)
    if promoted:
        print("[main] 지속 트렌드 후보 신규 등록:", ", ".join(promoted))

    print(f"\n결과 파일: {path}")
    print(f"추천 {len(picks)}개 · 소요 {el():.0f}초 · 토큰 {budget.used:,}" + (" · ⚠ 60초 초과" if el() > TIME_LIMIT else ""))
    for i, p in enumerate(picks[:3], 1):
        print(f"{i}. {p['name']} ({p['ticker']}, {p['ret_5d'] * 100:+.1f}%, {p['grade']}) — {report_writer.mask_assertive(p['analysis']['reason'])}")
    if not picks:
        print("조건(상승률·거래량·시총)과 검증을 모두 통과한 종목이 없습니다. 임계값을 낮추지 않았습니다.")
    print(f"\n{report_writer.DISCLAIMER_KR}\n{report_writer.DISCLAIMER_EN}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="주식 종목 추천 에이전트 (추천만, 거래 실행 없음)")
    ap.add_argument("--request", required=True, help='자연어 요청 (예: "오늘 추천 10개")')
    ap.add_argument("--confirm", action="store_true", help="집중 추천 등 사용자 확인이 필요한 요청을 승인")
    args = ap.parse_args()

    verdict, why = guardrail(args.request)
    if verdict == "block":
        print(REFUSAL)
        return 2
    if verdict == "confirm" and not args.confirm:
        print(f"확인 필요: {why} 추천 요청입니다. 진행하려면 사용자 승인 후 --confirm 을 붙여 다시 실행하세요.")
        return 4
    return run(args.request)


if __name__ == "__main__":
    sys.exit(main())
