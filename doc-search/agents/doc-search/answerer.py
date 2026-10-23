"""검색 결과 → Claude 답변 생성 (핵심 답변 / 근거 인용 / 관련 문서 / 추가 질문)."""
import json
import os
import re
from pathlib import Path

import anthropic

SPEC = Path("/home/prime/harness_sb/doc-search/specs/system_prompt.md")

# guardrails.md 분류 트리거 (입력 질문 기준, 검색·임베딩 이전에 사용)
_C1 = ["매출액", "매출", "영업이익", "계약 금액", "계약금액", "임원 연봉", "급여 총액", "임원 의사결정"]
_C2 = ["코드네임", "코드명", "미공개 제품 계획", "미공개 제품"]
_C2_RE = re.compile(r"(프로젝트|project)\s*[-_]?\s*[A-Za-z가-힣0-9]{1,12}", re.IGNORECASE)

C1_REFUSAL = (
    "이 질문은 비공개 정보(C1)를 포함합니다.\n"
    "검색 에이전트는 답을 보여드릴 수 없습니다.\n"
    "담당자: HR(hr@novatech.example) 또는 분부장에게 직접 문의해주시면 됩니다."
)


def classify(question):
    """'C1' > 'C2' > 'OK'. C1·C2 동시 해당 시 C1 우선."""
    if any(t in question for t in _C1):
        return "C1"
    if any(t in question for t in _C2) or _C2_RE.search(question):
        return "C2"
    return "OK"


def mask_c2(text):
    return _C2_RE.sub("[프로젝트 X]", text)


FORMAT = """반드시 아래 JSON 객체 하나만 출력하세요(코드펜스 금지).
{"answer": "핵심 답변 3문장 이내. 문장마다 [문서명 · 섹션] 출처 표시",
 "citations": [{"doc": "문서명", "section": "섹션", "quote": "제공된 섹션 본문에 실제로 있는 문장을 그대로 인용"}],
 "related": [{"doc": "문서명", "reason": "추천 이유"}],
 "followups": ["추가 질문 1", "추가 질문 2"]}
규칙: citations는 핵심 1~3개 문서만(4개 이상 금지), related는 2~3개, followups는 1~2개.
제공된 섹션에 답이 없으면 answer를 "사내 문서에는 정보 없음"으로 하고 citations는 빈 배열로 하세요."""


def build_system():
    return SPEC.read_text(encoding="utf-8") + "\n\n" + FORMAT


def generate(question, hits, diagnostic=None):
    context = "\n\n".join(f"### {h['path']}\n{h['body']}" for h in hits)
    user = f"질문: {question}\n\n검색된 섹션:\n{context}"
    if diagnostic:
        user = f"{diagnostic}\n\n{user}"
    client = anthropic.Anthropic(timeout=40)
    msg = client.messages.create(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_tokens=1200,
        system=build_system(),
        messages=[{"role": "user", "content": user}],
    )
    text = msg.content[0].text
    start = text.index("{")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj, msg.usage.input_tokens + msg.usage.output_tokens
