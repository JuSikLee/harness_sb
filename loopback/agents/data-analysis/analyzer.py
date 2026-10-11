"""클로드 API로 한 파일을 분석하고 JSON 결과를 반환."""
import json
import re

import anthropic

MODEL = "claude-sonnet-5-5"
TOKEN_LIMIT = 200,000

SYSTEM_PROMPT = "당신은 5년 차 데이터 분석가입니다. 모든 분석은 전월 대비 기준으로 합니다."

OUTPUT_SPEC = """아래 JSON 객체만 출력하세요 (설명, 코드블록 금지).
{
  "department": "부서명 (지시받은 이름 그대로)",
  "period": "분석 기간 (데이터에서 알 수 없으면 \\"미상\\")",
  "total_revenue": 숫자 (이번 달 매출 합계, 원),
  "by_region": [{"region": "지역명", "revenue": 숫자}],
  "comparison_basis": "전월 대비",
  "summary": "전월 대비 매출 변화의 핵심 원인을 1개 이상 포함한 요약. 데이터에 없는 수치는 만들지 말 것"
}"""


class TokenLimitExceeded(Exception):
    pass


class TokenBudget:
    def __init__(self, limit=TOKEN_LIMIT):
        self.limit = limit
        self.used = 0

    def add(self, usage):
        self.used += usage.input_tokens + usage.output_tokens
        if self.used > self.limit:
            raise TokenLimitExceeded(f"누적 토큰 {self.used:,} > 한도 {self.limit:,}")


def get_client():
    return anthropic.Anthropic()


def call_model(client, budget, system, user, max_tokens=8000):
    resp = client.messages.create(model=MODEL, max_tokens=max_tokens, system=system,
                                  messages=[{"role": "user", "content": user}])
    budget.add(resp.usage)
    return "".join(b.text for b in resp.content if b.type == "text")


def parse_json(text):
    """응답에서 JSON 객체를 추출. 실패 시 ValueError."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("응답에 JSON이 없음")
    return json.loads(m.group(0))


def analyze(client, budget, data, diagnostic=None):
    """한 파일 분석. data는 loader.load_file 결과."""
    user = (f"부서명: {data['department']}\n\n다음은 매출 데이터(CSV)입니다. 컬럼 의미: "
            "지역/region=지역, 매출액/revenue=이번 달 매출, 전월/last_month=전월 매출. "
            "영문 컬럼명도 같은 의미로 취급하세요. 결측값은 합계에서 제외하고 그 사실을 summary에 적으세요.\n\n"
            f"{data['csv_text']}\n\n{OUTPUT_SPEC}")
    if diagnostic:
        user += f"\n\n{diagnostic}"
    return parse_json(call_model(client, budget, SYSTEM_PROMPT, user))
