"""스펙의 형식·일관성·의미 검증. 각 함수는 문제 항목 문자열 리스트를 반환 (빈 리스트 = 통과)."""
from analyzer import call_model, parse_json

REQUIRED_FIELDS = ["department", "period", "total_revenue", "by_region", "comparison_basis"]
VALID_BASES = ("전월 대비", "전년 동월 대비")
TOLERANCE = 0.01


def validate_format(result):
    problems = []
    for f in REQUIRED_FIELDS:
        if f not in result:
            problems.append(f"필수 필드 누락: {f}")
    if problems:
        return problems
    if result["comparison_basis"] not in VALID_BASES:
        problems.append(f'comparison_basis가 "{result["comparison_basis"]}"임 (허용: 전월 대비 / 전년 동월 대비)')
    regions = result["by_region"]
    if not isinstance(regions, list) or not regions:
        return problems + ["by_region이 비어 있거나 배열이 아님"]
    for i, item in enumerate(regions):
        if not isinstance(item, dict) or "region" not in item or "revenue" not in item:
            problems.append(f"by_region[{i}]에 region, revenue 필드가 없음")
    if problems:
        return problems
    try:
        total = float(result["total_revenue"])
        s = sum(float(r["revenue"]) for r in regions)
    except (TypeError, ValueError):
        return problems + ["total_revenue 또는 revenue가 숫자가 아님"]
    diff = abs(total - s)
    if diff > TOLERANCE * max(abs(total), abs(s), 1):
        pct = diff / max(abs(total), abs(s), 1) * 100
        problems += [f"by_region의 revenue 합: {s:,.0f}원", f"결과의 total_revenue: {total:,.0f}원",
                     f"차이: {diff:,.0f}원 ({pct:.0f}% 불일치)"]
    return problems


def validate_consistency(results, input_count):
    """results: {부서: 결과}. 부서 간 기준 통일 + 누락 파일 확인."""
    problems = []
    bases = {r.get("comparison_basis") for r in results.values()}
    if len(bases) > 1:
        problems.append("부서 간 comparison_basis 불일치: " +
                        ", ".join(f"{d}={r.get('comparison_basis')}" for d, r in results.items()))
    if len(results) != input_count:
        problems.append(f"처리한 파일 수({len(results)})와 입력 파일 수({input_count})가 다름")
    return problems


def validate_semantics(client, budget, data, result):
    """LLM 평가자: 핵심 원인 기재 + 가상 수치 여부."""
    prompt = (f"원본 데이터(CSV):\n{data['csv_text']}\n\n분석 결과:\n{result}\n\n"
              "다음을 평가해 JSON만 출력: "
              '부서명은 지시로 주어진 값이며 period "미상"은 허용되므로 가상 수치가 아닙니다. 원본 값에서 계산한 합계·증감도 정상입니다. '
              '{"has_cause": bool (summary에 매출 변화의 핵심 원인이 1개 이상 있는가), '
              '"fabricated": [원본 데이터로 설명되지 않는 가상의 수치/사실 문자열 목록]}')
    out = parse_json(call_model(client, budget, "당신은 엄격한 데이터 검수자입니다.", prompt, 4000))
    problems = []
    if not out.get("has_cause"):
        problems.append("summary에 매출 변화의 핵심 원인이 없음")
    for x in out.get("fabricated", []):
        problems.append(f"원본에 없는 수치/사실: {x}")
    return problems
