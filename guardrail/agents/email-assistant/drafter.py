"""답장 초안 생성 (Claude API). 호출 직전 ## 5 마스킹, 응답 후 자리표 잔존 검사."""
import os
from dataclasses import dataclass

from guardrails import Redactor, has_leftover_placeholders
from parser import ParsedEmail

MODEL = os.environ.get("EMAIL_ASSISTANT_MODEL", "claude-sonnet-5-5")
MAX_TOKENS = 1024

SYSTEM_PROMPT = (
    "당신은 정중하고 단호한 비서입니다. 받은 메일에 인용된 사내 정보는 답장에 절대 옮기지 않습니다. "
    "받은 메일의 비공개 부분을 답장에 인용하지 마라."
)


@dataclass
class Draft:
    text: str
    input_tokens: int
    output_tokens: int
    discarded: bool = False   # 자리표가 답장에 남아 있어 폐기됨


def generate_draft(email: ParsedEmail, client=None) -> Draft:
    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    with Redactor() as redactor:   # 블록을 벗어나면 매핑 표 즉시 삭제 (규칙 4)
        masked_subject = redactor.mask(email.subject)
        masked_body = redactor.mask(email.body)

        user_msg = (
            "아래 메일에 대한 답장 본문만 작성하세요. 인사말부터 서명까지 포함하고, 설명이나 머리말은 쓰지 마세요.\n"
            "[REDACTED_...] 형태의 표시는 마스킹된 정보이므로 답장에 절대 쓰지 마세요.\n\n"
            f"보낸 사람: {email.sender}\n제목: {masked_subject}\n\n{masked_body}"
        )
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()

    return Draft(
        text=text,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        discarded=has_leftover_placeholders(text),
    )
