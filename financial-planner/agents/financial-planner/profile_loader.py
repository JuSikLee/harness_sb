"""profile_loader.py - 입력 파싱(parse_intent 가드레일 포함) + 프로필 메모리 입출력.

규칙 (specs/architecture.md §1, §2 / guardrails.md / memory_policy.md)
- 가드레일(구체 상품·자동 거래·세금 단정 요청 탐지)은 parse_intent()에서 가장 먼저 수행한다.
  차단된 요청은 LLM 호출·계산·검색으로 절대 넘어가지 않는다.
- LLM(Claude API)은 자연어 -> 구조화 "표현 문자열" 파싱만 담당한다. 금액 환산 등 산수는 파이썬이 한다.
- 거래 API 호출 코드는 이 에이전트에 존재하지 않는다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MEMORY_DIR = ROOT / "memory" / "financial"
LONG_DIR = MEMORY_DIR / "long_term"
WORK_DIR = MEMORY_DIR / "working"
PROFILE_MD = LONG_DIR / "profile.md"
ASSET_HISTORY_JSON = LONG_DIR / "asset_history.json"
GOAL_HISTORY_MD = LONG_DIR / "goal_history.md"
RECENT_ASSUMPTIONS_JSON = WORK_DIR / "recent_assumptions.json"

LLM_MODEL = "claude-haiku-5-5"
LLM_TIMEOUT_SEC = 20

REFUSAL_MESSAGE = (
    "이 요청은 자동 거래 또는 구체 상품 추천을 포함합니다. 본 에이전트는 자산군별 가이드만 제공하며, "
    "구체 상품·세금 자문은 면허 받은 전문가에게 문의하면 됩니다."
)

# ---------------------------------------------------------------------------
# 1. 가드레일 (parse_intent 단계)
# ---------------------------------------------------------------------------
# 구체 상품명·티커·운용사/회사명 (고유명사). 사용자 입력에 들어오면 즉시 거부한다.
_PRODUCT_NAMES = (
    r"KODEX|TIGER|ARIRANG|KBSTAR|HANARO|KOSEF|\bACE\b|\bSOL\b|"
    r"삼성전자|하이닉스|현대차|네이버|카카오|LG에너지|셀트리온|포스코|테슬라|애플|엔비디아|마이크로소프트|아마존|"
    r"\b(?:SPY|QQQ|VOO|VTI|IVV|SCHD|TQQQ|SOXL|TSLA|AAPL|NVDA|MSFT|AMZN|GOOGL)\b"
)
# 금융회사명 (증권·은행). 일반 용어 '증권(사)'도 추천 맥락에서만 잡는다.
_FIRM_NAMES = (
    r"미래에셋|한국투자|NH투자|신한투자|메리츠|유안타|한화투자|이베스트|유진투자|하이투자|"
    r"국민은행|신한은행|우리은행|하나은행|농협|카카오뱅크|토스|케이뱅크|"
    r"[가-힣]{2,6}(?<!유가)증권(?!\s*계좌)"
)
_PRODUCT_RE = re.compile(f"(?:{_PRODUCT_NAMES}|{_FIRM_NAMES})", re.I)
_CODE_RE = re.compile(r"종목\s*코드|[(\[]\s*\d{6}\s*[)\]]")
# 상품 추천 요청 의도 (고유명사가 없어도 구체 상품을 요구하는 경우)
_RECOMMEND_RE = re.compile(
    r"(펀드|종목|ETF|상품|증권(?:사)?|브로커|보험)\s*\S{0,4}\s*(추천|골라|알려\s*줘|사야|사줘|가입)",
    re.I,
)
# 자동 거래
_TRADE_RE = re.compile(r"(매수|매도|주문\s*(?:넣|실행)|사\s*줘|팔아|자동\s*(?:매매|거래|매수|매도)|대신\s*(?:사|팔)|리밸런싱\s*실행)")
# 세금·상속·증여 단정 요청 (모든 세목)
_TAX_RE = re.compile(
    r"(세금|상속|증여|[가-힣]{0,6}소득세|양도세|취득세|재산세|종부세|종합부동산세|부가가치세|부가세|법인세|"
    r"종소세|금융소득종합과세|세액\s*공제|절세|과세|비과세|세무)"
)
# 연금 수령액 등 단정 수치 요청
_PENSION_RE = re.compile(r"연금.{0,8}(수령액|얼마\s*(?:받|나오)|받게\s*될|받을\s*금액)")


def parse_intent(request: str) -> dict:
    """요청을 가드레일로 먼저 검사한다. LLM/계산 이전 단계에서 호출해야 한다."""
    text = request or ""
    checks = (
        (_TRADE_RE, "자동 거래 요청"),
        (_PRODUCT_RE, "구체 상품·종목·금융회사명 포함"),
        (_CODE_RE, "종목코드 포함"),
        (_RECOMMEND_RE, "구체 상품 추천 요청"),
        (_TAX_RE, "세금·상속·증여 단정 요청"),
        (_PENSION_RE, "연금 수령액 단정 요청"),
    )
    for pattern, reason in checks:
        if pattern.search(text):
            extra = ""
            if "세금" in reason or "연금" in reason:
                extra = " (세금·연금·상속은 세무사·면허 재무설계사에게 문의하세요)"
            return {"blocked": True, "reason": reason, "message": REFUSAL_MESSAGE + extra}
    return {"blocked": False, "reason": "", "message": ""}


# ---------------------------------------------------------------------------
# 2. 금액 변환·표시 (파이썬 산수)
# ---------------------------------------------------------------------------
_NUM = r"\d[\d,]*(?:\.\d+)?"
_UNIT = r"(?:억|천만|백만|만)"
AMT = rf"{_NUM}\s*{_UNIT}?(?:\s*{_NUM}\s*(?:천만|백만|만))?(?:\s*원)?"
_UNIT_VALUE = {"억": 10**8, "천만": 10**7, "백만": 10**6, "만": 10**4, "원": 1}


def to_won(expr: str, default_unit: int = 10**4) -> int:
    """'5억 3천만', '700', '300만원' -> 원 단위 정수. 단위 없는 숫자는 default_unit(만원)."""
    tokens = re.findall(rf"({_NUM})\s*({_UNIT}|원)?", str(expr))
    if not tokens:
        raise ValueError(f"금액을 해석할 수 없음: {expr!r}")
    total = Decimal(0)
    for num, unit in tokens:
        mult = _UNIT_VALUE[unit] if unit else default_unit
        total += Decimal(num.replace(",", "")) * mult
    return int(total)


def format_won(amount: float) -> str:
    """원 -> '13억 8,252만원' 표기."""
    sign = "-" if amount < 0 else ""
    n = int(round(abs(amount)))
    eok, rest = divmod(n, 10**8)
    man = rest // 10**4
    if eok and man:
        return f"{sign}{eok:,}억 {man:,}만원"
    if eok:
        return f"{sign}{eok:,}억원"
    return f"{sign}{man:,}만원"


# ---------------------------------------------------------------------------
# 3. 자산군 매핑
# ---------------------------------------------------------------------------
ASSET_CLASSES = ["국내 주식", "해외 주식", "국내 채권", "해외 채권", "현금성 자산", "부동산", "대체투자"]
UNCLASSIFIED = "미분류"

_ASSET_KEY_RE = (
    r"국내\s*주식|해외\s*주식|국내\s*채권|해외\s*채권|주식|채권|예금|적금|현금|CMA|MMF|"
    r"부동산|아파트|연금|대체투자|리츠|원자재|코인|비트코인|(?<![가-힣])금"
)


def _classify(key: str) -> list[tuple[str, int]]:
    """키워드 -> [(자산군, 비율 permille)]. 포괄 키워드는 architecture 기본 배분(주식 6:4, 채권 7:3)."""
    k = re.sub(r"\s+", "", key)
    if k == "국내주식":
        return [("국내 주식", 1000)]
    if k == "해외주식":
        return [("해외 주식", 1000)]
    if k == "주식":
        return [("국내 주식", 600), ("해외 주식", 400)]
    if k == "국내채권":
        return [("국내 채권", 1000)]
    if k == "해외채권":
        return [("해외 채권", 1000)]
    if k == "채권":
        return [("국내 채권", 700), ("해외 채권", 300)]
    if k == "연금":  # 연금 계좌는 채권형 위주로 가정 (가정값 섹션에 명시)
        return [("국내 채권", 1000)]
    if k in ("예금", "적금", "현금", "CMA", "MMF"):
        return [("현금성 자산", 1000)]
    if k in ("부동산", "아파트"):
        return [("부동산", 1000)]
    return [("대체투자", 1000)]


# ---------------------------------------------------------------------------
# 4. 자연어 -> raw 표현 (LLM 우선, 실패 시 정규식)
# ---------------------------------------------------------------------------
def _regex_extract(text: str) -> dict:
    raw: dict = {}
    work = text

    def cut(m: re.Match | None) -> None:
        nonlocal work
        if m:
            work = work[: m.start()] + " " * (m.end() - m.start()) + work[m.end():]

    # 은퇴 나이
    m = re.search(r"(\d{2})\s*세\s*(?:에|까지)?\s*은퇴", work) or re.search(r"은퇴\D{0,6}(\d{2})\s*세", work)
    if m:
        raw["retirement_age"] = int(m.group(1))
        cut(m)
    # 노후 월 지출 (현재 가치)
    m = re.search(rf"노후\s*(?:월\s*)?(?:생활비|지출)?\s*(?:월\s*)?(?:목표\s*)?(?:은|이|:)?\s*({AMT})", work) or re.search(
        rf"은퇴\s*후\s*(?:월\s*)?(?:생활비|지출)?\s*(?:월\s*)?({AMT})", work
    )
    if m:
        raw["retirement_monthly_expense"] = m.group(1)
        cut(m)
    for field, pat in (
        ("monthly_income", r"(?:월\s*소득|월\s*수입|월급|월\s*급여)"),
        ("monthly_expense", r"(?:월\s*지출|월\s*생활비)"),
        ("monthly_saving_current", r"(?:월\s*저축(?:액)?|현재\s*저축)"),
        ("monthly_savable", r"(?:저축\s*가능(?:액)?|월\s*가용)"),
    ):
        m = re.search(rf"{pat}\s*(?:은|이|:)?\s*({AMT})", work)
        if m:
            raw[field] = m.group(1)
            cut(m)
    # 자산군별 보유
    classes: dict[str, str] = {}
    for m in re.finditer(rf"({_ASSET_KEY_RE})\s*(?:자산|보유|계좌)?\s*(?:은|이|:)?\s*({AMT})", work):
        classes[m.group(1)] = classes.get(m.group(1), "") + (" " if m.group(1) in classes else "") + m.group(2)
        cut(m)
    if classes:
        raw["current_assets"] = classes
    # 총자산
    m = re.search(rf"(?:총\s*)?(?:순?자산|보유\s*자산)\s*(?:은|이|:)?\s*({AMT})", work) or re.search(
        rf"({AMT})\s*(?:의\s*)?자산", work
    )
    if m:
        raw["total_assets"] = m.group(1)
        cut(m)
    # 나이
    m = re.search(r"(\d{2})\s*세", work) or re.search(r"나이\s*(\d{2})", work)
    if m:
        raw["age"] = int(m.group(1))
    # 위험 성향
    m = re.search(r"(보수|안정|중립|공격)", text)
    if m:
        raw["risk_tolerance"] = m.group(1)
    return raw


_LLM_SYSTEM = (
    "너는 한국어 재무 요청을 JSON으로 구조화하는 파서다. 계산·환산·추정은 하지 말고 사용자가 쓴 표현을 그대로 옮겨라. "
    "금액은 사용자가 쓴 문자열 그대로('5억', '700', '300만원') 문자열로 반환한다. 없는 항목은 키를 생략한다. "
    "키: age(int), retirement_age(int), total_assets(str), current_assets({자산군표현: 금액str}), "
    "monthly_income(str), monthly_expense(str), monthly_saving_current(str), monthly_savable(str), "
    "retirement_monthly_expense(str, 노후 월 지출 목표), risk_tolerance(보수|중립|공격). JSON 객체만 출력."
)


def _llm_extract(text: str, timeout: float) -> tuple[dict, int]:
    """Claude API로 파싱. 키 없음/오류 시 ({}, 0). 반환: (raw, 사용 토큰)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {}, 0
    try:
        import anthropic

        client = anthropic.Anthropic(timeout=max(1.0, timeout), max_retries=0)
        resp = client.messages.create(
            model=LLM_MODEL,
            max_tokens=600,
            system=_LLM_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        body = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", body, re.S)
        raw = json.loads(m.group(0)) if m else {}
        used = int(resp.usage.input_tokens + resp.usage.output_tokens)
        return (raw if isinstance(raw, dict) else {}), used
    except Exception:  # 네트워크·파싱 오류는 정규식 경로로 대체
        return {}, 0


# ---------------------------------------------------------------------------
# 5. raw -> 정규화 프로필
# ---------------------------------------------------------------------------
def _normalize(raw: dict) -> dict:
    out: dict = {}
    for key in ("age", "retirement_age"):
        if raw.get(key) is not None:
            try:
                v = int(raw[key])
            except (TypeError, ValueError):
                continue
            if 18 <= v <= 100:
                out[key] = v
    for key in ("monthly_income", "monthly_expense", "monthly_saving_current", "monthly_savable",
                "retirement_monthly_expense", "total_assets"):
        if raw.get(key) not in (None, ""):
            try:
                out[key] = to_won(raw[key])
            except ValueError:
                continue
    if isinstance(raw.get("current_assets"), dict):
        assets: dict[str, int] = {}
        for k, v in raw["current_assets"].items():
            try:
                amount = to_won(v)
            except ValueError:
                continue
            for cls, share in _classify(str(k)):
                assets[cls] = assets.get(cls, 0) + amount * share // 1000
            # 분할 시 나머지 원 단위는 첫 자산군에 귀속 (합계 보존)
            split = _classify(str(k))
            diff = amount - sum(amount * s // 1000 for _, s in split)
            assets[split[0][0]] += diff
        if assets:
            out["current_assets"] = assets
    risk = str(raw.get("risk_tolerance", "")).strip()
    if risk:
        out["risk_tolerance"] = "보수" if risk.startswith(("보수", "안정")) else "공격" if risk.startswith("공격") else "중립"
    return out


# ---------------------------------------------------------------------------
# 6. 메모리 (memory_policy.md)
# ---------------------------------------------------------------------------
_PROFILE_KEYS = ("age", "retirement_age", "current_assets", "total_assets", "monthly_income", "monthly_expense",
                 "monthly_saving_current", "monthly_savable", "retirement_monthly_expense", "risk_tolerance")


def load_stored_profile() -> dict:
    if not PROFILE_MD.exists():
        return {}
    m = re.search(r"```json\s*(\{.*?\})\s*```", PROFILE_MD.read_text(encoding="utf-8"), re.S)
    try:
        return json.loads(m.group(1)) if m else {}
    except json.JSONDecodeError:
        return {}


def _read_history_lines() -> list[str]:
    if not PROFILE_MD.exists():
        return []
    text = PROFILE_MD.read_text(encoding="utf-8")
    if "## 변경 이력" not in text:
        return []
    return [ln for ln in text.split("## 변경 이력", 1)[1].splitlines() if ln.startswith("- ")]


def save_profile(profile: dict, today: dt.date) -> list[str]:
    """프로필 갱신. 변경 시 변경일·이전 값을 이력에 남긴다. 변경된 키 목록 반환."""
    old = load_stored_profile()
    new = {k: profile[k] for k in _PROFILE_KEYS if k in profile}
    changes = [k for k in new if k in old and old[k] != new[k]]
    history = _read_history_lines()
    for k in changes:
        history.append(f"- {today.isoformat()}: {k}: {json.dumps(old[k], ensure_ascii=False)} → {json.dumps(new[k], ensure_ascii=False)}")
    LONG_DIR.mkdir(parents=True, exist_ok=True)
    body = (
        "# 사용자 프로필\n\n"
        f"최종 갱신: {today.isoformat()}\n\n"
        "```json\n" + json.dumps(new, ensure_ascii=False, indent=2) + "\n```\n\n"
        "## 변경 이력\n" + ("\n".join(history) + "\n" if history else "")
    )
    PROFILE_MD.write_text(body, encoding="utf-8")
    return changes


def save_goal_history(profile: dict, today: dt.date) -> None:
    """노후 목표(월지출·은퇴 시점)가 바뀌었거나 처음일 때만 기록."""
    LONG_DIR.mkdir(parents=True, exist_ok=True)
    entry = (profile["retirement_age"], profile["retirement_monthly_expense"])
    last = None
    if GOAL_HISTORY_MD.exists():
        rows = re.findall(r"^\|\s*\d{4}-\d{2}-\d{2}\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", GOAL_HISTORY_MD.read_text(encoding="utf-8"), re.M)
        if rows:
            last = (int(rows[-1][0]), int(rows[-1][1]))
    if last == entry:
        return
    if not GOAL_HISTORY_MD.exists():
        GOAL_HISTORY_MD.write_text(
            "# 노후 목표 변경 이력\n\n| 날짜 | 은퇴 나이 | 노후 월지출(원, 현재가치) | 이전 값 |\n|---|---|---|---|\n", encoding="utf-8")
    prev = f"{last[0]}세 / {last[1]}원" if last else "-"
    with GOAL_HISTORY_MD.open("a", encoding="utf-8") as f:
        f.write(f"| {today.isoformat()} | {entry[0]} | {entry[1]} | {prev} |\n")


def _quarter(d: dt.date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def load_previous_snapshot() -> dict | None:
    if not ASSET_HISTORY_JSON.exists():
        return None
    try:
        hist = json.loads(ASSET_HISTORY_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return hist[-1] if hist else None


def save_asset_snapshot(profile: dict, today: dt.date) -> None:
    """분기별 스냅샷 저장 (같은 분기면 덮어쓰기)."""
    hist: list = []
    if ASSET_HISTORY_JSON.exists():
        try:
            hist = json.loads(ASSET_HISTORY_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            hist = []
    snap = {"quarter": _quarter(today), "date": today.isoformat(), "total_assets": profile["total_assets"],
            "current_assets": profile["current_assets"] if profile["distribution_known"] else {}}
    hist = [h for h in hist if h.get("quarter") != snap["quarter"]] + [snap]
    LONG_DIR.mkdir(parents=True, exist_ok=True)
    ASSET_HISTORY_JSON.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 7. 공개 API
# ---------------------------------------------------------------------------
REQUIRED = {
    "age": "현재 나이",
    "retirement_age": "은퇴 목표 나이",
    "total_assets": "총 자산(또는 자산군별 금액)",
    "retirement_monthly_expense": "노후 월 지출 목표(현재 가치)",
}


def load_profile(request: str, llm_timeout: float = LLM_TIMEOUT_SEC) -> dict:
    """요청 파싱 -> 메모리 프로필로 보충 -> 검증된 프로필 dict.

    반환 dict.missing 이 비어 있지 않으면 사용자에게 질문해야 한다.
    """
    raw_llm, tokens = _llm_extract(request, llm_timeout)
    parsed_llm = _normalize(raw_llm)
    parsed_re = _normalize(_regex_extract(request))
    # 정규식 결과를 우선(결정적), LLM은 정규식이 놓친 항목만 보충
    parsed = {**parsed_llm, **parsed_re}

    if not parsed:
        # 요청에서 아무 조건도 인식하지 못하면 저장된 프로필로 임의 진단하지 않고 질문한다.
        return {"missing": ["요청에서 인식된 조건이 없습니다. 나이·은퇴 나이·자산·월소득/월지출·노후 월 지출 중 "
                            "알려줄 항목(또는 '같은 조건인데 ~만 바꾸면' 형태)을 입력해주세요"],
                "sources": {}, "notes": [], "llm_tokens": tokens}

    stored = load_stored_profile()
    sources: dict[str, str] = {k: "입력" for k in parsed}
    merged = dict(parsed)

    # 자산: 입력에 자산 정보가 전혀 없을 때만 메모리에서 보충
    has_assets = "current_assets" in parsed or "total_assets" in parsed
    for k in _PROFILE_KEYS:
        if k in ("current_assets", "total_assets") and has_assets:
            continue
        if k not in merged and k in stored:
            merged[k] = stored[k]
            sources[k] = "메모리(profile.md)"

    if "risk_tolerance" not in merged:
        merged["risk_tolerance"] = "중립"
        sources["risk_tolerance"] = "기본값(중립)"

    breakdown = merged.get("current_assets", {})
    total = merged.get("total_assets")
    notes: list[str] = []
    if breakdown:
        s = sum(breakdown.values())
        if total is None:
            total = s
        elif total > s:
            breakdown = {**breakdown, UNCLASSIFIED: total - s}
            notes.append("총자산 대비 자산군 합계가 부족해 차액을 '미분류'로 처리했습니다.")
        elif total < s:
            total = s
            notes.append("자산군 합계가 입력 총자산보다 커 합계 기준으로 보정했습니다.")
    if total is not None:
        merged["total_assets"] = int(total)
    merged["current_assets"] = dict(breakdown)
    merged["distribution_known"] = bool(breakdown)

    # 월 저축 가능액
    if "monthly_savable" not in merged and "monthly_income" in merged and "monthly_expense" in merged:
        merged["monthly_savable"] = max(0, merged["monthly_income"] - merged["monthly_expense"])
        if merged["monthly_income"] < merged["monthly_expense"]:
            notes.append("월지출이 월소득보다 커서 월 저축 가능액을 0원으로 처리했습니다.")
    merged.setdefault("monthly_saving_current", 0)
    sources.setdefault("monthly_saving_current", "기본값(0원, 미입력)")

    missing = [label for k, label in REQUIRED.items() if k not in merged]
    if "monthly_savable" not in merged:
        missing.append("월소득·월지출(월 저축 가능액 산출용)")
    if not missing and merged["retirement_age"] < merged["age"]:
        missing.append("은퇴 나이는 현재 나이 이상이어야 합니다(은퇴 나이 재확인)")

    merged.update({"missing": missing, "sources": sources, "notes": notes, "llm_tokens": tokens})
    return merged


# ---------------------------------------------------------------------------
# 8. 가정값 캐시 (working/recent_assumptions.json)
# ---------------------------------------------------------------------------
def load_recent_assumptions() -> dict | None:
    if not RECENT_ASSUMPTIONS_JSON.exists():
        return None
    try:
        return json.loads(RECENT_ASSUMPTIONS_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_recent_assumptions(payload: dict) -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    RECENT_ASSUMPTIONS_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
