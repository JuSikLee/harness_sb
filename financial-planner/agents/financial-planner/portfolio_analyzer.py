"""portfolio_analyzer.py - 가정값 + 위험 성향 x 은퇴까지 기간 기반 자산군 권장 비중 산출.

모든 비중은 permille(1/1000) 정수로 계산해 합계가 정확히 100.0%가 되도록 한다.
금액은 최대잉여법(largest remainder)으로 배분해 합계가 총자산과 정확히 일치한다.
구체 상품명은 어디에도 사용하지 않는다(자산군·일반 분류만).
"""
from __future__ import annotations

import os

from profile_loader import ASSET_CLASSES, UNCLASSIFIED

# --- 가정값 (specs/architecture.md §3) --------------------------------------
BASE_ASSUMPTIONS = {
    "inflation": 0.025,
    "duration_years": 30,
    "returns": {
        "국내 주식": 0.060, "해외 주식": 0.070, "국내 채권": 0.040, "해외 채권": 0.045,
        "현금성 자산": 0.025, "부동산": 0.030, "대체투자": 0.050,
    },
}
# 보수/표준/낙관: 수익률 -1.0%p/0/+1.0%p, 물가 +0.5%p/0/-0.5%p
_PROFILE_SHIFT = {"보수": (-0.010, +0.005), "표준": (0.0, 0.0), "낙관": (+0.010, -0.005)}
_ALIAS = {"conservative": "보수", "standard": "표준", "optimistic": "낙관", "보수": "보수", "표준": "표준", "낙관": "낙관"}


def get_assumptions(name: str | None = None) -> dict:
    """환경변수 ASSUMPTION_PROFILE(보수/표준/낙관) 반영한 가정값."""
    requested = (name or os.environ.get("ASSUMPTION_PROFILE") or "표준").strip()
    key = _ALIAS.get(requested.lower() if requested.isascii() else requested)
    note = ""
    if key is None:
        key, note = "표준", f"알 수 없는 ASSUMPTION_PROFILE '{requested}' -> 표준 적용"
    ret_shift, infl_shift = _PROFILE_SHIFT[key]
    return {
        "profile": key,
        "note": note,
        "inflation": round(BASE_ASSUMPTIONS["inflation"] + infl_shift, 6),
        "duration_years": BASE_ASSUMPTIONS["duration_years"],
        "returns": {c: round(r + ret_shift, 6) for c, r in BASE_ASSUMPTIONS["returns"].items()},
    }


# --- 권장 비중 (architecture.md §4) -----------------------------------------
# 버킷: 주식 / 채권 / 현금 / 대체 (permille)
_BUCKET_WEIGHTS = {
    "보수": {"주식": 300, "채권": 500, "현금": 100, "대체": 100},
    "중립": {"주식": 400, "채권": 300, "현금": 100, "대체": 200},
    "공격": {"주식": 600, "채권": 150, "현금": 50, "대체": 200},
}
SHORT_HORIZON_YEARS = 5
SHORT_HORIZON_SHIFT = 100  # 10%p
DOMESTIC_STOCK_PM = 600
DOMESTIC_BOND_PM = 700
GOV_BOND_PM = 600  # 국공채 60% / 회사채(AA 이상) 40%
REAL_ESTATE_SHARE_OF_ALT_PM = 500  # 대체 버킷 내 부동산 50% / 기타 대체 50%

CLASS_BUCKET = {
    "국내 주식": "주식", "해외 주식": "주식", "국내 채권": "채권", "해외 채권": "채권",
    "현금성 자산": "현금", "부동산": "대체", "대체투자": "대체",
}


def target_weights(risk: str, years_to_retire: int) -> dict:
    base = dict(_BUCKET_WEIGHTS[risk if risk in _BUCKET_WEIGHTS else "중립"])
    adjusted = years_to_retire < SHORT_HORIZON_YEARS
    if adjusted:
        base["주식"] -= SHORT_HORIZON_SHIFT
        base["채권"] += SHORT_HORIZON_SHIFT
    dom_stock = base["주식"] * DOMESTIC_STOCK_PM // 1000
    dom_bond = base["채권"] * DOMESTIC_BOND_PM // 1000
    real_estate = base["대체"] * REAL_ESTATE_SHARE_OF_ALT_PM // 1000
    classes = {
        "국내 주식": dom_stock, "해외 주식": base["주식"] - dom_stock,
        "국내 채권": dom_bond, "해외 채권": base["채권"] - dom_bond,
        "현금성 자산": base["현금"],
        "부동산": real_estate, "대체투자": base["대체"] - real_estate,
    }
    return {"buckets": base, "classes": classes, "short_horizon_adjusted": adjusted}


