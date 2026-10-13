"""컨텍스트 압축 · Context Reflect 판단 · 토큰 한도.

- 세션 토큰이 압축 기준(100,000)에 다가가면 단기 컨텍스트를 압축
- 세션 토큰이 200,000을 넘으면 새 세션으로 재시작 (Context Reflect)
- 작업 누적 1,000,000 / 자료 한 건 30,000 토큰 한도
"""
import json
import re
from dataclasses import dataclass, field

COMPRESS_AT = 100_000
REFLECT_AT = 200_000
TASK_LIMIT = 1_000_000
SOURCE_LIMIT = 30_000
COMPRESS_MARGIN = 0.9  # "넘기 전에": 기준의 90%에 닿으면 압축
SUMMARY_CHARS = 200


class TaskTokenLimitExceeded(Exception):
    pass


class SourceTokenLimitExceeded(Exception):
    pass


def estimate_tokens(text):
    """한국어 기준으로 넉넉하게 잡은 토큰 추정 (글자 1.5개 ≈ 1토큰)."""
    return int(len(text) / 1.5) + 1


def summarize200(text):
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS - 1] + "…"


def fit_body(body, other_tokens, max_output, source_limit=SOURCE_LIMIT):
    """자료 한 건의 총 토큰이 source_limit을 넘지 않도록 본문을 자른다."""
    room = source_limit - max_output - other_tokens
    max_chars = max(int(room * 1.5), 0)
    if len(body) <= max_chars:
        return body
    return body[:max_chars] + "\n[...토큰 한도로 이하 생략]"


@dataclass
class ShortTermContext:
    """단기 메모리 중 호출마다 달라지는 부분. (시스템 프롬프트 · 관심 분야 · 키워드는
    저장소에서 매번 새로 읽어 붙이므로 여기에 두지 않는다.)"""
    current_body: str = ""
    last_result: str = ""
    notes: list = field(default_factory=list)  # 아직 notes.md로 옮기지 않은 분석 노트


class ContextManager:
    def __init__(self, base_prompt, total_used=0, compress_at=COMPRESS_AT,
                 reflect_at=REFLECT_AT, task_limit=TASK_LIMIT, source_limit=SOURCE_LIMIT):
        self.base_prompt = base_prompt
        self.system_prompt = base_prompt
        self.compress_at, self.reflect_at = compress_at, reflect_at
        self.task_limit, self.source_limit = task_limit, source_limit
        self.total_tokens = total_used
        self.session_tokens = 0
        self.source_tokens = 0
        self.session_no = 1
        self.compressed = False
        self.ctx = ShortTermContext()

    # ---- 토큰 집계 ----
    def begin_source(self):
        self.source_tokens = 0

    def record(self, usage):
        used = usage.input_tokens + usage.output_tokens
        self.session_tokens += used
        self.total_tokens += used
        self.source_tokens += used
        if self.source_tokens > self.source_limit:
            raise SourceTokenLimitExceeded(
                f"자료 1건 토큰 {self.source_tokens:,} > 한도 {self.source_limit:,}")

    def check_task_limit(self, next_estimate=0):
        if self.total_tokens + next_estimate > self.task_limit:
            raise TaskTokenLimitExceeded(
                f"작업 누적 토큰 {self.total_tokens:,} (+다음 호출 {next_estimate:,}) "
                f"> 한도 {self.task_limit:,}")

    # ---- 판단 ----
    def should_reflect(self):
        return self.session_tokens >= self.reflect_at

    def should_compress(self):
        return (not self.compressed
                and self.session_tokens >= self.compress_at * COMPRESS_MARGIN)

    # ---- 실행 ----
    def flush_notes(self, wm, keep_last=0):
        """오래된 분석 노트를 notes.md로 옮기고 컨텍스트에서 제거. 옮긴 건수 반환."""
        notes = self.ctx.notes
        cut = len(notes) - keep_last
        if cut <= 0:
            return 0
        wm.append_note("\n\n".join(notes[:cut]))
        self.ctx.notes = notes[cut:]
        return cut

    def compress(self, wm):
        """이미 처리한 본문 제거 · 직전 결과 200자 요약 · 오래된 노트 이관."""
        self.ctx.current_body = ""
        self.ctx.last_result = summarize200(self.ctx.last_result)
        moved = self.flush_notes(wm, keep_last=1)
        self.compressed = True
        return f"압축 적용 (세션 토큰 {self.session_tokens:,}, 노트 {moved}건 notes.md로 이관)"

    def reflect(self, wm):
        """새 세션으로 재시작: 진행 핵심을 시스템 프롬프트에 붙이고 단기 메모리는 비운다."""
        self.flush_notes(wm)
        core = json.dumps(wm.progress_core(), ensure_ascii=False, indent=2)
        self.system_prompt = (f"{self.base_prompt}\n\n[이전 세션에서 이어받은 진행 상황]\n{core}")
        self.ctx = ShortTermContext()
        old = self.session_tokens
        self.session_tokens = 0
        self.compressed = False
        self.session_no += 1
        return f"Context Reflect: 세션 {self.session_no - 1} 종료({old:,} 토큰) -> 새 세션 {self.session_no} 시작"
