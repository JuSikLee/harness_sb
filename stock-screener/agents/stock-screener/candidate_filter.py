"""1차 후보 추출 (specs/architecture.md 섹션 2).

유니버스(KOSPI200 + KOSDAQ150 + S&P500) → 5일 상승률 · 거래량 · 시총 필터 → 시장 다양성 → 상위 20개.
주가 · 거래량 · 시총은 yfinance(한국 시총은 pykrx 우선)만 사용한다. 거래 관련 API는 사용하지 않는다.
"""
import json
import os
import re
import time
from datetime import datetime
from io import StringIO
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data" / "cache"

MIN_RETURN_5D = float(os.getenv("MIN_RETURN_5D", "0.05"))
MIN_VOLUME_RATIO = float(os.getenv("MIN_VOLUME_RATIO", "1.5"))
MIN_MCAP_KRW = float(os.getenv("MIN_MCAP_KRW", "1e11"))
MIN_MCAP_USD = float(os.getenv("MIN_MCAP_USD", "5e8"))
TOP_N = 20
MAX_SHARE = 0.6

# "중소형주 위주" 요청 시 적용하는 시총 상한
SMALL_MID_CAP_KRW = 1e13
SMALL_MID_CAP_USD = 2e10

# 동적 로드 실패 시 fallback (한국 30 + 미국 30)
FALLBACK_KR = [
    ("005930.KS", "삼성전자"), ("000660.KS", "SK하이닉스"), ("373220.KS", "LG에너지솔루션"),
    ("207940.KS", "삼성바이오로직스"), ("005380.KS", "현대차"), ("000270.KS", "기아"),
    ("068270.KS", "셀트리온"), ("105560.KS", "KB금융"), ("055550.KS", "신한지주"),
    ("005490.KS", "POSCO홀딩스"), ("035420.KS", "NAVER"), ("035720.KS", "카카오"),
    ("006400.KS", "삼성SDI"), ("051910.KS", "LG화학"), ("012330.KS", "현대모비스"),
    ("012450.KS", "한화에어로스페이스"), ("047810.KS", "한국항공우주"), ("329180.KS", "HD현대중공업"),
    ("028260.KS", "삼성물산"), ("009150.KS", "삼성전기"), ("086790.KS", "하나금융지주"),
    ("034020.KS", "두산에너빌리티"), ("042660.KS", "한화오션"), ("079550.KS", "LIG넥스원"),
    ("003670.KS", "포스코퓨처엠"), ("247540.KQ", "에코프로비엠"), ("086520.KQ", "에코프로"),
    ("196170.KQ", "알테오젠"), ("028300.KQ", "HLB"), ("058470.KQ", "리노공업"),
]
FALLBACK_US = [(t, t) for t in (
    "AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO JPM LLY XOM UNH V MA COST NFLX AMD ORCL PLTR LMT "
    "RTX CRM ADBE INTC MU GE PFE BA PYPL SNOW").split()]


def currency_of(ticker: str) -> str:
    return "KRW" if ticker.endswith((".KS", ".KQ")) else "USD"


def country_of(ticker: str) -> str:
    return "KR" if currency_of(ticker) == "KRW" else "US"


def _load_kr():
    """pykrx로 KOSPI200 + KOSDAQ150 구성종목 · 시총 로드. 실패하면 예외."""
    if not (os.getenv("KRX_ID") and os.getenv("KRX_PW")):  # pykrx는 KRX 로그인이 필요
        raise RuntimeError("KRX_ID/KRX_PW 없음")
    from pykrx import stock
    today = datetime.now().strftime("%Y%m%d")
    rows = []
    for index_code, suffix in (("1028", ".KS"), ("2203", ".KQ")):
        for code in stock.get_index_portfolio_deposit_file(index_code):
            rows.append([code, suffix])
    if not rows:
        raise RuntimeError("pykrx 구성종목 없음")
    caps = None
    for back in range(0, 6):  # 휴장일 대비: 최근 영업일 탐색
        day = (pd.Timestamp(today) - pd.Timedelta(days=back)).strftime("%Y%m%d")
        caps = stock.get_market_cap_by_ticker(day)
        if caps is not None and not caps.empty:
            break
    out = []
    for code, suffix in rows:
        mcap = None
        if caps is not None and code in caps.index:
            mcap = float(caps.loc[code, "시가총액"]) or None
        out.append({"ticker": code + suffix, "name": stock.get_market_ticker_name(code),
                    "country": "KR", "mcap": mcap})
    return out


