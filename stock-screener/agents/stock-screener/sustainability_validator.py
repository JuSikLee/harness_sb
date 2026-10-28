"""지속 가능성 검증 (specs/architecture.md 섹션 5 · specs/retry_policy.md '종목 추천 검증' 코드화).

- 지속 가능성 기준: 구조적 이벤트 OR 산업 트렌드 매칭 OR 기관/외국인 순매수 → 3개 모두 0개면 후보 제외
- 종목별 검증 항목 실패 시 해당 종목만 재분석(최대 3회, 재시도 루프는 main.py)
- 추천 등급(검토 · 관심 · 주의)과 랭킹 점수 산정
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from news_analyzer import EVENT_TYPES, NATURES
from report_writer import DISCLAIMER_EN, DISCLAIMER_KR

MAX_RETRIES = 3
OVERHEATED_RETURN = 0.30


# ---------------------------------------------------------------- 기관/외국인 순매수
def _us_institutional(stock: dict):
    """미국: yfinance가 제공하는 13F 기관 보유 변동(pctChange) 평균이 양수이면 순매수 신호."""
    import yfinance as yf
    h = yf.Ticker(stock["ticker"]).institutional_holders
    if h is None or h.empty or "pctChange" not in h:
        return None
    chg = h["pctChange"].dropna()
    if chg.empty:
        return None
    mean = float(chg.mean())
    return {"ok": mean > 0, "desc": f"상위 기관 {len(chg)}곳 13F 보유 변동 평균 {mean * 100:+.2f}%",
            "source": f"[yfinance 13F institutional_holders, {stock['ticker']}]"}


def _kr_investor_flow(stock: dict):
    """한국: pykrx 투자자별 순매수(최근 5영업일). KRX 로그인 정보가 없으면 조회 불가 → None."""
    import os
    if not (os.getenv("KRX_ID") and os.getenv("KRX_PW")):
        return None
    from pykrx import stock as krx
    end = datetime.now()
    df = krx.get_market_trading_value_by_investor(
        (end - timedelta(days=7)).strftime("%Y%m%d"), end.strftime("%Y%m%d"), stock["ticker"].split(".")[0])
    if df is None or df.empty:
        return None
    net = df.loc[[i for i in ("기관합계", "외국인합계") if i in df.index], "순매수"]
    if net.empty:
        return None
    total = float(net.sum())
    return {"ok": total > 0, "desc": f"기관+외국인 최근 5영업일 순매수 합계 {total:,.0f}원",
            "source": f"[pykrx 투자자별 거래대금, {stock['ticker']}]"}


def fetch_institutional(stocks: list[dict]) -> None:
    def one(s):
        try:
            s["institutional"] = _us_institutional(s) if s["country"] == "US" else _kr_investor_flow(s)
        except Exception:
            s["institutional"] = None  # 조회 불가는 '신호 없음'으로 취급(추측하지 않음)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, stocks))


# ---------------------------------------------------------------- 검증
def verify(stock: dict) -> list[str]:
    """retry_policy '종목 추천 검증' 항목 점검. 반환: 실패 사유 목록 (빈 리스트면 통과)."""
    a = stock.get("analysis") or {}
    fails = []
    ev = [e for e in a.get("evidence", []) if e.get("url") and e.get("title")]
    if not a.get("reason") or not ev:
        fails.append("상승 이유를 공개 출처로 검증할 수 없음 (근거 기사/공시 없음)")
    elif not a.get("explains_rise"):
        fails.append("근거 기사가 상승 원인을 직접 설명하지 않음 (관련 소식만 확인됨)")
    if any(not t.get("article") for t in stock.get("trends", [])):
        fails.append("산업 트렌드가 검색 결과에 기반하지 않음")
    if a.get("nature") not in NATURES or a.get("event_type") not in EVENT_TYPES:
        fails.append("일회성/구조적 또는 이벤트 유형 구분 누락")
    risks = a.get("risks", [])
    if len(risks) > 2:
        a["risks"] = risks[:2]  # 1~2개 규칙: 초과분은 잘라낸다
    elif not risks:
        fails.append("하방 리스크 미기재")
    return fails


def criteria(stock: dict) -> dict:
    a = stock.get("analysis") or {}
    inst = stock.get("institutional") or {}
    return {"구조적 이벤트": a.get("nature") == "구조적",
            "산업 트렌드 매칭": bool(stock.get("trends")),
            "기관/외국인 순매수": bool(inst.get("ok"))}


def sustainable(stock: dict) -> bool:
    return any(criteria(stock).values())


def rating(stock: dict) -> str:
    """검토 · 관심 · 주의 (매수/매도 표현을 쓰지 않는다)."""
    n = sum(criteria(stock).values())
    a = stock["analysis"]
    if a["nature"] == "일회성" and stock["ret_5d"] >= OVERHEATED_RETURN:
        return "주의"
    if a["nature"] == "구조적" and n >= 2:
        return "검토"
    if n >= 2:
        return "검토" if a["nature"] == "구조적" else "관심"
    return "관심"


def score(stock: dict) -> float:
    c = criteria(stock)
    s = sum(c.values()) * 10 + (5 if c["구조적 이벤트"] else 0)
    s += min(stock["ret_5d"], 0.3) * 10 + min(stock["vol_ratio"], 5)
    if stock["analysis"]["nature"] == "일회성":
        s -= 3
    return s


# ---------------------------------------------------------------- 문서 검증
def verify_document(path) -> list[str]:
    """생성된 .docx의 첫 단락 · 마지막 단락에 면책 문구(한 · 영)가 모두 있는지."""
    from docx import Document
    paras = [p.text.strip() for p in Document(str(path)).paragraphs if p.text.strip()]
    fails = []
    if not paras:
        return ["문서가 비어 있음"]
    for label, text in (("첫", paras[0]), ("마지막", paras[-1])):
        if DISCLAIMER_KR not in text or DISCLAIMER_EN not in text:
            fails.append(f"{label} 단락에 면책 문구(한·영) 누락")
    return fails
