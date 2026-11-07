"""통계 계산 - Python only (architecture.md §4). LLM 에는 어떤 산수도 맡기지 않는다."""
from __future__ import annotations

import calendar
import datetime as dt
import json
import math

import pandas as pd

from merchant_normalizer import LONG_TERM_DIR

SUMMARY_FILE = LONG_TERM_DIR / "monthly_summary.json"
MIN_OVERSPEND_AMOUNT = 50_000       # Top 3 후보 최소 지출
MERCHANT_FLAG_AMOUNT = 1_000_000    # guardrails: 단일 가맹점 월 100만 원 이상
FALLBACK_SAVING_RATE = 0.10         # 전월 비교가 불가할 때의 절감 추정 비율


def prev_month_key(month: str) -> str:
    y, m = int(month[:4]), int(month[5:])
    return f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"


def coverage_days(month_df: pd.DataFrame, month: str) -> int:
    """자료가 실제로 덮는 일수: 해당 월 첫 거래일 ~ 마지막 거래일 (파일이 월 중간에서 시작/끝날 수 있음)."""
    return max((max(month_df["date"]) - min(month_df["date"])).days + 1, 1)


def month_summary(df: pd.DataFrame, month: str) -> dict:
    cur = df[df["month"] == month]
    total = int(cur["amount"].sum())
    days = coverage_days(cur, month)
    cats = {}
    for c, g in cur.groupby("category"):
        s = int(g["amount"].sum())
        cats[c] = {
            "sum": s, "count": int(len(g)),
            "mean": float(g["amount"].mean()), "std": float(g["amount"].std(ddof=0)),
            "share": (s / total * 100.0) if total else 0.0,
        }
    return {"total": total, "count": int(len(cur)), "coverage_days": days,
            "daily_avg": total / days, "avg_per_txn": (total / len(cur)) if len(cur) else 0.0,
            "categories": cats}


def load_summary() -> dict:
    if SUMMARY_FILE.exists():
        return json.loads(SUMMARY_FILE.read_text(encoding="utf-8"))
    return {}


def update_monthly_summary(df: pd.DataFrame) -> dict:
    """입력에 포함된 모든 월을 monthly_summary.json 에 (덮어쓰기 방식으로) 누적 → 재실행해도 중복되지 않음."""
    store = load_summary()
    for month in sorted(df["month"].unique()):
        s = month_summary(df, month)
        store[month] = {"total": s["total"], "count": s["count"], "coverage_days": s["coverage_days"],
                        "categories": {c: {"sum": v["sum"], "count": v["count"], "mean": round(v["mean"], 1)}
                                       for c, v in s["categories"].items()}}
    SUMMARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return store


def _growth(cur_val: float, prev_val: float):
    return None if prev_val <= 0 else (cur_val / prev_val - 1.0) * 100.0


