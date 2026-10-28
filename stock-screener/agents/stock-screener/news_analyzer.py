"""뉴스 · 이벤트 분석 (specs/architecture.md 섹션 3).

Tavily로 최근 5일 뉴스를 수집(한국 종목 → 한국어, 미국 종목 → 영어)하고, 공시(DART / SEC EDGAR)를 보조로 붙인 뒤
Claude API로 이벤트 분류(실적 · 계약 · 정책 · M&A · 기타)와 일회성 vs 구조적 분류를 한 번에 수행한다.
공개 자료만 사용하며, 근거 기사 id를 반드시 되돌려받아 인용 출처를 코드에서 조립한다(모델이 출처를 지어내지 못하게).
"""
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
NEWS_CACHE = ROOT / "memory" / "stock" / "working" / "recent_news_cache.json"
CACHE_DIR = ROOT / "data" / "cache"
NEWS_TTL_SEC = 24 * 3600
MODEL = os.getenv("STOCK_MODEL", "claude-haiku-5-5")
TOKEN_LIMIT = 60_000
EVENT_TYPES = ("실적", "계약", "정책", "M&A", "기타")
NATURES = ("일회성", "구조적")

_lock = threading.Lock()


class TokenBudget:
    """한 요청당 토큰 한도(60,000) 추적."""

    def __init__(self, limit: int = TOKEN_LIMIT):
        self.limit, self.used = limit, 0

    def add(self, usage) -> None:
        with _lock:
            self.used += getattr(usage, "input_tokens", 0) + getattr(usage, "output_tokens", 0)

    @property
    def left(self) -> int:
        return self.limit - self.used


# ---------------------------------------------------------------- 뉴스 수집
def _load_cache() -> dict:
    try:
        return json.loads(NEWS_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    NEWS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    cache = {k: v for k, v in cache.items() if now - v.get("ts", 0) < NEWS_TTL_SEC}
    NEWS_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def _query(stock: dict, variant: int) -> str:
    if stock["country"] == "KR":  # 한국어 + 종목명
        return [f"{stock['name']} 주가 상승 이유", f"{stock['name']} 급등 공시 실적 수주"][variant % 2]
    # 영어 + ticker 회사명
    return [f"{stock['name']} {stock['ticker']} stock rally why shares rose",
            f"{stock['ticker']} shares jump news catalyst earnings"][variant % 2]


def _media(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _iso_date(raw: str | None) -> str:
    if not raw:
        return "날짜 미상"
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        m = re.search(r"\d{4}-\d{2}-\d{2}", raw)
        return m.group(0) if m else "날짜 미상"


def fetch_news(stock: dict, variant: int = 0, use_cache: bool = True) -> list[dict]:
    """최근 5일 뉴스 (24시간 캐시). variant>0이면 다른 질의로 재검색(재시도용)."""
    key = f"{stock['ticker']}|{variant}"
    if use_cache:
        with _lock:
            hit = _load_cache().get(key)
        if hit and time.time() - hit["ts"] < NEWS_TTL_SEC:
            return hit["articles"]
    from tavily import TavilyClient
    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    arts = []
    try:
        res = client.search(_query(stock, variant), topic="news", days=5 if variant == 0 else 7,
                            max_results=5, include_answer=False)
        for r in res.get("results", []):
            arts.append({"title": (r.get("title") or "").strip(), "url": r.get("url", ""),
                         "date": _iso_date(r.get("published_date")), "media": _media(r.get("url", "")),
                         "content": re.sub(r"\s+", " ", r.get("content") or "")[:300]})
    except Exception as exc:
        print(f"[news_analyzer] Tavily 실패 {stock['ticker']}: {type(exc).__name__}")
        return []
    with _lock:
        cache = _load_cache()
        cache[key] = {"ts": time.time(), "articles": arts}
        _save_cache(cache)
    return arts


# ---------------------------------------------------------------- 공시 보조 (SEC EDGAR / DART)
_SEC_HEADERS = {"User-Agent": "StockScreenerResearch/1.0 (public-data research)"}


def _edgar_filings(stock: dict) -> list[dict]:
    import requests
    path = CACHE_DIR / "sec_company_tickers.json"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not path.exists() or time.time() - path.stat().st_mtime > 7 * 86400:
        r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=_SEC_HEADERS, timeout=8)
        r.raise_for_status()
        path.write_text(r.text, encoding="utf-8")
    ciks = {v["ticker"].upper(): v["cik_str"] for v in json.loads(path.read_text(encoding="utf-8")).values()}
    cik = ciks.get(stock["ticker"].replace("-", ".").upper()) or ciks.get(stock["ticker"].upper())
    if not cik:
        return []
    r = requests.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers=_SEC_HEADERS, timeout=8)
    r.raise_for_status()
    recent = r.json()["filings"]["recent"]
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    out = []
    for form, day, acc, items in zip(recent["form"], recent["filingDate"], recent["accessionNumber"], recent["items"]):
        if form == "8-K" and day >= since:
            out.append({"title": f"SEC 8-K 공시 (items {items or 'n/a'})", "date": day, "media": "SEC EDGAR",
                        "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/",
                        "content": ""})
    return out[:2]


