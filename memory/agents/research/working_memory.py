"""작업 메모리: 한 리서치의 진행 상황을 progress.json · sources.jsonl · notes.md에 기록.

저장 위치: memory/research/working/{topic_slug}/
"""
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(__file__).resolve().parents[2]  # memory/
PHASES = ["수집", "정렬", "분석", "요약", "정리"]
MAX_SLUG_CHARS = 60


def topic_slug(topic):
    """주제 -> 파일/폴더 이름. 작업 메모리와 장기 메모리가 같은 함수를 쓴다."""
    slug = re.sub(r"[^\w]+", "_", topic.strip().lower()).strip("_")
    if not slug:
        raise ValueError(f"파일 이름으로 쓸 수 없는 주제: {topic!r}")
    if len(slug) > MAX_SLUG_CHARS:
        digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:6]
        slug = f"{slug[:MAX_SLUG_CHARS]}_{digest}"
    return slug


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _write_atomic(path, text):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class WorkingMemory:
    def __init__(self, topic, memory_dir=MEMORY_DIR):
        self.topic = topic
        self.dir = Path(memory_dir) / "research" / "working" / topic_slug(topic)
        self.progress_path = self.dir / "progress.json"
        self.sources_path = self.dir / "sources.jsonl"
        self.notes_path = self.dir / "notes.md"
        self.progress = None

    # ---- 시작 · 재개 ----
    def start(self):
        """새 작업을 만들거나 미완료 작업을 이어받는다. 'new' | 'resumed' 반환."""
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.progress_path.exists():
            old = json.loads(self.progress_path.read_text(encoding="utf-8"))
            if old.get("status") != "completed":
                old["status"] = "in_progress"
                old.pop("stop_reason", None)
                self.progress = old
                self._save()
                return "resumed"
        now = _now()
        self.progress = {
            "topic": self.topic, "started_at": now, "last_updated": now,
            "status": "in_progress", "phase": PHASES[0],
            "phases_completed": [], "phases_remaining": list(PHASES),
            "sources_collected": 0, "sources_processed": 0,
            "key_findings_so_far": [], "tokens_used": 0,
        }
        _write_atomic(self.sources_path, "")  # 새 실행: 이전 실행의 자료 목록은 비운다
        header = f"# 리서치 노트: {self.topic}\n"
        if self.notes_path.exists():  # notes.md는 사용자가 검토하는 누적 메모라 지우지 않는다
            self.append_note(f"--- 새 실행 시작 ({now}) ---")
        else:
            _write_atomic(self.notes_path, header)
        self._save()
        return "new"

    def _save(self):
        self.progress["last_updated"] = _now()
        _write_atomic(self.progress_path,
                      json.dumps(self.progress, ensure_ascii=False, indent=4))

    # ---- 단계 · 상태 ----
    def enter_phase(self, phase):
        i = PHASES.index(phase)
        self.progress.update(phase=phase, phases_completed=PHASES[:i],
                             phases_remaining=PHASES[i:])
        self._save()

    def finish(self):
        self.progress.update(status="completed", phases_completed=list(PHASES),
                             phases_remaining=[])
        self._save()

    def stop(self, status, reason):
        self.progress.update(status=status, stop_reason=reason)
        self._save()

    def update_tokens(self, total):
        self.progress["tokens_used"] = total
        self._save()

    def add_findings(self, findings):
        known = self.progress["key_findings_so_far"]
        known.extend(f for f in findings if f and f not in known)
        self._save()

    def recent_findings(self, n=5):
        return self.progress["key_findings_so_far"][-n:]

    def progress_core(self):
        """Context Reflect 때 새 세션의 시스템 프롬프트에 붙이는 핵심 부분."""
        p = self.progress
        return {k: p[k] for k in ("topic", "phase", "phases_completed", "phases_remaining",
                                  "sources_collected", "sources_processed")} | {
            "key_findings_so_far": p["key_findings_so_far"][-10:]}

    # ---- sources.jsonl ----
    def read_sources(self):
        if not self.sources_path.exists():
            return []
        lines = self.sources_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def _write_sources(self, rows):
        text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        _write_atomic(self.sources_path, text)
        self.progress["sources_collected"] = len(rows)
        self.progress["sources_processed"] = sum(1 for r in rows if r["processed"])
        self._save()

    def add_source(self, url, title, summary=""):
        """자료 한 건을 한 줄로 추가. 이미 있는 url이면 건너뛰고 False."""
        rows = self.read_sources()
        if any(r["url"] == url for r in rows):
            return False
        rows.append({"id": f"s{len(rows) + 1:03d}", "url": url, "title": title,
                     "summary": summary, "processed": False})
        self._write_sources(rows)
        return True

    def sort_sources(self):
        """url 순으로 정렬하고 같은 제목의 중복을 제거. 제거한 건수 반환."""
        rows = sorted(self.read_sources(), key=lambda r: (not r["processed"], r["url"]))
        seen, kept = set(), []
        for r in rows:
            if r["title"] in seen and not r["processed"]:
                continue
            seen.add(r["title"])
            kept.append(r)
        kept.sort(key=lambda r: r["url"])
        self._write_sources(kept)
        return len(rows) - len(kept)

    def pending_sources(self):
        return [r for r in self.read_sources() if not r["processed"]]

    def _update_source(self, url, **fields):
        rows = self.read_sources()
        for r in rows:
            if r["url"] == url:
                r.update(fields)
        self._write_sources(rows)

    def mark_processed(self, url, summary, keywords, critique=""):
        self._update_source(url, processed=True, summary=summary, keywords=keywords,
                            critique=critique, error=None)

    def mark_error(self, url, error):
        self._update_source(url, error=str(error))

    def keywords(self, limit=50):
        """처리한 자료들의 핵심 키워드 (빈도순, 최대 limit개)."""
        counts = Counter(k for r in self.read_sources() if r["processed"]
                         for k in r.get("keywords", []))
        return [k for k, _ in counts.most_common(limit)]

    # ---- notes.md ----
    def append_note(self, text):
        with self.notes_path.open("a", encoding="utf-8") as f:
            f.write(f"\n{text.strip()}\n")
