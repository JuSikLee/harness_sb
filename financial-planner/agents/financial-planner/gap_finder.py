"""gap_finder.py - 노후 갭을 메우기 위한 월 저축 권장액·자산군 분배·대안 시나리오.

월 저축 권장액은 월 가용 소득(월소득-월지출) 이내로 제한한다. 초과 시 '은퇴 연기'·'노후 지출 조정'
시나리오를 함께 제시한다. 모든 계산은 파이썬(retirement_calculator)으로 수행한다.
"""
from __future__ import annotations

import math

from portfolio_analyzer import allocate
from retirement_calculator import affordable_expense, project

MAN = 10_000
DELAY_YEARS = (1, 2)
BUCKET_LABELS = {
    "주식": "주식 (인덱스 ETF 위주)",
    "채권": "채권 (국공채·AA 이상 회사채)",
    "현금": "현금성 (비상자금·단기 자금)",
    "대체": "대체·연금 (분산형)",
}


def find_gap(profile: dict, portfolio: dict, retirement: dict, assumptions: dict) -> dict:
    params = retirement["params"]
    savable = int(profile["monthly_savable"])
    savable_cap = savable // MAN * MAN  # 만원 단위 내림
    current_saving = int(profile["monthly_saving_current"])

    gap = retirement["gap"]
    extra = retirement["extra_monthly"]
    required_total = None if extra is None else current_saving + extra
    required_total_man = None if required_total is None else math.ceil(required_total / MAN) * MAN
    recommended = savable_cap if required_total_man is None else min(required_total_man, savable_cap)
    feasible = gap <= 0 or (required_total_man is not None and required_total_man <= savable_cap)
    shortfall_monthly = 0 if feasible else (None if required_total_man is None else required_total_man - savable_cap)

    alloc_units = allocate(recommended // MAN, portfolio["bucket_weights"])
    allocation = [
        {"bucket": b, "label": BUCKET_LABELS[b], "weight_pm": portfolio["bucket_weights"][b], "amount": alloc_units[b] * MAN}
        for b in ("주식", "채권", "현금", "대체")
    ]

    scenarios: dict = {"delay": [], "spending": None}
    if not feasible:
        for d in DELAY_YEARS:
            r = project(**{**params, "retirement_age": params["retirement_age"] + d})
            req = None if r["extra_monthly"] is None else params["monthly_saving"] + r["extra_monthly"]
            req_man = None if req is None else math.ceil(req / MAN) * MAN
            scenarios["delay"].append({
                "years": d, "retirement_age": params["retirement_age"] + d, "need": r["need"], "expected": r["expected"],
                "gap": r["gap"], "required_total_monthly": req_man,
                "feasible": r["gap"] <= 0 or (req_man is not None and req_man <= savable_cap),
            })
        afford = affordable_expense(params, float(savable_cap))
        target = params["retirement_monthly_expense"]
        scenarios["spending"] = {
            "target": target, "affordable": afford, "reduction": max(0.0, target - afford),
            "reduction_pct": max(0.0, (target - afford) / target * 100) if target else 0.0,
        }

    notes = []
    if gap <= 0:
        notes.append("현재 가정 기준으로 노후 필요 자금을 충족합니다(추가 저축 불필요).")
    elif feasible:
        notes.append("권장 월 저축액은 월 가용 소득 이내이며, 이 수준을 은퇴 시점까지 유지하면 갭이 해소됩니다.")
    else:
        notes.append("필요 월 저축액이 월 가용 소득을 초과합니다. 은퇴 연기 또는 노후 지출 조정 시나리오를 함께 검토하세요.")
    if current_saving > savable:
        notes.append("입력된 현재 월 저축액이 월 가용 소득(월소득-월지출)보다 큽니다. 입력값을 확인하세요.")

    return {
        "savable": savable, "savable_cap": savable_cap, "current_saving": current_saving,
        "required_total_monthly": required_total_man, "required_exact": required_total,
        "recommended_monthly": recommended, "feasible": feasible, "shortfall_monthly": shortfall_monthly,
        "allocation": allocation, "scenarios": scenarios, "notes": notes,
    }
