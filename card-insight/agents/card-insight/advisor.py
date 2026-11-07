"""절약 제안 생성 (architecture.md §5, guardrails.md, system_prompt.md).

- LLM 에는 카테고리 단위 요약(1만 원 반올림 금액·증가율)만 전달. 가맹점명·날짜 없음.
- LLM 은 숫자를 쓰지 않는다(문장에 숫자가 있으면 검증 실패). 정량 근거는 Python 이 계산해 덧붙인다.
- 단정 표현은 후처리로 '검토 권장' 어조로 치환. 금융상품 추천 표현이 들어오면 해당 문장을 폐기.
"""
from __future__ import annotations

import json
import re

from merchant_normalizer import LLMUnavailable, call_llm, round_man, with_retry

ADVICE_MODEL = "claude-sonnet-5-5"
REFUSAL_MESSAGE = "이 요청은 PII 외부 전송 또는 단정 표현을 포함합니다. 에이전트는 익명화된 분석과 제안만 제공합니다."

# 단정 표현 → 완화 표현 (긴 패턴 먼저)
ASSERTIVE_RULES: list[tuple[str, str]] = [
    (r"끊\S*", "검토 권장"),
    (r"(사지|쓰지|먹지|마시지|하지|가지) ?(마|말)\S*", "우선 줄여볼 만함"),
    (r"(절대|무조건|당장|반드시) ?", ""),
    (r"안 ?(돼|됩니다|된다)\S*", "검토 권장"),
    (r"(줄여야|아껴야|바꿔야) ?(한다|합니다|해요|해)\S*", "줄이는 것을 고려해 볼 만함"),
    (r"금지\S*", "검토 권장"),
]
_ASSERTIVE_COMPILED = [(re.compile(p), r) for p, r in ASSERTIVE_RULES]
FINANCIAL_PRODUCT_RE = re.compile(r"대출|신용카드 ?(추천|발급|갈아)|카드 ?(추천|발급|신청|갈아타)|적금|펀드|보험 ?(가입|추천)|상품 ?(추천|가입)|투자 ?상품")

CATEGORY_FALLBACK_TIPS = {
    "식비": "배달·외식 횟수를 점검하고 장보기·도시락 비중을 늘리는 방안을 검토 권장",
    "카페": "방문 횟수를 줄이거나 텀블러·원두 구매 등 대안을 고려해 볼 만함",
    "교통": "정기권·환승 할인 등 정액 옵션과 비교해 보는 것을 검토 권장",
    "쇼핑": "구매 전 대기 기간을 두고 할부 결제 건을 우선 점검해 볼 만함",
    "의료": "정기 검진·필수 의료비 중심으로 구분해 점검하는 것을 검토 권장",
    "여가": "이용 빈도가 낮은 항목을 정리하고 대체 활동을 비교해 보는 것을 고려",
    "구독": "사용 빈도가 낮은 구독을 점검하고 중복 서비스를 정리하는 것을 검토 권장",
    "생활비": "고정비 항목을 요금제·사용량 기준으로 재점검하는 것을 검토 권장",
    "기타": "세부 항목을 분류해 비중이 큰 곳부터 점검하는 것을 검토 권장",
}


def sanitize_assertive(text: str) -> tuple[str, bool]:
    """단정 표현을 '검토 권장' 어조로 치환. (결과, 치환 여부)"""
    out, changed = text, False
    for pat, repl in _ASSERTIVE_COMPILED:
        new = pat.sub(repl, out)
        changed = changed or new != out
        out = new
    return re.sub(r"\s{2,}", " ", out).strip(), changed


def has_assertive(text: str) -> bool:
    return any(p.search(text) for p, _ in _ASSERTIVE_COMPILED)


def _targets(stats: dict, topic: str | None) -> list[str]:
    cats = [t["category"] for t in stats["top3"]]
    if topic and topic in stats["categories"] and topic not in cats:
        cats.append(topic)
    return cats


