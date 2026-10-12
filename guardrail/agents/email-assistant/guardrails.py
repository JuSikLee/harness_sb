"""specs/guardrails.md 의 가드레일 구현 (##1 허용 · ##2 거부 · ##3 승인 · ##4 검출 · ##5 마스킹)."""
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]          # guardrail/
ALLOWED_READ_DIR = BASE_DIR / "tests" / "emails"
ALLOWED_WRITE_DIR = BASE_DIR / "agents" / "email-assistant" / "drafts"
KEYWORD_FILE = Path(__file__).resolve().parent / "sensitive_keywords.txt"

COMPANY_DOMAIN = "mycompany.co.kr"
EMPLOYEE_PHONES: set[str] = set()   # 임직원 휴대폰 (숫자만, 예: "01012345678"). 여기 없는 번호는 검출 대상
MAX_EXTERNAL_RECIPIENTS = 5         # ##2: 5명 이상의 외부 수신자 발송 거부
APPROVAL_TIMEOUT_SEC = 30           # ##3


# ---------------------------------------------------------------- ## 1 항상 허용
def _is_under(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def is_read_allowed(path) -> bool:
    """tests/emails/ 폴더의 파일 읽기만 자동 허용."""
    return _is_under(Path(path), ALLOWED_READ_DIR)


def is_write_allowed(path) -> bool:
    """agents/email-assistant/drafts/ 폴더 안에 저장하는 것만 자동 허용."""
    return _is_under(Path(path), ALLOWED_WRITE_DIR)


# ---------------------------------------------------------------- ## 4 비공개 정보 검출
REGEX_PATTERNS = {   # type -> regex  (## 5 마스킹에도 같은 패턴을 사용)
    "RRN": re.compile(r"\d{6}-\d{7}"),
    "CARD": re.compile(r"\d{4}-\d{4}-\d{4}-\d{4}"),
    "PHONE": re.compile(r"01[016789][-.\s]?\d{3,4}[-.\s]?\d{4}"),
    "IP": re.compile(
        r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
    ),
}

KEYWORDS = [
    "연봉", "급여", "상여", "보너스", "인센티브",
    "인사 평가", "고과", "성과급", "직급 조정",
    "M&A", "인수", "합병", "매각",
]


@dataclass
class Finding:
    kind: str       # 패턴 유형 또는 "KEYWORD"
    text: str
    start: int
    end: int

    def line_no(self, source: str) -> int:
        return source.count("\n", 0, self.start) + 1


def _is_employee_phone(value: str) -> bool:
    return re.sub(r"\D", "", value) in EMPLOYEE_PHONES


def _load_keywords() -> list[str]:
    words = list(KEYWORDS)
    if KEYWORD_FILE.exists():
        for line in KEYWORD_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                words.append(line)
    return words


def find_regex_matches(text: str) -> list[Finding]:
    found = []
    for kind, pat in REGEX_PATTERNS.items():
        for m in pat.finditer(text):
            if kind == "PHONE" and _is_employee_phone(m.group()):
                continue
            found.append(Finding(kind, m.group(), m.start(), m.end()))
    return sorted(found, key=lambda f: f.start)


def detect_sensitive(text: str) -> list[Finding]:
    """정규식 + 키워드 기반 검출. 위치(start/end)와 함께 반환."""
    found = find_regex_matches(text)
    for word in _load_keywords():
        for m in re.finditer(re.escape(word), text, flags=re.IGNORECASE):
            found.append(Finding("KEYWORD", m.group(), m.start(), m.end()))
    return sorted(found, key=lambda f: f.start)


def format_findings(findings: list[Finding], text: str) -> str:
    return "\n".join(
        f"  - [{f.kind}] \"{f.text}\" (줄 {f.line_no(text)}, 위치 {f.start}-{f.end})" for f in findings
    )


# ---------------------------------------------------------------- ## 5 입력 마스킹
PLACEHOLDER_RE = re.compile(r"\[REDACTED_[A-Z]+_\d+\]")


class Redactor:
    """정규식 매칭값을 [REDACTED_TYPE_N] 로 치환. 매핑 표는 메모리에만 두고 clear() 시 삭제."""

    def __init__(self):
        self._mapping: dict[str, str] = {}   # placeholder -> 원본
        self._reverse: dict[str, str] = {}   # 원본 -> placeholder
        self._counters: dict[str, int] = {}

    def mask(self, text: str) -> str:
        matches = find_regex_matches(text)
        out, pos = [], 0
        for f in matches:
            if f.start < pos:       # 겹치는 매칭은 건너뜀
                continue
            if f.text not in self._reverse:
                n = self._counters.get(f.kind, 0) + 1
                self._counters[f.kind] = n
                ph = f"[REDACTED_{f.kind}_{n}]"
                self._reverse[f.text] = ph
                self._mapping[ph] = f.text
            out.append(text[pos:f.start])
            out.append(self._reverse[f.text])
            pos = f.end
        out.append(text[pos:])
        return "".join(out)

    def clear(self) -> None:
        self._mapping.clear()
        self._reverse.clear()
        self._counters.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.clear()


def has_leftover_placeholders(text: str) -> bool:
    return bool(PLACEHOLDER_RE.search(text))


# ---------------------------------------------------------------- ## 2 항상 거부
def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower()


def is_external(addr: str) -> bool:
    return _domain(addr) != COMPANY_DOMAIN


def check_always_deny(original_sender: str, recipients: list[str], draft_text: str) -> list[str]:
    """거부 사유 목록 반환. 비어 있으면 통과."""
    reasons = []
    if any(r.lower() != original_sender.lower() for r in recipients):
        reasons.append("받은 메일의 발신자가 아닌 다른 주소로의 발송")
    if sum(is_external(r) for r in recipients) >= MAX_EXTERNAL_RECIPIENTS:
        reasons.append(f"동시에 {MAX_EXTERNAL_RECIPIENTS}명 이상의 외부 수신자에게 발송")
    if detect_sensitive(draft_text):
        reasons.append("본문에 비공개 정보가 검출됨 (##4)")
    return reasons


# ---------------------------------------------------------------- ## 3 사용자 승인
COMMITMENT_RE = re.compile(
    r"[^.!?\n]*(?:\d{1,2}\s*월|\d{1,2}\s*일|\d{1,2}\s*시|오전|오후|다음\s*주|내일|모레|"
    r"만나\s*뵙|뵙겠|회신\s*드리겠|연락\s*드리겠|보내\s*드리겠)[^.!?\n]*"
)


def extract_commitments(draft_text: str) -> list[str]:
    return [m.group().strip() for m in COMMITMENT_RE.finditer(draft_text) if m.group().strip()]


def build_approval_prompt(recipient: str, subject: str, draft_text: str, attachments: list[str]) -> str:
    commitments = extract_commitments(draft_text)
    commit_line = " / ".join(f'"{c}"' for c in commitments) if commitments else "(없음)"
    lines = [
        "[사용자 승인 필요]",
        "행동: 메일 발송",
        f"받는 사람: {recipient}" + ("  (외부 도메인)" if is_external(recipient) else ""),
        f"제목: {subject}",
        f"약속/일정: {commit_line}",
        f"첨부: {', '.join(attachments) if attachments else '(없음)'}",
        "",
        f"계속하시겠습니까? (y/n, {APPROVAL_TIMEOUT_SEC}초 내 미입력 시 n): ",
    ]
    return "\n".join(lines)


def ask_approval(prompt: str, timeout: int = APPROVAL_TIMEOUT_SEC) -> bool:
    """y 면 True. n · 그 외 입력 · 시간 초과 · 입력 불가는 모두 False (안전 쪽)."""
    answer: list[str] = []

    def _read():
        try:
            answer.append(sys.stdin.readline())
        except Exception:
            pass

    print(prompt, end="", flush=True)
    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        print(f"\n입력 시간 {timeout}초 초과: 자동으로 n 처리합니다.")
        return False
    return bool(answer) and answer[0].strip().lower() == "y"
