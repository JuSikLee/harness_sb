"""주제 한 줄을 받아 자료 수집부터 최종 정리까지 처리하는 리서치 에이전트.

사용법:
    python run.py "AI 에이전트의 운영 환경"
    python run.py "AI 에이전트의 운영 환경" --save-long-term yes
    python run.py --add-author "Mitchell Hashimoto (HashiCorp 공동 창업자)"

같은 주제로 다시 실행하면 (1) 미완료 작업은 progress.json에서 이어서 진행하고,
(2) 완료한 적이 있으면 long_term/past_research/{topic}.md를 먼저 읽어 단기 컨텍스트와
종합 단계에 주입한다.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import compressor
import researcher
from compressor import ContextManager, SourceTokenLimitExceeded, TaskTokenLimitExceeded
from long_term_memory import LongTermMemory
from working_memory import MEMORY_DIR, PHASES, WorkingMemory

TEXT_EXTS = {".txt", ".md"}


def collect(wm, mode, sources_dir):
    """자료 수집. 지금은 mock 모드만: sources_dir의 파일을 자료로 등록."""
    if mode != "mock":
        raise NotImplementedError(f"수집 모드 '{mode}'는 아직 지원하지 않습니다 (mock만 가능)")
    added = 0
    for path in sorted(sources_dir.iterdir()):
        if path.suffix.lower() not in TEXT_EXTS:
            continue
        title = path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        if wm.add_source(str(path), title):
            added += 1
    return added


def analyze(client, mgr, wm, lt, topic, past_text):
    """아직 처리하지 않은 자료만 한 건씩 처리. 처리 전에 압축 / Context Reflect를 판단."""
    pending = wm.pending_sources()
    failed = []
    for i, source in enumerate(pending, 1):
        if mgr.should_reflect():
            print(f"  ↻ {mgr.reflect(wm)}")
        elif mgr.should_compress():
            print(f"  ⇣ {mgr.compress(wm)}")
        print(f"[{i}/{len(pending)}] {source['title']}")
        try:
            body = Path(source["url"]).read_text(encoding="utf-8")
            researcher.process_source(client, mgr, wm, lt, topic, source, body, past_text)
        except (ValueError, SourceTokenLimitExceeded) as e:  # 파싱 실패 · 자료 한도 초과: 이 자료만 건너뜀
            wm.mark_error(source["url"], e)
            failed.append((source["title"], e))
            print(f"  ! 건너뜀: {e}")
        wm.update_tokens(mgr.total_tokens)
    return failed


def decide_save(mode):
    if mode in ("yes", "no"):
        return mode == "yes"
    if not sys.stdin.isatty():
        print("(비대화형 실행: --save-long-term yes 를 주지 않아 장기 메모리에 저장하지 않습니다)")
        return False
    return input("이 결과를 장기 메모리에 저장할까요? [y/N] ").strip().lower() in ("y", "yes", "예", "ㅇ")


def main():
    ap = argparse.ArgumentParser(description="리서치 에이전트")
    ap.add_argument("topic", nargs="?", help="리서치 주제 (한 줄)")
    ap.add_argument("--source-mode", default="mock", choices=["mock"])
    ap.add_argument("--sources-dir", type=Path, default=MEMORY_DIR / "tests" / "research")
    ap.add_argument("--memory-dir", type=Path, default=MEMORY_DIR)
    ap.add_argument("--save-long-term", choices=["ask", "yes", "no"], default="ask")
    ap.add_argument("--add-author", metavar="ENTRY", help="선호 저자를 장기 메모리에 즉시 추가")
    ap.add_argument("--compress-at", type=int, default=compressor.COMPRESS_AT)
    ap.add_argument("--reflect-at", type=int, default=compressor.REFLECT_AT)
    ap.add_argument("--task-limit", type=int, default=compressor.TASK_LIMIT)
    args = ap.parse_args()

    lt = LongTermMemory(args.memory_dir)
    if args.add_author:
        added = lt.add_preferred_author(args.add_author)
        print(f"선호 저자 {'추가' if added else '이미 있음'}: {args.add_author}")
        if not args.topic:
            return 0
    if not args.topic:
        ap.error("리서치 주제를 입력하세요")
    topic = args.topic.strip()

    wm = WorkingMemory(topic, args.memory_dir)
    state = wm.start()
    past_text = lt.load_past_research(topic)
    print(f"[리서치 {'재개' if state == 'resumed' else '시작'}] {topic} "
          f"(단계: {wm.progress['phase']})")
    if state == "resumed":
        p = wm.progress
        done, total = p["sources_processed"], p["sources_collected"]
        print(f"[작업 메모리 로드] {wm.progress_path}")
        print(f"  이미 자료 {done}개 처리됨 (전체 {total}개), 다음은 {done + 1}번째부터"
              f" · 단계 완료: {', '.join(p['phases_completed']) or '없음'}"
              f" · 남은 단계: {', '.join(p['phases_remaining'])}")
    else:
        print("[작업 메모리] 진행 기록 없음 -> 처음부터 시작")
    if past_text:
        print(f"[이전 리서치 발견] {lt.past_research_path(topic)} -> 컨텍스트와 종합 단계에 주입")

    mgr = ContextManager(researcher.SYSTEM_PROMPT, total_used=wm.progress["tokens_used"],
                         compress_at=args.compress_at, reflect_at=args.reflect_at,
                         task_limit=args.task_limit)
    client = researcher.get_client()
    summary, failed, stop_reason = None, [], None

    try:
        for phase in PHASES[PHASES.index(wm.progress["phase"]):]:
            wm.enter_phase(phase)
            print(f"== {phase} ==")
            if phase == "수집":
                n = collect(wm, args.source_mode, args.sources_dir)
                print(f"  신규 자료 {n}건 (전체 {wm.progress['sources_collected']}건)")
            elif phase == "정렬":
                print(f"  중복 제거 {wm.sort_sources()}건")
            elif phase == "분석":
                failed = analyze(client, mgr, wm, lt, topic, past_text)
            elif phase == "요약":
                if wm.progress["sources_processed"] == 0:
                    raise RuntimeError("처리된 자료가 없어 종합할 수 없습니다")
                summary = researcher.synthesize(client, mgr, wm, lt, topic, past_text)
                mgr.ctx.notes.append(f"## 최종 요약\n{summary}")
            elif phase == "정리":
                if summary is None:  # 요약 단계 직후 중단되었다가 재개된 경우
                    summary = researcher.synthesize(client, mgr, wm, lt, topic, past_text)
                    mgr.ctx.notes.append(f"## 최종 요약\n{summary}")
                mgr.flush_notes(wm)
                print(f"\n{summary}\n")
                if decide_save(args.save_long_term):
                    rows = [r for r in wm.read_sources() if r["processed"]]
                    path, updated = lt.save_past_research(
                        topic, summary, wm.keywords(10), [r["title"] for r in rows])
                    print(f"장기 메모리 저장: {path}")
                    if updated:
                        print(f"user_interests.md 갱신: {lt.interests_path}")
                wm.finish()
    except TaskTokenLimitExceeded as e:
        stop_reason = str(e)
        wm.stop("stopped", stop_reason)
    except RuntimeError as e:
        stop_reason = str(e)
        wm.stop("failed", stop_reason)
    finally:
        mgr.flush_notes(wm)
        wm.update_tokens(mgr.total_tokens)

    print(f"\n[작업 {'중단' if stop_reason else '완료'}]")
    if stop_reason:
        print(f"중단 사유: {stop_reason} (같은 주제로 다시 실행하면 이어서 진행합니다)")
    for title, why in failed:
        print(f"- 실패: {title}: {why}")
    print(f"세션 {mgr.session_no}개 · 토큰 사용량: {mgr.total_tokens:,} / {mgr.task_limit:,}")
    print(f"작업 메모리: {wm.dir}")
    return 1 if stop_reason else 0


if __name__ == "__main__":
    sys.exit(main())
