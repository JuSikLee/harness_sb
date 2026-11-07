"""가맹점 정규화 + 익명화 토큰 + LLM 게이트웨이 (architecture.md §1·§2).

이 모듈이 anthropic 라이브러리를 호출하는 유일한 출구(call_llm)다.
- 호출 직전 payload 검사: 등록된 가맹점명·카드/전화번호·날짜·1만 원 단위가 아닌 금액이 보이면 차단
- 모든 호출 payload 는 memory/card/working/llm_calls.log 에 기록 (원문 노출 여부 사후 점검용)
- 한 요청당 토큰 40,000 / 60초 한도 추적
"""
from __future__ import annotations

import json
import os
import re
import time
import datetime as dt
from pathlib import Path

from csv_loader import PROJECT_ROOT, mask_pii

MEMORY_DIR = PROJECT_ROOT / "memory" / "card"
LONG_TERM_DIR = MEMORY_DIR / "long_term"
WORKING_DIR = MEMORY_DIR / "working"
LLM_LOG = WORKING_DIR / "llm_calls.log"
CACHE_FILE = WORKING_DIR / "this_month_anonymized.json"
CACHE_TTL_DAYS = 7

TOKEN_BUDGET = 40_000
TIME_BUDGET = 60.0

# ---------------------------------------------------------------- 정규화
# (정규식, 표준명) - 위에서부터 첫 매칭. 확장 가능한 사전.
ALIASES: list[tuple[str, str]] = [
    (r"스타벅스|starbucks", "스타벅스"), (r"투썸", "투썸플레이스"), (r"메가\s*(mgc)?커피|mega\s*coffee", "메가커피"),
    (r"이디야", "이디야커피"), (r"공차", "공차"), (r"더벤티", "더벤티"), (r"파리바게[뜨트]|paris\s*baguette", "파리바게뜨"),
    (r"bhc", "BHC치킨"), (r"도미노", "도미노피자"), (r"롯데리아", "롯데리아"), (r"맥도날드|mcdonald", "맥도날드"),
    (r"버거킹|burger\s*king", "버거킹"), (r"노브랜드버거", "노브랜드버거"), (r"샐러디", "샐러디"), (r"엽기떡볶이", "엽기떡볶이"),
    (r"한솥", "한솥도시락"), (r"다나과일", "다나과일나라"), (r"하나로마트", "강서농협하나로마트"), (r"홈플러스", "홈플러스"),
    (r"롯데마트", "롯데마트"), (r"gs\s*25", "GS25"), (r"다이소", "다이소"), (r"교보문고", "교보문고"), (r"메가박스", "메가박스"),
    (r"예스이십사|yes24", "예스24"), (r"올리브영", "올리브영"), (r"쿠팡|coupang", "쿠팡"), (r"컬리", "컬리"),
    (r"현대홈쇼핑", "현대홈쇼핑"), (r"롯데홈쇼핑", "롯데홈쇼핑"), (r"에스에스지|ssg", "SSG닷컴"), (r"한샘", "한샘몰"),
    (r"아디다스|adidas", "아디다스"), (r"룰루레몬|lululemon", "룰루레몬"), (r"kt통신|^kt\b", "KT통신요금"), (r"한국전력", "한국전력"),
    (r"netflix", "Netflix"), (r"claude\.ai|anthropic", "Claude"), (r"github", "GitHub"), (r"vercel", "Vercel"),
    (r"notion", "Notion"), (r"dropbox", "Dropbox"), (r"google\s*one", "Google One"), (r"apple\.com", "Apple"),
]
_COMPILED = [(re.compile(p, re.I), c) for p, c in ALIASES]
_CORP = re.compile(r"\(주\)|\(유\)|㈜|주식회사|유한회사|유한회$|유한$")
_UNMATCHED: set[str] = set()


def normalize_merchant(raw: str) -> str:
    """'스타벅스 강남점'·'STARBUCKS#KANG' → '스타벅스'. 사전 → PG 접두 제거 → 지점 제거 순."""
    s = mask_pii(raw).strip()
    for pat, canon in _COMPILED:
        if pat.search(s):
            return canon
    if " - " in s:  # 'KCP_결제 - 상호' 같은 PG 표기는 실제 상호 쪽만 사용
        s = s.split(" - ", 1)[1]
    s = _CORP.sub("", s)
    s = s.split("#")[0].split("_")[0]
    s = re.sub(r"\s+\S*점$", "", s).strip()
    out = s or mask_pii(raw).strip()
    _UNMATCHED.add(out)
    return out


def unmatched_merchants() -> list[str]:
    return sorted(_UNMATCHED)