def build_payload(stats: dict, cats: list[str]) -> str:
    """LLM 에 보내는 유일한 입력: 카테고리명·1만 원 반올림 금액·로컬 계산 비율. 가맹점명·날짜 없음."""
    rows = []
    for c in cats:
        v = stats["categories"][c]
        rows.append({"category": c, "amount": round_man(v["sum"]),
                     "share_pct": round(v["share"], 1),
                     "growth_pct": None if v["growth"] is None else round(v["growth"], 1)})
    return json.dumps({"items": rows}, ensure_ascii=False)


_SYSTEM = (
    "너는 가계 절약 코치다(조언자·중립적·증거 기반). 입력은 카테고리별 요약이다. "
    "각 카테고리마다 줄일 수 있는 방향을 한국어 한 문장(50자 내외)으로 제안하라. "
    "규칙: 1) 숫자를 절대 쓰지 마라(수치는 시스템이 따로 붙인다). 2) '끊어'·'사지 마'·'안 돼' 같은 단정 표현 금지 — "
    "'검토 권장'·'줄이는 것을 고려' 어조 사용. 3) 특정 금융상품·카드·대출 추천 금지. 4) 가맹점 이름 언급 금지. "
    '출력은 JSON만: {"suggestions":[{"category":"...","text":"..."}]}'
)


def _parse(text: str, cats: list[str]) -> dict[str, str]:
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0))
    return {s["category"]: s["text"].strip() for s in data["suggestions"]}


def _valid(got, cats: list[str]) -> bool:
    if not got or any(c not in got for c in cats):
        return False
    for t in got.values():
        if re.search(r"\d", t) or FINANCIAL_PRODUCT_RE.search(t):  # LLM 산수·상품 추천 차단
            return False
    return True


def evidence_text(stats: dict, cat: str) -> str:
    """정량 근거 - 전부 Python 계산값."""
    v = stats["categories"][cat]
    parts = []
    if v["growth"] is not None:
        parts.append(f"{stats.get('base_label', '전월')} 대비 {v['growth']:+.1f}%({stats['growth_basis']} 기준)")
    parts.append(f"전체 지출의 {v['share']:.1f}%({v['sum']:,}원)")
    if v["z"] is not None:
        parts.append(f"자가 평균 대비 z={v['z']:+.2f}")
    return ", ".join(parts)


def saving_for(stats: dict, cat: str):
    for t in stats["top3"]:
        if t["category"] == cat:
            return t["saving"], t["saving_basis"]
    v = stats["categories"][cat]
    return v["sum"] * 0.10, "해당 카테고리 지출의 10% 가정"


def generate_advice(stats: dict, topic: str | None = None) -> dict:
    cats = _targets(stats, topic)
    notes = []
    texts: dict[str, str] = {}
    if cats:
        payload = build_payload(stats, cats)
        try:
            got, ok, tries = with_retry(
                lambda: _parse(call_llm("advice", ADVICE_MODEL, _SYSTEM, payload, 900), cats),
                lambda r: _valid(r, cats))
            if ok:
                texts = got
                notes.append(f"LLM 제안 사용(시도 {tries}회)")
            else:
                notes.append("LLM 출력이 검증(숫자·상품추천 없음)을 통과하지 못해 로컬 문구로 대체")
        except LLMUnavailable as e:
            notes.append(f"LLM 미사용({e}) → 로컬 문구")

    items = []
    blocked = 0
    for c in cats:
        raw = texts[c] if c in texts else CATEGORY_FALLBACK_TIPS[c]
        text, changed = sanitize_assertive(raw)
        blocked += int(changed)
        saving, basis = saving_for(stats, c)
        ev = evidence_text(stats, c)
        # 검증: 각 제안에 정량 근거(% 또는 원 단위 수치) 1개 이상
        assert re.search(r"\d+(\.\d+)?%", ev) and "원" in ev
        items.append({"category": c, "text": text, "evidence": ev,
                      "saving": int(round(saving, -3)), "saving_basis": basis})
    return {"items": items, "assertive_blocked": blocked, "notes": notes}
