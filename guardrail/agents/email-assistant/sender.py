"""모의(mock) 발송: 실제 SMTP 호출 없음. 가드레일 통과 후에만 호출할 것."""


def send(to: str, subject: str, body: str) -> None:
    print(f"[발송됨] To: {to} Subject: {subject}")
