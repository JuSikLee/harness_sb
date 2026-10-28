"""산업 트렌드 매칭 (specs/architecture.md 섹션 4).

뉴스 텍스트 임베딩과 트렌드 카테고리의 코사인 유사도(≥0.4)로 1차 매칭하고,
'섹터 게이트'(yfinance sector · industry)를 통과한 트렌드만 확정한다.
뉴스가 그 주제를 언급하는 것만으로는 트렌드를 부여하지 않는다.
"""
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data" / "cache"
SECTOR_CACHE = CACHE_DIR / "sector_info.json"
SECTOR_TTL_SEC = 30 * 86400
SIM_THRESHOLD = 0.4
EMBED_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"

CATEGORIES = {
    "AI": ["인공지능 AI 생성형 AI 데이터센터 투자", "artificial intelligence generative AI data center GPU demand"],
    "반도체": ["반도체 메모리 HBM 파운드리 장비", "semiconductor chip memory foundry HBM equipment"],
    "EV 2차전지": ["전기차 2차전지 배터리 양극재", "electric vehicle EV battery lithium cathode"],
    "금리": ["금리 인하 인상 기준금리 통화정책 물가", "interest rates Fed rate cut monetary policy inflation"],
    "바이오": ["바이오 신약 임상시험 제약 허가", "biotech drug clinical trial FDA approval pharmaceutical"],
    "방산": ["방산 방위산업 무기 수출 국방 예산", "defense aerospace military missile contract"],
    "클라우드": ["클라우드 SaaS 서버 소프트웨어 구독", "cloud computing SaaS software subscription"],
    "핀테크": ["핀테크 결제 전자결제 디지털 금융", "fintech payments digital payments platform"],
    "사이버보안": ["정보보안 사이버보안 백신 랜섬웨어 해킹 침해사고 보안 솔루션",
               "cybersecurity ransomware data breach endpoint security threat detection",
               "네트워크 보안", "보안 솔루션", "백신·보안"],
}
MACRO_THEMES = {"금리"}  # 섹터 게이트 예외 (모든 섹터 허용)
# 섹터 게이트가 없는 매크로 테마는 임의 매칭을 막기 위해 기사 본문에 핵심어가 실제로 있어야 인정한다
MACRO_KEYWORDS = re.compile(r"금리|통화정책|기준금리|국채|물가|interest rates?|rate (cut|hike)|\bfed\b|federal reserve|"
                            r"monetary policy|treasury yields?|inflation", re.I)
FIN_EXCLUDED = ("bank", "insurance", "asset management", "conglomerate", "capital markets")

_model = None
_model_thread = None
_model_lock = threading.Lock()


def _load_model():
    global _model
    with _model_lock:
        if _model is None:
            import logging
            import os
            os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
            logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(EMBED_MODEL)
    return _model


def preload() -> None:
    """임베딩 모델 로딩을 백그라운드로 시작 (뉴스 수집 · Claude 호출과 겹치게)."""
    global _model_thread
    _model_thread = threading.Thread(target=_load_model, daemon=True)
    _model_thread.start()


