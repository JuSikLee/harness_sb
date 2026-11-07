"""카드 사용 내역 분석 에이전트 진입점.

사용법: python main.py "4월 지출 정리해줘"   ← 자연어 한 문장(request)만 받는다. --month 같은 옵션 금지.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
import time

import advisor
import category_classifier as cc
import csv_loader
import merchant_normalizer as mn
import report_writer
import statistics_engine as se

TONE_WORDS = ["끊", "단호", "강하게", "사지 마", "사지마", "하지 마", "하지마", "무조건", "혼내", "닥치고"]
TOPIC_SYNONYMS = {
    "식비": ["식비", "외식", "배달", "밥", "음식"], "카페": ["카페", "커피"], "구독": ["구독"], "쇼핑": ["쇼핑", "옷"],
    "교통": ["교통", "택시", "버스", "지하철"], "의료": ["의료", "병원", "약"], "여가": ["여가", "문화", "영화"],
    "생활비": ["생활비", "공과금", "통신"],
}


def parse_intent(request: str) -> dict:
    """자연어 한 문장 → {month, base_month, kind, topic, assertive_block, compare}."""
    text = request.strip()
    month = None  # {"year": int|None, "month": int|None, "relative": "this"|"last"|None}
    base_month = None  # "A월 대비 B월" 의 A (비교 기준). month 는 B (대상)
    pair = re.search(r"(?:(\d{4})\s*년\s*)?(\d{1,2})\s*월\s*(?:을|를|과|와)?\s*대비\s*(?:(\d{4})\s*년\s*)?(\d{1,2})\s*월", text)
    m = re.search(r"(?:(\d{4})\s*년\s*)?(\d{1,2})\s*월", text)
    m6 = re.search(r"\b(20\d{2})(0[1-9]|1[0-2])\b", text)
    if pair:
        base_month = {"year": int(pair.group(1)) if pair.group(1) else None, "month": int(pair.group(2)), "relative": None}
        month = {"year": int(pair.group(3)) if pair.group(3) else None, "month": int(pair.group(4)), "relative": None}
    elif m:
        month = {"year": int(m.group(1)) if m.group(1) else None, "month": int(m.group(2)), "relative": None}
    elif m6:
        month = {"year": int(m6.group(1)), "month": int(m6.group(2)), "relative": None}
    elif re.search(r"이번\s*달|금월|이달", text):
        month = {"year": None, "month": None, "relative": "this"}
    elif re.search(r"지난\s*달|저번\s*달|전월", text) and not re.search(r"대비|비교", text):
        month = {"year": None, "month": None, "relative": "last"}

    topic = None
    for cat, words in TOPIC_SYNONYMS.items():
        if any(w in text for w in words):
            topic = cat
            break

    assertive = any(w in text for w in TONE_WORDS)
    compare = bool(re.search(r"전월|지난\s*달|대비|비교", text)) or base_month is not None
    if assertive:
        kind = "단정 표현 거부"
    elif re.search(r"학습|분류\s*(갱신|업데이트|추가)|가맹점\s*등록", text):
        kind = "카테고리 학습"
    elif re.search(r"디버그|debug|로그", text, re.I):
        kind = "디버그"
    elif compare:
        kind = "전월 비교"
    else:
        kind = "리포트"
    return {"month": month, "base_month": base_month, "kind": kind, "topic": topic, "assertive_block": assertive, "compare": compare}


def resolve_month(spec, available: list[str], today: dt.date) -> tuple[str, str | None]:
    """대상 월 결정. (YYYY-MM, 안내문). 미지정 시 자료에서 가장 최근 월."""
    latest = available[-1]
    if spec is None:
        return latest, None
    if spec["relative"]:
        y, mth = today.year, today.month
        if spec["relative"] == "last":
            y, mth = (y - 1, 12) if mth == 1 else (y, mth - 1)
        key = f"{y:04d}-{mth:02d}"
        if key in available:
            return key, None
        return latest, f"요청한 {key} 자료가 없어 자료에서 가장 최근 월({latest})로 분석합니다."
    cands = [k for k in available if int(k[5:]) == spec["month"] and (spec["year"] is None or int(k[:4]) == spec["year"])]
    if not cands:
        raise ValueError(f"{spec['month']}월 자료가 없습니다. 사용 가능한 월: {', '.join(available)}")
    return cands[-1], None


def run(request: str) -> int:
    t0 = time.monotonic()
    mn.reset_run_state()
    mn.purge_old_cache()
    intent = parse_intent(request)
    print(f"[의도] 종류={intent['kind']} 월={intent['month']} 토픽={intent['topic']} 단정차단={intent['assertive_block']}")
    if intent["assertive_block"]:
        print(advisor.REFUSAL_MESSAGE)

    try:
        df, meta = csv_loader.load_all()
    except csv_loader.HeaderNotRecognized as e:
        print(str(e))
        return 3
    df["merchant"] = df["merchant_raw"].map(mn.normalize_merchant)
    mn.register_sensitive(df["merchant_raw"].unique())
    anon = mn.Anonymizer(df["merchant"].unique())
    df, cinfo = cc.classify_dataframe(df, anon)

    if intent["kind"] == "카테고리 학습":
        print(f"[학습] 사전 매칭 {cinfo['dict_hits']}개 · LLM 대상 {cinfo['llm_targets']}개 · 자동 추가 {len(cinfo['auto_added'])}개 · "
              f"후보 {len(cinfo['candidates'])}개 · 미분류 {len(cinfo['unclassified'])}개")
        return 0

    available = sorted(df["month"].unique())
    try:
        month, notice = resolve_month(intent["month"], available, dt.date.today())
    except ValueError as e:
        print(str(e))
        return 2
    if notice:
        print(notice)

    base = None
    if intent["base_month"]:  # "A월 대비 B월": 기준 월 A 는 자료에 반드시 있어야 함 (임의 대체 금지)
        try:
            base, _ = resolve_month(intent["base_month"], available, dt.date.today())
        except ValueError as e:
            print(str(e))
            return 2
        if base == month:
            print("비교 기준 월과 대상 월이 같습니다. 서로 다른 두 달을 지정해 주세요.")
            return 2

    summary = se.update_monthly_summary(df)
    stats = se.compute_stats(df, month, summary, base=base)

    if intent["kind"] == "디버그":
        print(f"[디버그] 파일={meta['files']} 월={available} 분류={cinfo['dict_hits']}/{cinfo['llm_targets']} "
              f"미분류={cinfo['unclassified']} 정규화 폴백={mn.unmatched_merchants()} 합계일치={stats['sum_matches']} "
              f"LLM={mn.llm_usage()}")
        return 0

    advice, ok, _ = mn.with_retry(lambda: advisor.generate_advice(stats, intent["topic"]),
                                  lambda a: all("%" in i["evidence"] for i in a["items"]))
    if not ok:
        print("절약 제안 검증 실패")
        return 4

    path = None
    for _attempt in range(3):  # 검증 실패 시 리포트 단계만 재시도
        path = report_writer.write_report(stats, advice, intent, meta, path=path or report_writer.next_output_path())
        problems = report_writer.verify_report(path, stats)
        if not problems:
            break
        print("검증 실패:", problems)
    else:
        return 5

    # 익명화 캐시 (토큰·카테고리·1만 원 반올림 금액만)
    cur = df[df["month"] == month]
    mn.save_anonymized_cache(month, [
        {"token": anon.token(r.merchant), "category": r.category, "amount": mn.round_man(r.amount)}
        for r in cur.itertuples()])

    elapsed = time.monotonic() - t0
    print(f"\n리포트: {path}")
    if base:
        print(f"[비교] {base} 대비 {month}")
    print(f"1) 총 사용액: {stats['total']:,}원 ({stats['month']}, {stats['count']}건)")
    top = ", ".join(f"{t['category']}({t['growth']:+.1f}%)" if t["growth"] is not None else t["category"] for t in stats["top3"])
    print(f"2) 과소비 Top 3: {top or '해당 없음'}")
    first = advice["items"][0] if advice["items"] else None
    print("3) 절약 제안 요청: " + (f"{first['category']} - {first['text']} ({first['evidence']}) 검토 권장" if first else "해당 없음"))
    print(f"[실행] {elapsed:.1f}초 · LLM 토큰 {mn.llm_usage()['tokens']:,}/{mn.TOKEN_BUDGET:,} · 호출 {mn.llm_usage()['calls']}회 · "
          f"단정표현 치환 {advice['assertive_blocked']}건 · {'; '.join(advice['notes'])}")
    print("\n" + report_writer.DISCLAIMER)
    return 0


def main(request: str) -> int:
    return run(request)


if __name__ == "__main__":
    # 자연어 한 문장만 허용: --month 같은 옵션 형태는 거부
    if len(sys.argv) != 2 or sys.argv[1].startswith("-"):
        print('사용법: python main.py "자연어 요청 한 문장"   (예: python main.py "4월 지출 정리해줘")')
        sys.exit(2)
    sys.exit(main(request=sys.argv[1]))