# ---------------------------------------------------------------- 익명화
class Anonymizer:
    """표준 가맹점명 ↔ 토큰(M001~). 역매핑은 메모리에만 존재하고 디스크·LLM 어디에도 쓰지 않는다."""

    def __init__(self, merchants):
        names = sorted(set(merchants))
        self._to_token = {n: f"M{i + 1:03d}" for i, n in enumerate(names)}
        self._to_name = {t: n for n, t in self._to_token.items()}
        register_sensitive(names)

    def token(self, name: str) -> str:
        return self._to_token[name]

    def name(self, token: str) -> str:
        return self._to_name[token]

    def __len__(self):
        return len(self._to_token)


def round_man(amount) -> int:
    """1만 원 단위 반올림 (LLM 에 보내는 금액은 항상 이 값)."""
    return int(round(float(amount) / 10_000.0)) * 10_000


def purge_old_cache(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now()
    if not CACHE_FILE.exists():
        return False
    try:
        created = dt.datetime.fromisoformat(json.loads(CACHE_FILE.read_text(encoding="utf-8"))["created_at"])
    except (OSError, ValueError, KeyError):
        created = dt.datetime.fromtimestamp(CACHE_FILE.stat().st_mtime)
    if now - created > dt.timedelta(days=CACHE_TTL_DAYS):
        CACHE_FILE.unlink()
        return True
    return False


def save_anonymized_cache(month: str, rows: list[dict]) -> None:
    """익명화 트랜잭션 캐시: 토큰·카테고리·반올림 금액만. 처리 후 7일 지나면 다음 실행에서 삭제."""
    WORKING_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps({"created_at": dt.datetime.now().isoformat(timespec="seconds"), "month": month, "rows": rows},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- 재시도
def with_retry(fn, validate, attempts: int = 3):
    """retry_policy: 검증 실패 시 해당 단계만 최대 3회 재시도. (결과, 성공여부, 시도횟수) 반환."""
    last = None
    for n in range(1, attempts + 1):
        try:
            last = fn()
        except LLMUnavailable:
            raise
        except Exception:  # noqa: BLE001 - 단계 실패는 재시도 대상
            last = None
            continue
        if validate(last):
            return last, True, n
    return last, False, attempts


# ---------------------------------------------------------------- LLM 게이트웨이
class LLMUnavailable(Exception):
    pass


_START = time.monotonic()
_state = {"tokens": 0, "calls": 0}
_sensitive: set[str] = set()
_DATE_RE = re.compile(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}")


def reset_run_state() -> None:
    global _START
    _START = time.monotonic()
    _state["tokens"] = 0
    _state["calls"] = 0
    WORKING_DIR.mkdir(parents=True, exist_ok=True)
    LLM_LOG.write_text("", encoding="utf-8")


def register_sensitive(names) -> None:
    for n in names:
        if n and len(n) >= 2:
            _sensitive.add(n.lower())


def remaining_seconds() -> float:
    return TIME_BUDGET - (time.monotonic() - _START)


def llm_usage() -> dict:
    return dict(_state)


def assert_safe_payload(text: str) -> None:
    low = text.lower()
    for name in _sensitive:
        if name in low:
            raise LLMUnavailable("차단: payload 에 가맹점명 원문이 포함됨")
    if mask_pii(text) != text:
        raise LLMUnavailable("차단: payload 에 카드번호/전화번호 등 PII 패턴이 포함됨")
    if _DATE_RE.search(text):
        raise LLMUnavailable("차단: payload 에 날짜가 포함됨")
    for num in re.findall(r"\d+", text):
        if int(num) >= 1000 and int(num) % 10_000 != 0:
            raise LLMUnavailable("차단: 1만 원 단위로 반올림되지 않은 금액이 포함됨")


def _log(entry: dict) -> None:
    WORKING_DIR.mkdir(parents=True, exist_ok=True)
    with open(LLM_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def call_llm(purpose: str, model: str, system: str, user: str, max_tokens: int = 1200) -> str:
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise LLMUnavailable("ANTHROPIC_API_KEY 없음")
    assert_safe_payload(system + "\n" + user)
    est = (len(system) + len(user)) // 2 + max_tokens
    if _state["tokens"] + est > TOKEN_BUDGET:
        raise LLMUnavailable("토큰 한도(40,000) 초과 예상")
    rem = remaining_seconds()
    if rem < 8:
        raise LLMUnavailable("시간 한도(60초) 임박")
    try:
        import anthropic
    except ImportError as e:
        raise LLMUnavailable("anthropic 라이브러리 없음") from e
    _log({"purpose": purpose, "model": model, "system": system, "user": user})
    try:
        client = anthropic.Anthropic(timeout=min(25.0, rem - 3), max_retries=0)
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:  # noqa: BLE001 - API 오류는 로컬 폴백으로 처리
        _log({"purpose": purpose, "error": type(e).__name__})
        raise LLMUnavailable(f"API 호출 실패: {type(e).__name__}") from e
    used = resp.usage.input_tokens + resp.usage.output_tokens
    _state["tokens"] += used
    _state["calls"] += 1
    _log({"purpose": purpose, "tokens": used})
    return "".join(b.text for b in resp.content if b.type == "text")
