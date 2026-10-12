"""Parse a .eml file into From / To / Subject / Body."""
from dataclasses import dataclass, field
from email import policy
from email.parser import Parser
from email.utils import getaddresses, parseaddr
from pathlib import Path


@dataclass
class ParsedEmail:
    sender: str
    to: list[str]
    subject: str
    body: str
    attachments: list[str] = field(default_factory=list)


def parse_eml(path: str | Path) -> ParsedEmail:
    # charset 헤더가 없는 단순 .eml 도 한글이 깨지지 않도록 UTF-8 텍스트로 읽는다
    msg = Parser(policy=policy.default).parsestr(Path(path).read_text(encoding="utf-8"))

    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part else ""
    # 테스트 메일은 본문 앞에 "Body:" 라벨이 붙어 있음
    body = body.strip()
    if body.startswith("Body:"):
        body = body[len("Body:"):].strip()

    return ParsedEmail(
        sender=parseaddr(str(msg.get("From", "")))[1],
        to=[addr for _, addr in getaddresses([str(msg.get("To", ""))]) if addr],
        subject=str(msg.get("Subject", "")),
        body=body,
        attachments=[p.get_filename() or "(이름 없음)" for p in msg.iter_attachments()],
    )