def compute_stats(df: pd.DataFrame, month: str, summary: dict, base: str | None = None) -> dict:
    """base: 비교 기준 월(YYYY-MM). 미지정 시 직전 월. 'A월 대비 B월' 요청이면 month=B, base=A."""
    cur = month_summary(df, month)
    pkey = base or prev_month_key(month)
    base_label = "전월" if pkey == prev_month_key(month) else f"{int(pkey[5:])}월"
    prev = summary[pkey] if pkey in summary else None

    # 증가율 기준: 두 달의 자료 일수가 같으면 월 합계, 다르면 일평균(부분 월을 공정하게 비교)
    basis = "합계"
    if prev is not None and prev["coverage_days"] != cur["coverage_days"]:
        basis = "일평균"

    def metric(total, days):
        return total if basis == "합계" else total / days

    cats = cur["categories"]
    for c, v in cats.items():
        v["prev_sum"] = None
        v["growth"] = None
        v["prev_metric"] = None
        v["z"] = None
        if prev is not None and c in prev["categories"]:
            v["prev_sum"] = prev["categories"][c]["sum"]
            pm = metric(prev["categories"][c]["sum"], prev["coverage_days"])
            v["prev_metric"] = pm
            v["growth"] = _growth(metric(v["sum"], cur["coverage_days"]), pm)
        hist = [summary[k]["categories"][c]["sum"] / summary[k]["coverage_days"]
                for k in sorted(summary) if k < month and c in summary[k]["categories"]]
        if len(hist) >= 3:  # 3개월 이상 누적 시 자가 평균 대비 z-score (일평균 기준)
            mu = sum(hist) / len(hist)
            sd = math.sqrt(sum((x - mu) ** 2 for x in hist) / len(hist))
            v["z"] = (v["sum"] / cur["coverage_days"] - mu) / sd if sd > 0 else None

    overall_growth = None
    if prev is not None:
        overall_growth = _growth(metric(cur["total"], cur["coverage_days"]), metric(prev["total"], prev["coverage_days"]))

    # 과소비 Top 3: 전월 대비 증가율 우선(절대 금액 아님), 최소 5만 원 이상 카테고리만
    cand = [(c, v) for c, v in cats.items() if c != "미분류" and v["sum"] >= MIN_OVERSPEND_AMOUNT]
    if prev is not None:
        pool = sorted([(c, v) for c, v in cand if v["growth"] is not None and v["growth"] > 0],
                      key=lambda t: t[1]["growth"], reverse=True)[:3]
        criterion = f"{base_label} 대비 증가율({basis} 기준) 상위, 월 5만 원 이상 지출 카테고리, 증가(+)한 항목만"
    else:
        pool = sorted(cand, key=lambda t: t[1]["share"], reverse=True)[:3]
        criterion = f"{base_label} 자료가 없어 증가율 산출 불가 → 비중 상위 3개 (참고용, 과소비 판정 아님)"
    top3 = []
    for c, v in pool:
        if v["growth"] is not None and v["prev_metric"] is not None:
            # 전월 수준(일평균 또는 합계)으로 돌아갈 때의 월 절감액 = 현재 대비 초과분
            cur_metric = metric(v["sum"], cur["coverage_days"])
            excess = (cur_metric - v["prev_metric"]) * (1 if basis == "합계" else cur["coverage_days"])
            saving, saving_basis = max(excess, 0.0), "전월 수준 복귀"
        else:
            saving, saving_basis = v["sum"] * FALLBACK_SAVING_RATE, "해당 카테고리 지출의 10% 가정"
        top3.append({"category": c, "sum": v["sum"], "share": v["share"], "growth": v["growth"],
                     "prev_sum": v["prev_sum"], "z": v["z"], "saving": saving, "saving_basis": saving_basis})

    # 주 사용처 Top 5 (로컬 리포트에만 실명 사용)
    month_df = df[df["month"] == month]
    prev_df = df[df["month"] == pkey]
    merchants = []
    for name, g in month_df.groupby("merchant"):
        merchants.append({"merchant": name, "sum": int(g["amount"].sum()), "count": int(len(g)),
                          "category": g["category"].iloc[0],
                          "prev_sum": int(prev_df[prev_df["merchant"] == name]["amount"].sum()) if len(prev_df) else None})
    merchants.sort(key=lambda m: m["sum"], reverse=True)
    top_merchants = merchants[:5]
    for m in merchants:
        m["flag_1m"] = m["sum"] >= MERCHANT_FLAG_AMOUNT

    # 검증용: 카테고리 합계 = 총 사용액
    cat_sum = sum(v["sum"] for v in cats.values())
    return {
        "month": month, "base_label": base_label, "prev_month": pkey if prev is not None else None,
        "total": cur["total"], "count": cur["count"], "coverage_days": cur["coverage_days"],
        "days_in_month": calendar.monthrange(int(month[:4]), int(month[5:]))[1],
        "daily_avg": cur["daily_avg"], "avg_per_txn": cur["avg_per_txn"],
        "prev_total": prev["total"] if prev is not None else None,
        "prev_coverage_days": prev["coverage_days"] if prev is not None else None,
        "growth_basis": basis, "overall_growth": overall_growth,
        "categories": cats, "top3": top3, "top3_criterion": criterion,
        "top_merchants": top_merchants,
        "total_saving": sum(t["saving"] for t in top3),
        "category_sum": cat_sum, "sum_matches": abs(cat_sum - cur["total"]) <= cur["total"] * 0.001,
    }