def _dart_filings(stock: dict) -> list[dict]:
    """DART_API_KEY가 있을 때만 동작 (공개 공시 목록, 최근 7일)."""
    key = os.getenv("DART_API_KEY")
    if not key:
        return []
    import io
    import xml.etree.ElementTree as ET
    import zipfile
    import requests
    path = CACHE_DIR / "dart_corp_codes.json"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        z = zipfile.ZipFile(io.BytesIO(requests.get("https://opendart.fss.or.kr/api/corpCode.xml",
                                                    params={"crtfc_key": key}, timeout=20).content))
        root = ET.fromstring(z.read(z.namelist()[0]))
        mapping = {c.findtext("stock_code").strip(): c.findtext("corp_code") for c in root.iter("list")
                   if (c.findtext("stock_code") or "").strip()}
        path.write_text(json.dumps(mapping), encoding="utf-8")
    corp = json.loads(path.read_text(encoding="utf-8")).get(stock["ticker"].split(".")[0])
    if not corp:
        return []
    today = datetime.now()
    r = requests.get("https://opendart.fss.or.kr/api/list.json", timeout=8, params={
        "crtfc_key": key, "corp_code": corp, "page_count": 5,
        "bgn_de": (today - timedelta(days=7)).strftime("%Y%m%d"), "end_de": today.strftime("%Y%m%d")})
    out = []
    for it in r.json().get("list", [])[:2]:
        d = it["rcept_dt"]
        out.append({"title": f"DART 공시: {it['report_nm']}", "date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "media": "DART",
                    "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['rcept_no']}", "content": ""})
    return out


def fetch_filings(stock: dict) -> list[dict]:
    try:
        return _dart_filings(stock) if stock["country"] == "KR" else _edgar_filings(stock)
    except Exception as exc:  # 공시는 보조 자료이므로 실패해도 진행
        print(f"[news_analyzer] 공시 조회 실패 {stock['ticker']}: {type(exc).__name__}")
        return []


def collect(stocks: list[dict], variant: int = 0, use_cache: bool = True) -> None:
    """각 종목에 stock['articles'] (뉴스 + 공시) 채우기. 스레드 병렬."""
    def one(s):
        s["articles"] = fetch_news(s, variant, use_cache) + fetch_filings(s)
    with ThreadPoolExecutor(10) as ex:
        list(ex.map(one, stocks))


# ---------------------------------------------------------------- Claude 분류
SYSTEM = """당신은 보수적이고 근거 중심인 리서치 애널리스트입니다. 공개된 기사·공시만 사용합니다.
규칙:
- 제공된 기사(id)에 있는 내용만 근거로 삼는다. 기사에 없는 사실, 미공개 실적·내부자 정보·M&A 추측은 절대 쓰지 않는다.
- 상승 이유를 뒷받침하는 기사가 없으면 reason을 빈 문자열, evidence를 빈 배열로 둔다 (지어내지 않는다).
- "매수"·"매도"·"사라"·"팔아라" 같은 단정 표현은 쓰지 않는다.
- event_type: 실적 / 계약 / 정책 / M&A / 기타 중 하나.
- nature: 단발 계약·일시적 이유·테마성 급등은 "일회성", 사업구조·수요·제도·점유율의 지속적 변화는 "구조적". 판단이 애매하면 "일회성".
- risks: 하방 리스크 1~2개, 각각 한 문장 (기사 내용 또는 급등 후 변동성·밸류에이션처럼 일반적으로 검증 가능한 위험).
- explains_rise: 기사가 이 종목의 최근 상승 원인을 직접 설명하면 true. 관련 소식(예: 실적 발표 사실)만 있고 상승과의 연결이 기사에서 확인되지 않으면 false.
- reason: 한국어 한 문장(80자 이내). risks 각 항목은 40자 이내. evidence: 근거 기사 id 배열 (관련도 높은 순, 최대 3개).
JSON 배열만 출력한다. 각 원소: {"ticker","event_type","nature","explains_rise","reason","evidence":[...],"risks":[...]}"""


