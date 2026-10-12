"""사용법: python agents/email-assistant/run.py tests/emails/normal.eml"""
import sys
from pathlib import Path

import guardrails as g
import sender
from drafter import generate_draft
from parser import parse_eml


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("사용법: python run.py <메일.eml>")
        return 1
    eml_path = Path(argv[1]).resolve()

    # ## 1: 허용된 폴더의 파일만 읽음
    if not g.is_read_allowed(eml_path):
        print(f"[거부] 허용되지 않은 경로입니다: {eml_path}")
        return 1
    email = parse_eml(eml_path)
    print(f"받은 메일: {email.sender} / {email.subject}")

    # 초안 생성 (마스킹 → LLM → 자리표 잔존 검사)
    draft = generate_draft(email)
    print(f"토큰 사용량: 입력 {draft.input_tokens} / 출력 {draft.output_tokens}")
    if draft.discarded:
        print("마스킹된 정보가 답장에 인용됨 - 폐기됨")
        return 2

    # ## 4: 초안 생성 단계 검출 → 경고
    draft_findings = g.detect_sensitive(draft.text)
    if draft_findings:
        print("이 초안에는 비공개 정보가 포함되어 있습니다 - 확인 후 발송하세요")

    subject = f"Re: {email.subject}" if not email.subject.lower().startswith("re:") else email.subject
    recipient = email.sender

    # ## 1: 초안을 콘솔 출력 + drafts/ 에 저장
    print("\n----- 답장 초안 -----")
    print(draft.text)
    print("---------------------\n")
    draft_path = g.ALLOWED_WRITE_DIR / f"{eml_path.stem}.txt"
    if g.is_write_allowed(draft_path):
        draft_path.write_text(f"To: {recipient}\nSubject: {subject}\n\n{draft.text}\n", encoding="utf-8")
        print(f"초안 저장: {draft_path.relative_to(g.BASE_DIR)}")

    # ## 2 · ## 4: 발송 직전 검사 (항상 거부)
    reasons = g.check_always_deny(email.sender, [recipient], draft.text)
    if reasons:
        print("[발송 차단] " + "; ".join(reasons))
        if draft_findings:
            print("검출 항목:\n" + g.format_findings(draft_findings, draft.text))
            print("초안에서 비공개 정보를 제거한 뒤 다시 시도하세요")
        return 3

    # ## 3: 사용자 승인 (실제 발송은 항상 승인 필요)
    prompt = g.build_approval_prompt(recipient, subject, draft.text, attachments=[])
    if not g.ask_approval(prompt):
        print("발송 취소됨. 초안은 그대로 유지됩니다.")
        return 0

    sender.send(recipient, subject, draft.text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
