"""진단 메시지 생성 + 재시도 흐름."""
import anthropic

import validator
from analyzer import analyze, TokenLimitExceeded
from loader import load_file, LoadError

MAX_ATTEMPTS = 3  # 최초 1회 + 재시도 2회

# 진단 메시지에 붙일 수정 지침 (문제 유형 -> 지침)
GUIDANCE = {
    "comparison_basis": ['모든 부서와 동일하게 "전월 대비"로 분석할 것',
                         "영문 컬럼명(last_month)을 한국어 분석 기준(전월)에 매핑해서 사용"],
    "합": ["결측값 행을 어떻게 처리했는지 summary에 명시",
          "결측값을 빼고 계산했다면 total_revenue도 그에 맞춰 갱신 (total_revenue = by_region revenue 합)"],
    "필드": ["누락된 필드를 모두 채운 JSON을 출력할 것"],
    "원인": ["summary에 전월 대비 변화의 핵심 원인을 데이터 근거와 함께 1개 이상 적을 것"],
    "원본에 없는": ["원본 데이터에 없는 수치·사실을 제거하고 데이터에 있는 값만 사용"],
}


def build_diagnostic(path, problems):
    guides = []
    for p in problems:
        for key, g in GUIDANCE.items():
            if key in p:
                guides += [x for x in g if x not in guides]
    guides = guides or ["위 문제 항목을 해결하도록 결과를 수정할 것"]
    return ("[이전 시도 검증 실패]\n" f"파일: {path}\n문제 항목:\n" +
            "\n".join(f"- {p}" for p in problems) +
            "\n\n다음 사항을 반드시 반영해 다시 분석해주세요.\n" + "\n".join(f"- {g}" for g in guides))


def analyze_with_retry(client, budget, path, log=print):
    """(result, data) 반환. 실패 시 (None, 사유). TokenLimitExceeded는 그대로 전파."""
    try:
        data = load_file(path)
    except LoadError as e:  # 재시도 안 함
        return None, f"{e} (재시도 안 함)"

    diagnostic = None
    reason = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        before = budget.used
        log(f"  시도 {attempt}/{MAX_ATTEMPTS}" + (" (진단 메시지 포함)" if diagnostic else ""))
        try:
            result = analyze(client, budget, data, diagnostic)
            problems = validator.validate_format(result)
            if not problems:
                problems = validator.validate_semantics(client, budget, data, result)
        except TokenLimitExceeded:
            raise
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.APIStatusError,
                ValueError) as e:  # 일시적 오류 / JSON 파싱 실패 -> 재시도
            problems = [f"호출 또는 응답 처리 실패: {type(e).__name__}: {e}"]
        used = f"토큰 +{budget.used - before:,}"
        if not problems:
            log(f"    -> 성공 ({used})")
            return result, data
        log(f"    -> 검증 실패 ({used})")
        for p in problems:
            log(f"       - {p}")
        diagnostic = build_diagnostic(path, problems)
    return None, f"{MAX_ATTEMPTS}회 재시도 후 실패"