# ---------------------------------------------------------------- 회사 업종 조회
def _load_sector_cache() -> dict:
    try:
        return json.loads(SECTOR_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _exchange_label(code: str | None) -> str | None:
    return {"NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NYQ": "NYSE", "ASE": "NYSE American",
            "PCX": "NYSE Arca", "BTS": "Cboe"}.get(code or "", code)


def fetch_company_info(stocks: list[dict]) -> None:
    """각 종목에 sector · industry · exchange 채우기 (종목당 1회, 30일 캐시). 실패하면 None으로 둔다."""
    import yfinance as yf
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = _load_sector_cache()

    def one(s):
        hit = cache.get(s["ticker"])
        if hit and time.time() - hit["ts"] < SECTOR_TTL_SEC:
            return s["ticker"], hit
        for _ in range(2):
            try:
                info = yf.Ticker(s["ticker"]).info or {}
                if info.get("sector"):
                    return s["ticker"], {"ts": time.time(), "sector": info.get("sector"),
                                         "industry": info.get("industry"), "exchange": info.get("exchange")}
            except Exception:
                time.sleep(0.3)
        return s["ticker"], None

    with ThreadPoolExecutor(10) as ex:
        results = dict(ex.map(one, stocks))
    for s in stocks:
        r = results.get(s["ticker"])
        s["sector"], s["industry"] = (r["sector"], r["industry"]) if r else (None, None)
        if r:
            cache[s["ticker"]] = r
            if s["market"] == "US":
                s["market"] = _exchange_label(r.get("exchange")) or "US"
    try:
        SECTOR_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- 섹터 게이트
def sector_allows(theme: str, sector: str, industry: str) -> bool:
    """회사의 실제 업종이 해당 테마와 맞는가."""
    sec, ind = (sector or "").lower(), (industry or "").lower()
    if theme in MACRO_THEMES:
        return True
    if theme in ("AI", "반도체", "클라우드"):
        return sec == "technology" or "semiconductor" in ind  # Financial Services 등은 불인정
    if theme == "EV 2차전지":
        return (sec == "technology" or "auto" in ind or sec == "basic materials"
                or "chemical" in ind or "electrical equipment" in ind)
    if theme == "바이오":
        return sec == "healthcare"
    if theme == "방산":
        return "aerospace & defense" in ind
    if theme == "사이버보안":
        return sec == "technology" and "software - infrastructure" in ind
    if theme == "핀테크":
        if any(x in ind for x in FIN_EXCLUDED):
            return False
        return "credit services" in ind or "financial data" in ind or "fintech" in ind or "payment" in ind
    return False


# ---------------------------------------------------------------- 분류
def classify(stocks: list[dict]) -> None:
    """stock['trends'] = [{theme, score, article}], stock['trend_status'] 채우기. fetch_company_info 선행 필요."""
    import numpy as np
    if _model_thread is not None:
        _model_thread.join()
    model = _load_model()
    names = list(CATEGORIES)
    anchors, owner = [], []
    for n in names:
        for phrase in CATEGORIES[n]:
            anchors.append(phrase)
            owner.append(n)
    a_emb = model.encode(anchors, normalize_embeddings=True)

    texts, idx = [], []  # (stock index, article index)
    for si, s in enumerate(stocks):
        for ai, art in enumerate(s.get("articles", [])):
            if art["media"] in ("SEC EDGAR", "DART"):
                continue
            texts.append(f"{art['title']} {art['content']}")
            idx.append((si, ai))
    sims = model.encode(texts, normalize_embeddings=True) @ a_emb.T if texts else np.zeros((0, len(anchors)))

    best = [dict() for _ in stocks]  # stock -> theme -> (score, article idx)
    for row, (si, ai) in zip(sims, idx):
        for k, theme in enumerate(owner):
            sc = float(row[k])
            if sc >= SIM_THRESHOLD and sc > best[si].get(theme, (0, None))[0]:
                if theme in MACRO_THEMES:
                    art = stocks[si]["articles"][ai]
                    if not MACRO_KEYWORDS.search(f"{art['title']} {art['content']}"):
                        continue
                best[si][theme] = (sc, ai)

    for s, matches in zip(stocks, best):
        s["trends"], unconfirmed = [], False
        for theme, (sc, ai) in sorted(matches.items(), key=lambda kv: -kv[1][0]):
            if theme in MACRO_THEMES:
                ok = True
            elif not s.get("sector"):
                unconfirmed = True  # 섹터 조회 실패 → 텍스트 매칭만으로 확정하지 않는다
                continue
            else:
                ok = sector_allows(theme, s["sector"], s.get("industry"))
            if ok:
                s["trends"].append({"theme": theme, "score": round(sc, 3), "article": s["articles"][ai]})
        s["trend_status"] = ("트렌드 미확정(섹터 미확인)" if unconfirmed and not s["trends"]
                             else ", ".join(t["theme"] for t in s["trends"]) or "해당 없음")