def allocate(total: int, weights_pm: dict[str, int]) -> dict[str, int]:
    """총액을 permille 가중치로 나눠 합계를 정확히 total로 맞춘다(최대잉여법)."""
    wsum = sum(weights_pm.values())
    base = {k: total * w // wsum for k, w in weights_pm.items()}
    rems = sorted(weights_pm, key=lambda k: (total * weights_pm[k]) % wsum, reverse=True)
    for k in rems[: total - sum(base.values())]:
        base[k] += 1
    return base


def weighted_return(weights_pm: dict[str, int], returns: dict[str, float]) -> float:
    return sum(weights_pm[c] * returns[c] for c in weights_pm) / sum(weights_pm.values())


def _sub_guides(rec: dict[str, int]) -> list[dict]:
    """자산군 내 일반 분류 가이드 (상품명 없음)."""
    gov = lambda amt: amt * GOV_BOND_PM // 1000  # noqa: E731
    out = []
    for cls in ASSET_CLASSES:
        amt = rec[cls]
        if cls == "국내 주식":
            g, detail = "광범위 시장 지수를 추종하는 인덱스 ETF 위주, 개별 종목은 소수·소액으로 제한", "-"
        elif cls == "해외 주식":
            g, detail = "선진국·신흥국 분산 인덱스 ETF 위주, 특정 국가·업종 쏠림 주의", "-"
        elif cls in ("국내 채권", "해외 채권"):
            g = "신용등급 AA 이상 위주, 국공채 비중을 회사채보다 높게"
            detail = f"국공채 {GOV_BOND_PM / 10:.0f}% ({gov(amt):,}원) · 회사채(AA 이상) {100 - GOV_BOND_PM / 10:.0f}% ({amt - gov(amt):,}원)"
        elif cls == "현금성 자산":
            g, detail = "비상자금(생활비 6개월분 이상)과 단기 목적 자금, 예금·단기 안전자산 위주", "-"
        elif cls == "부동산":
            g, detail = "실거주 주택 포함 여부 확인, 유동성·집중 위험 점검(대체 자산군으로 분류)", "-"
        else:
            g, detail = "금·원자재·인프라 등 주식·채권과 상관이 낮은 자산을 소액 분산", "-"
        out.append({"asset_class": cls, "amount": amt, "guide": g, "detail": detail})
    return out


def analyze(profile: dict, assumptions: dict) -> dict:
    total = int(profile["total_assets"])
    years = profile["retirement_age"] - profile["age"]
    tw = target_weights(profile["risk_tolerance"], years)
    rec_amount = allocate(total, tw["classes"])
    current = profile["current_assets"] if profile["distribution_known"] else {}
    tol = total * 5 // 1000  # 0.5% 미만 차이는 '유지'
    rows = []
    for cls in ASSET_CLASSES:
        cur = current.get(cls, 0)
        diff = rec_amount[cls] - cur
        if not profile["distribution_known"]:
            direction = "분포 미입력 (신규 배분 기준)"
        elif abs(diff) <= tol:
            direction = "유지"
        elif diff > 0:
            direction = "비중 확대 방향"
        else:
            direction = "비중 축소 방향"
        rows.append({"asset_class": cls, "bucket": CLASS_BUCKET[cls], "weight_pm": tw["classes"][cls],
                     "rec_amount": rec_amount[cls], "current_amount": cur, "diff": diff, "direction": direction})
    if current.get(UNCLASSIFIED):
        rows.append({"asset_class": UNCLASSIFIED, "bucket": "-", "weight_pm": 0, "rec_amount": 0,
                     "current_amount": current[UNCLASSIFIED], "diff": -current[UNCLASSIFIED],
                     "direction": "자산군 분류 확인 필요"})

    warnings = []
    for cls, cur in current.items():
        if total and cur * 2 >= total:
            warnings.append(f"위험 분산 검토: '{cls}' 비중이 {cur * 100 / total:.1f}%로 단일 자산군 50% 이상입니다.")
    if tw["short_horizon_adjusted"]:
        warnings.append(f"은퇴까지 {years}년(5년 미만)이라 주식 -10%p, 채권 +10%p 보수 쪽으로 자동 보정했습니다.")

    wr = weighted_return(tw["classes"], assumptions["returns"])
    return {
        "total": total,
        "risk": profile["risk_tolerance"],
        "years_to_retire": years,
        "bucket_weights": tw["buckets"],
        "class_weights": tw["classes"],
        "short_horizon_adjusted": tw["short_horizon_adjusted"],
        "rows": rows,
        "weighted_return": wr,
        "sub_guides": _sub_guides(rec_amount),
        "warnings": warnings,
        "distribution_known": profile["distribution_known"],
    }
