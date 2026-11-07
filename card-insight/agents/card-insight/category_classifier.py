"""카테고리 분류 (architecture.md §3, memory_policy.md).

1차 merchant_category_map.json 사전 → 2차 미매칭만 LLM(익명화 토큰·요약 특성만 전달) → 3차 신뢰도 < 0.4 는 '미분류'.
LLM 신뢰도 ≥ 0.9 → 사전에 confirmed=true 로 자동 저장·적용, 0.9 미만 → confirmed=false 후보로만 저장(이번 분석 미적용).
사용자에게 되묻지 않는다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

from merchant_normalizer import LONG_TERM_DIR, Anonymizer, LLMUnavailable, call_llm, round_man

CATEGORIES = ["식비", "카페", "교통", "쇼핑", "의료", "여가", "구독", "생활비", "기타"]
UNCLASSIFIED = "미분류"
MAP_FILE = LONG_TERM_DIR / "merchant_category_map.json"
CLASSIFY_MODEL = "claude-haiku-5-5"
RETRY_BELOW = 0.6   # retry_policy: 이 미만이면 재분류
MIN_CONF = 0.4      # 이 미만은 미분류
AUTO_CONFIRM = 0.9

SEED_MAP = {
    "식비": ["BHC치킨", "도미노피자", "롯데리아", "맥도날드", "버거킹", "노브랜드버거", "샐러디", "엽기떡볶이",
            "한솥도시락", "다나과일나라", "파리바게뜨"],
    "카페": ["스타벅스", "투썸플레이스", "메가커피", "이디야커피", "공차", "더벤티"],
    "쇼핑": ["아디다스", "룰루레몬", "현대홈쇼핑", "롯데홈쇼핑", "쿠팡", "컬리", "SSG닷컴", "올리브영", "한샘몰"],
    "여가": ["메가박스", "교보문고", "예스24"],
    "구독": ["Netflix", "Claude", "GitHub", "Vercel", "Notion", "Dropbox", "Google One", "Apple"],
    "생활비": ["강서농협하나로마트", "홈플러스", "롯데마트", "GS25", "다이소", "KT통신요금", "한국전력"],
}


def load_map(path: Path = MAP_FILE) -> dict:
    if not path.exists():
        seeded = {m: {"category": c, "confidence": 1.0, "confirmed": True, "source": "seed"}
                  for c, ms in SEED_MAP.items() for m in ms}
        save_map(seeded, path)
        return seeded
    return json.loads(path.read_text(encoding="utf-8"))


def save_map(m: dict, path: Path = MAP_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(m, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


_SYSTEM = (
    "너는 카드 거래 분류기다. 입력은 익명화된 거래 요약(토큰, 건수, 합계와 평균 금액(1만 원 단위 반올림), 할부 비율)뿐이다. "
    f"후보 카테고리: {', '.join(CATEGORIES)}. 정보가 부족하면 억지로 추정하지 말고 낮은 confidence(0~1)를 줘라. "
    '출력은 JSON 배열만: [{"token":"M001","category":"...","confidence":0.0}]. 설명·계산 금지.'
)


def _features(df: pd.DataFrame, names: list[str], anon: Anonymizer) -> list[dict]:
    rows = []
    for n in names:
        sub = df[df["merchant"] == n]
        rows.append({
            "token": anon.token(n),
            "count": int(len(sub)),
            "total": round_man(sub["amount"].sum()),
            "avg": round_man(sub["amount"].mean()),
            "installment_ratio": round(float((sub["installment"] == "할부").mean()), 2),
        })
    return rows


def _parse(text: str) -> dict[str, tuple[str, float]]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("JSON 배열 없음")
    out = {}
    for item in json.loads(m.group(0)):
        cat, conf = item["category"], float(item["confidence"])
        if cat in CATEGORIES and 0.0 <= conf <= 1.0:
            out[item["token"]] = (cat, conf)
    return out


def classify_with_llm(df: pd.DataFrame, names: list[str], anon: Anonymizer) -> dict[str, tuple[str, float]]:
    """미매칭 가맹점만 LLM 에 보낸다. 신뢰도 < 0.6 은 해당 항목만 최대 3회 재분류."""
    best: dict[str, tuple[str, float]] = {}
    pending = list(names)
    for _ in range(3):
        if not pending:
            break
        payload = json.dumps(_features(df, pending, anon), ensure_ascii=False)
        try:
            got = _parse(call_llm("classify", CLASSIFY_MODEL, _SYSTEM, payload, 1000))
        except LLMUnavailable:
            break
        except (ValueError, KeyError, TypeError):  # 형식 오류 → 다음 시도에서 재분류
            continue
        sent = {anon.token(x): x for x in pending}
        for tok, (cat, conf) in got.items():
            if tok in sent and (sent[tok] not in best or conf > best[sent[tok]][1]):
                best[sent[tok]] = (cat, conf)
        pending = [n for n in pending if n not in best or best[n][1] < RETRY_BELOW]
    return best


def classify_dataframe(df: pd.DataFrame, anon: Anonymizer, map_path: Path = MAP_FILE):
    cmap = load_map(map_path)
    result: dict[str, tuple[str, float]] = {}
    unknown = []
    for n in sorted(df["merchant"].unique()):
        if n in cmap and cmap[n]["confirmed"]:
            result[n] = (cmap[n]["category"], float(cmap[n]["confidence"]))
        elif n in cmap:  # 후보(confirmed=false): 이번 분석에는 적용하지 않는다
            result[n] = (UNCLASSIFIED, float(cmap[n]["confidence"]))
        else:
            unknown.append(n)

    info = {"dict_hits": len(result), "llm_targets": len(unknown), "auto_added": [], "candidates": []}
    if unknown:
        llm = classify_with_llm(df, unknown, anon)
        for n in unknown:
            if n not in llm:
                result[n] = (UNCLASSIFIED, 0.0)
                continue
            cat, conf = llm[n]
            if conf >= AUTO_CONFIRM:
                cmap[n] = {"category": cat, "confidence": conf, "confirmed": True, "source": "llm"}
                result[n] = (cat, conf)
                info["auto_added"].append(n)
            elif conf >= MIN_CONF:
                cmap[n] = {"category": cat, "confidence": conf, "confirmed": False, "source": "llm"}
                result[n] = (UNCLASSIFIED, conf)
                info["candidates"].append(n)
            else:  # 신뢰도 < 0.4: 미분류로 표시만, 사전에도 저장하지 않음
                result[n] = (UNCLASSIFIED, conf)
        if info["auto_added"] or info["candidates"]:
            save_map(cmap, map_path)

    out = df.copy()
    out["category"] = out["merchant"].map(lambda n: result[n][0])
    out["confidence"] = out["merchant"].map(lambda n: result[n][1])
    info["unclassified"] = sorted(n for n, (c, _) in result.items() if c == UNCLASSIFIED)
    return out, info