def _extract_json(text: str):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("JSON 배열 없음")
    return json.loads(m.group(0))


CHUNK = 3  # 지연 단축을 위해 3종목씩 병렬 호출


def _analyze_chunk(stocks: list[dict], budget: TokenBudget, feedback: dict | None, timeout: float) -> dict[str, dict]:
    import anthropic
    payload = []
    for s in stocks:
        arts = [{"id": f"A{i}", "title": a["title"], "date": a["date"], "media": a["media"], "text": a["content"]}
                for i, a in enumerate(s.get("articles", []))]
        item = {"ticker": s["ticker"], "name": s["name"], "return_5d_pct": round(s["ret_5d"] * 100, 1), "articles": arts}
        if feedback and s["ticker"] in feedback:
            item["previous_failure"] = feedback[s["ticker"]]
        payload.append(item)
    est = len(json.dumps(payload, ensure_ascii=False)) // 2 + 1500  # 대략적 사전 추정 (입력 + 출력)
    if est > budget.left:
        raise RuntimeError(f"토큰 한도 초과 예상 (추정 {est} > 남은 {budget.left})")
    resp = anthropic.Anthropic(timeout=timeout, max_retries=1).messages.create(
        model=MODEL, max_tokens=min(2500, max(800, budget.left // 4)), system=SYSTEM,
        thinking={"type": "disabled"},  # 사고 토큰이 출력 한도 · 지연을 잡아먹지 않게
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
    budget.add(resp.usage)
    rows = _extract_json("".join(b.text for b in resp.content if b.type == "text"))
    by_ticker = {s["ticker"]: s for s in stocks}
    out = {}
    for row in rows:
        s = by_ticker.get(row.get("ticker"))
        if not s:
            continue
        arts = s.get("articles", [])
        ev = []
        for ref in row.get("evidence") or []:  # 존재하는 id만 인정 (출처를 지어내지 못하게)
            m = re.fullmatch(r"A(\d+)", str(ref))
            if m and int(m.group(1)) < len(arts):
                ev.append(arts[int(m.group(1))])
        out[s["ticker"]] = {
            "event_type": row.get("event_type") if row.get("event_type") in EVENT_TYPES else None,
            "nature": row.get("nature") if row.get("nature") in NATURES else None,
            "explains_rise": row.get("explains_rise") is True,
            "reason": (row.get("reason") or "").strip(),
            "risks": [r.strip() for r in (row.get("risks") or []) if isinstance(r, str) and r.strip()],
            "evidence": ev,
        }
    return out


def analyze(stocks: list[dict], budget: TokenBudget, feedback: dict | None = None, timeout: float = 30.0) -> dict[str, dict]:
    """종목들의 이벤트 · 일회성/구조적 · 상승 이유 · 리스크 분석. 반환: {ticker: analysis}.
    feedback: {ticker: 이전 검증 실패 사유} (재시도 시 전달)."""
    chunks = [stocks[i:i + CHUNK] for i in range(0, len(stocks), CHUNK)]
    out = {}
    errors = []

    def one(chunk):
        try:
            return _analyze_chunk(chunk, budget, feedback, timeout)
        except Exception as exc:
            errors.append(exc)
            return {}

    with ThreadPoolExecutor(max(1, len(chunks))) as ex:
        for part in ex.map(one, chunks):
            out.update(part)
    if errors and not out:
        raise errors[0]
    return out


def citation(a: dict) -> str:
    """[기사명, 날짜, 매체] 형식 출처."""
    return f"[{a['title']}, {a['date']}, {a['media']}]"