def _load_kr_wiki():
    """pykrx를 쓸 수 없을 때(KRX 로그인 필요): ko.wikipedia KOSPI 200 구성종목 + KOSDAQ 시총 상위 150(KOSDAQ150 근사)."""
    import requests
    html = requests.get("https://ko.wikipedia.org/wiki/KOSPI_200", headers={"User-Agent": "Mozilla/5.0 (stock-screener research)"},
                        timeout=10).text
    df = pd.read_html(StringIO(html), converters={"종목코드": str})[1]
    out = [{"ticker": str(r["종목코드"]).zfill(6) + ".KS", "name": str(r["회사명"]), "country": "KR", "mcap": None}
           for _, r in df.iterrows() if re.fullmatch(r"[0-9A-Z]{1,6}", str(r["종목코드"]).strip()) and str(r["종목코드"]).upper() != "NAN"]
    if len(out) < 100:
        raise RuntimeError("KOSPI200 표 비정상")
    try:  # KOSDAQ150 구성종목은 pykrx(로그인 필요) 없이 얻을 수 없어, yfinance screener의 KOSDAQ 시총 상위 150으로 근사
        import yfinance as yf
        res = yf.screen(yf.EquityQuery("eq", ["exchange", "KOE"]), sortField="intradaymarketcap", sortAsc=False, size=150)
        out += [{"ticker": q["symbol"], "name": q.get("longName") or q.get("shortName") or q["symbol"], "country": "KR",
                 "mcap": float(q["marketCap"]) if q.get("marketCap") else None}
                for q in res["quotes"] if q["symbol"].endswith(".KQ")]
    except Exception as exc:
        print(f"[candidate_filter] KOSDAQ 시총 상위 조회 실패 → 대표 종목만 사용 ({type(exc).__name__})")
        out += [{"ticker": t, "name": n, "country": "KR", "mcap": None} for t, n in FALLBACK_KR if t.endswith(".KQ")]
    return out


def _load_us():
    """Wikipedia S&P500 표 로드. 실패하면 예외."""
    import requests
    html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                        headers={"User-Agent": "Mozilla/5.0 (stock-screener research)"}, timeout=10).text
    df = pd.read_html(StringIO(html))[0]
    out = [{"ticker": str(r["Symbol"]).replace(".", "-"), "name": str(r["Security"]),
            "country": "US", "mcap": None} for _, r in df.iterrows()]
    if len(out) < 100:
        raise RuntimeError("S&P500 표 비정상")
    return out


