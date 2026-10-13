"""장기 메모리: 세션을 가로지르는 정보 (memory/research/long_term/).

- user_interests.md      사용자 관심 분야 (저장된 리서치 5건마다 자동 갱신)
- preferred_authors.md   자주 인용하는 저자 (사용자가 알리면 즉시 추가)
- past_research/{topic}.md  완료한 리서치의 최종 요약 (사용자 승인 시 저장)
"""
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path

from working_memory import MEMORY_DIR, _write_atomic, topic_slug

INTERESTS_SUMMARY_CHARS = 200
UPDATE_EVERY = 5
TOPIC_LINE = "- 자주 다룬 주제:"


class LongTermMemory:
    def __init__(self, memory_dir=MEMORY_DIR):
        self.dir = Path(memory_dir) / "research" / "long_term"
        self.interests_path = self.dir / "user_interests.md"
        self.authors_path = self.dir / "preferred_authors.md"
        self.past_dir = self.dir / "past_research"
        self.state_path = self.dir / "_state.json"  # 저장 횟수 · 키워드 누적

    # ---- past_research ----
    def past_research_path(self, topic):
        """저장할 때와 읽을 때 모두 이 경로 함수를 쓴다 (파일명 규칙 단일화)."""
        return self.past_dir / f"{topic_slug(topic)}.md"

    def load_past_research(self, topic):
        path = self.past_research_path(topic)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def save_past_research(self, topic, summary, keywords, source_titles):
        """최종 요약을 저장하고 저장 횟수를 올린다. 5의 배수면 user_interests.md도 갱신.

        반환: (저장 경로, user_interests.md를 갱신했는지)
        """
        path = self.past_research_path(topic)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join([
            f"# {topic}", "",
            f"- 완료일: {date.today().isoformat()}",
            f"- 사용 자료: {len(source_titles)}건",
            f"- 키워드: {', '.join(keywords)}", "",
            "## 사용한 자료", *[f"- {t}" for t in source_titles], "",
            "## 최종 요약", summary.strip(), ""])
        _write_atomic(path, body)

        state = self._load_state()
        state["saved_count"] += 1
        counts = Counter(state["keyword_counts"])
        counts.update(keywords)
        state["keyword_counts"] = dict(counts)
        self._save_state(state)
        updated = state["saved_count"] % UPDATE_EVERY == 0
        if updated:
            self.update_user_interests()
        return path, updated

    def _load_state(self):
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"saved_count": 0, "keyword_counts": {}}

    def _save_state(self, state):
        self.dir.mkdir(parents=True, exist_ok=True)
        _write_atomic(self.state_path, json.dumps(state, ensure_ascii=False, indent=2))

    # ---- user_interests.md ----
    def update_user_interests(self, top=10):
        """자주 다룬 키워드를 '자주 다룬 주제' 줄에 반영 (나머지 줄은 보존)."""
        counts = Counter(self._load_state()["keyword_counts"])
        line = f"{TOPIC_LINE} {', '.join(k for k, _ in counts.most_common(top))}"
        self.dir.mkdir(parents=True, exist_ok=True)
        lines = (self.interests_path.read_text(encoding="utf-8").splitlines()
                 if self.interests_path.exists() else ["# 사용자 관심 분야", ""])
        if any(ln.startswith(TOPIC_LINE) for ln in lines):
            lines = [line if ln.startswith(TOPIC_LINE) else ln for ln in lines]
        else:
            lines.append(line)
        _write_atomic(self.interests_path, "\n".join(lines) + "\n")

    def interests_summary(self, limit=INTERESTS_SUMMARY_CHARS):
        """단기 컨텍스트에 항상 들어가는 관심 분야 요약 (200자 이내)."""
        if not self.interests_path.exists():
            return "(아직 기록된 관심 분야 없음)"
        items = [ln[2:].strip() for ln in
                 self.interests_path.read_text(encoding="utf-8").splitlines()
                 if ln.startswith("- ")]
        text = re.sub(r"\s+", " ", " / ".join(items)).strip() or "(아직 기록된 관심 분야 없음)"
        return text if len(text) <= limit else text[:limit - 1] + "…"

    # ---- preferred_authors.md ----
    def load_preferred_authors(self):
        if not self.authors_path.exists():
            return []
        return [ln[2:].strip() for ln in
                self.authors_path.read_text(encoding="utf-8").splitlines()
                if ln.startswith("- ")]

    def add_preferred_author(self, entry):
        """저자 한 줄 추가 (예: 'Name (소속, 설명)'). 이미 있는 이름이면 False."""
        name = entry.split("(")[0].strip().lower()
        authors = self.load_preferred_authors()
        if any(a.split("(")[0].strip().lower() == name for a in authors):
            return False
        authors.append(entry.strip())
        self.dir.mkdir(parents=True, exist_ok=True)
        _write_atomic(self.authors_path,
                      "# 선호 저자\n\n" + "".join(f"- {a}\n" for a in authors))
        return True
