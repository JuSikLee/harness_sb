"""retirement_calculator.py - 노후 필요 자금·예상 자산·갭 계산 (전부 파이썬 산수).

FV(필요 자금)  = 노후 월지출 x 12 x 노후 기간 x (1+물가)^은퇴까지년수
예상 자산      = 현재 자산 x (1+r)^n + 월저축 x 복리누적계수(월복리, 기말 납입)
갭             = FV - 예상 자산
단순화: 은퇴 후 운용수익·세금·연금 수령은 반영하지 않는다(보수적 단순 모형, 가정값 섹션에 명시).
"""
from __future__ import annotations


def annuity_factor(annual_return: float, months: int) -> float:
    """월 납입 1원의 n개월 후 가치(기말 납입, 연 수익률을 월복리로 환산)."""
    if months <= 0:
        return 0.0
    m = (1 + annual_return) ** (1 / 12) - 1
    if abs(m) < 1e-12:
        return float(months)
    return ((1 + m) ** months - 1) / m


def project(*, age: int, retirement_age: int, assets: float, monthly_saving: float,
            retirement_monthly_expense: float, inflation: float, annual_return: float,
            duration_years: int) -> dict:
    n = max(0, retirement_age - age)
    infl_factor = (1 + inflation) ** n
    need = retirement_monthly_expense * 12 * duration_years * infl_factor
    growth = (1 + annual_return) ** n
    factor = annuity_factor(annual_return, n * 12)
    asset_fv = assets * growth
    saving_fv = monthly_saving * factor
    expected = asset_fv + saving_fv
    gap = need - expected
    if gap <= 0:
        extra = 0.0
    elif factor > 0:
        extra = gap / factor
    else:
        extra = None  # 은퇴 시점이 이미 도래: 추가 저축으로 해결 불가
    return {
        "years_to_retire": n, "inflation_factor": infl_factor, "need": need, "growth_factor": growth,
        "annuity_factor": factor, "asset_fv": asset_fv, "saving_fv": saving_fv, "expected": expected,
        "gap": gap, "extra_monthly": extra,
        "total_monthly_required": None if extra is None else monthly_saving + extra,
    }


def affordable_expense(params: dict, monthly_saving: float) -> float:
    """주어진 월 저축으로 감당 가능한 노후 월지출(현재 가치)."""
    r = project(**{**params, "monthly_saving": monthly_saving})
    denom = 12 * params["duration_years"] * r["inflation_factor"]
    return r["expected"] / denom


def compute(profile: dict, assumptions: dict, weighted_return: float) -> dict:
    params = {
        "age": profile["age"], "retirement_age": profile["retirement_age"], "assets": float(profile["total_assets"]),
        "monthly_saving": float(profile["monthly_saving_current"]),
        "retirement_monthly_expense": float(profile["retirement_monthly_expense"]),
        "inflation": assumptions["inflation"], "annual_return": weighted_return,
        "duration_years": assumptions["duration_years"],
    }
    result = project(**params)
    result["params"] = params

    # 가정 변경 시 결과 변동 (민감도)
    variants = [
        ("물가상승률 +0.5%p", {"inflation": params["inflation"] + 0.005}),
        ("기대수익률(가중평균) -1.0%p", {"annual_return": params["annual_return"] - 0.01}),
        ("기대수익률(가중평균) +1.0%p", {"annual_return": params["annual_return"] + 0.01}),
        ("노후 기간 35년(+5년)", {"duration_years": params["duration_years"] + 5}),
    ]
    sens = []
    for label, change in variants:
        r = project(**{**params, **change})
        sens.append({"label": label, "gap": r["gap"], "delta": r["gap"] - result["gap"], "extra_monthly": r["extra_monthly"]})
    result["sensitivity"] = sens
    return result


def crosscheck(result: dict) -> tuple[bool, str]:
    """독립 경로 검증: 월 단위 반복 시뮬레이션 + 공식 재계산 결과와 일치하는지 확인."""
    p = result["params"]
    n = result["years_to_retire"]
    m = (1 + p["annual_return"]) ** (1 / 12) - 1

    def simulate(pmt: float) -> float:
        bal = p["assets"]
        for _ in range(n * 12):
            bal = bal * (1 + m) + pmt
        return bal

    sim_expected = simulate(p["monthly_saving"])
    need = p["retirement_monthly_expense"] * 12 * p["duration_years"]
    for _ in range(n):
        need *= 1 + p["inflation"]
    checks = [
        ("예상 자산", sim_expected, result["expected"]),
        ("필요 자금", need, result["need"]),
        ("갭", need - sim_expected, result["gap"]),
    ]
    if result["gap"] > 0 and result["extra_monthly"] is not None:
        checks.append(("추가 저축 후 예상 자산", simulate(p["monthly_saving"] + result["extra_monthly"]), result["need"]))
    for name, a, b in checks:
        if abs(a - b) > max(1.0, abs(b) * 1e-9):
            return False, f"{name} 불일치: 시뮬레이션 {a:,.2f} vs 공식 {b:,.2f}"
    return True, "OK"