def load_universe() -> tuple[list[dict], dict]:
    """유니버스 로드. 같은 날 캐시 재사용(단, fallback으로 채운 시장은 다시 동적 로드 시도)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"universe_{datetime.now():%Y%m%d}.json"
    cached = {}
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = {}
    sources = {}
    data = {}
    for key, loader, fallback, country in (("kr", [("pykrx", _load_kr), ("wikipedia", _load_kr_wiki)], FALLBACK_KR, "KR"),
                                           ("us", [("wikipedia", _load_us)], FALLBACK_US, "US")):
        if cached.get(key) and cached.get("sources", {}).get(key) not in (None, "fallback"):
            data[key], sources[key] = cached[key], cached["sources"][key]
            continue
        for name, fn in loader:
            try:
                data[key], sources[key] = fn(), name
                break
            except Exception as exc:  # 네트워크 · 로그인 · 파싱 실패 → 다음 방법
                print(f"[candidate_filter] {key} 유니버스 {name} 로드 실패 ({type(exc).__name__}: {str(exc)[:60]})")
        else:
            data[key] = [{"ticker": t, "name": n, "country": country, "mcap": None} for t, n in fallback]
            sources[key] = "fallback"
    path.write_text(json.dumps({"kr": data["kr"], "us": data["us"], "sources": sources}, ensure_ascii=False),
                    encoding="utf-8")
    return data["kr"] + data["us"], sources


def fetch_market_cap(ticker: str, known: float | None = None) -> float | None:
    """시총 조회. 한국은 pykrx 값(known) 우선, 미국은 fast_info → info. 실패 시 1회 재시도. 확정 못 하면 None."""
    if known and known > 0:
        return float(known)
    import yfinance as yf
    for attempt in range(2):
        try:
            t = yf.Ticker(ticker)
            try:
                mc = t.fast_info["market_cap"]
            except Exception:
                mc = None
            if not mc:
                mc = (t.info or {}).get("marketCap")
            if mc and float(mc) > 0:
                return float(mc)
        except Exception:
            pass
        time.sleep(0.3)
    return None


METRICS_TTL_SEC = 1800  # 같은 요청이 반복돼도 30분 안에는 가격 지표를 재사용 (60초 제한 대비)


def _download(chunk: list[str]):
    import yfinance as yf
    try:
        return yf.download(chunk, period="15d", progress=False, threads=False, auto_adjust=True)
    except Exception:
        return None


def _price_metrics(universe: list[dict]) -> list[dict]:
    """종목별 5일 상승률 · 거래량 비율 계산 (yfinance 병렬 다운로드, 30분 캐시)."""
    cache = CACHE_DIR / f"metrics_{datetime.now():%Y%m%d}.json"
    key = sorted(u["ticker"] for u in universe)
    if cache.exists() and time.time() - cache.stat().st_mtime < METRICS_TTL_SEC:
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if saved.get("tickers") == key:
                return saved["metrics"]
        except (OSError, ValueError):
            pass
    tickers = [u["ticker"] for u in universe]
    from concurrent.futures import ThreadPoolExecutor
    workers = 8  # 실측: 8개 청크 병렬(청크 내부는 순차)이 yf.download(threads=N)보다 약 2배 빠름
    size = len(tickers) // workers + 1
    with ThreadPoolExecutor(workers) as ex:
        frames = list(ex.map(_download, [tickers[i:i + size] for i in range(0, len(tickers), size)]))
    out = []
    by_ticker = {u["ticker"]: u for u in universe}
    for df in frames:
        if df is None or df.empty:
            continue
        close, vol = df["Close"], df["Volume"]
        for t in close.columns:
            c = close[t].dropna()
            v = vol[t].reindex(c.index)
            if len(c) < 7 or t not in by_ticker:
                continue
            last_vol = float(v.iloc[-1]) if pd.notna(v.iloc[-1]) else 0.0
            avg_vol = float(v.iloc[-6:-1].mean())  # 직전 5거래일 평균
            if last_vol <= 0 or not avg_vol or pd.isna(avg_vol):
                continue
            out.append({**by_ticker[t],
                        "last_close": float(c.iloc[-1]), "last_date": c.index[-1].strftime("%Y-%m-%d"),
                        "ret_5d": float(c.iloc[-1] / c.iloc[-6] - 1), "vol_ratio": last_vol / avg_vol})
    if out:
        cache.write_text(json.dumps({"tickers": key, "metrics": out}, ensure_ascii=False), encoding="utf-8")
    return out


def select_diverse(items: list[dict], n: int, diversify: bool = True) -> list[dict]:
    """정렬된 items에서 n개 선택. 한국 ≤ 60% AND 미국 ≤ 60% (한쪽 시장이 부족하면 가능한 범위까지만 허용)."""
    if not diversify:
        return items[:n]
    cap = int(n * MAX_SHARE)
    picked, count = [], {"KR": 0, "US": 0}
    for it in items:
        if len(picked) >= n:
            break
        if count[it["country"]] >= cap:
            continue
        picked.append(it)
        count[it["country"]] += 1
    # 한쪽이 부족해 선택 결과에서 60%를 넘으면, 반대편 수의 1.5배까지로 줄인다(반대편이 0이면 그대로 두고 경고).
    for c in ("KR", "US"):
        other = "US" if c == "KR" else "KR"
        if count[other] and count[c] / len(picked) > MAX_SHARE:
            keep = int(count[other] * MAX_SHARE / (1 - MAX_SHARE))
            seen = 0
            trimmed = []
            for it in picked:
                if it["country"] == c:
                    seen += 1
                    if seen > keep:
                        continue
                trimmed.append(it)
            picked = trimmed
            count = {k: sum(1 for p in picked if p["country"] == k) for k in count}
    return picked


def run(filters: dict | None = None, deadline: float | None = None) -> dict:
    """후보 상위 20개 반환. filters: market('KR'/'US'/None), small_mid(bool)."""
    filters = filters or {}
    universe, sources = load_universe()
    if filters.get("market") in ("KR", "US"):
        universe = [u for u in universe if u["country"] == filters["market"]]
    stats = {"universe": len(universe), "sources": sources}

    metrics = _price_metrics(universe)
    stats["priced"] = len(metrics)
    passed = [m for m in metrics if m["ret_5d"] >= MIN_RETURN_5D and m["vol_ratio"] >= MIN_VOLUME_RATIO]
    stats["after_return_volume"] = len(passed)
    passed.sort(key=lambda m: m["ret_5d"], reverse=True)

    # 시총: 확정 못 한 종목은 fail-closed(제외)
    survivors, na_dropped = [], 0
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        mcaps = list(ex.map(lambda m: fetch_market_cap(m["ticker"], m.get("mcap")), passed))
    for m, mcap in zip(passed, mcaps):
        floor = MIN_MCAP_KRW if m["country"] == "KR" else MIN_MCAP_USD
        if mcap is None:
            na_dropped += 1
            continue
        if mcap < floor:
            continue
        if filters.get("small_mid") and mcap > (SMALL_MID_CAP_KRW if m["country"] == "KR" else SMALL_MID_CAP_USD):
            continue
        m["mcap"] = mcap
        m["currency"] = currency_of(m["ticker"])
        m["market"] = "KOSPI" if m["ticker"].endswith(".KS") else "KOSDAQ" if m["ticker"].endswith(".KQ") else "US"
        survivors.append(m)
    stats["mcap_na_dropped"] = na_dropped
    stats["after_mcap"] = len(survivors)

    diversify = filters.get("market") not in ("KR", "US")
    picked = select_diverse(survivors, TOP_N, diversify)
    stats["diversity_relaxed"] = diversify and len({p["country"] for p in picked}) < 2
    return {"candidates": picked, "stats": stats}
