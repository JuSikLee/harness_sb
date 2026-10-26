"""사내 문서 검색 에이전트 진입점: python main.py --question "..." """
import argparse
import hashlib
import json
import os
import sys
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path

# 시스템 python3로 실행돼도 프로젝트 venv(.venv)의 파이썬으로 자동 전환
_VENV_PY = Path("/home/prime/harness_sb/doc-search/.venv/bin/python")
if _VENV_PY.exists() and Path(sys.prefix).resolve() != _VENV_PY.parent.parent.resolve():
    os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])

sys.path.insert(0, str(Path(__file__).resolve().parent))

import answerer
import searcher
import validator

ROOT = Path("/home/prime/harness_sb/doc-search")
OUT_DIR = ROOT / "output" / "searches"
MEM_DIR = ROOT / "memory" / "doc-search"
RECENT = MEM_DIR / "working" / "recent_questions.json"
FAQ_FILE = MEM_DIR / "long_term" / "faq_extracted.md"
ABBR_FILE = MEM_DIR / "long_term" / "abbreviations.md"
SEARCHED = "faq/ · hr/ · onboarding/ · operations/ (harness-book-samples/doc-search)"
MAX_RETRY = 2
TOKEN_LIMIT = 20000
INTERNAL_KEYWORDS = ["회사", "정책", "우리", "사내"]
INTERNAL_ONLY_MSG = "사내 답변 권장 - HR 직접 문의"
EXTERNAL_FOOTER = "외부 출처. 사내 정책과 다를 수 있음"
TAVILY_URL = "https://api.tavily.com/search"


def qhash(q):
    return hashlib.sha256(q.encode("utf-8")).hexdigest()[:12]


def load_recent():
    try:
        data = json.loads(RECENT.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    cutoff = datetime.now() - timedelta(days=7)
    return {k: v for k, v in data.items() if datetime.fromisoformat(v["time"]) > cutoff}


def save_recent(data):
    RECENT.parent.mkdir(parents=True, exist_ok=True)
    RECENT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def write_no_answer(h, reason, refusal_only=False):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{h}_no_answer.md"
    if refusal_only:
        path.write_text(reason + "\n", encoding="utf-8")
        return path
    path.write_text(f"# 답변 없음\n\n## 사유\n{reason}\n\n## 재질문 제안\n질문을 더 구체적으로 바꾸거나 관련 문서명을 포함해 다시 질문해 주세요.\n", encoding="utf-8")
    return path


def render(question, r, masked):
    fix = answerer.mask_c2 if masked else (lambda s: s)
    cites = "\n".join(f"- [{c['doc']} · {c['section']}] \"{c['quote']}\"" for c in r.get("citations", [])) or "- (없음)"
    rel = "\n".join(f"- {x['doc']}: {x['reason']}" for x in r.get("related", [])) or "- (없음)"
    fol = "\n".join(f"- {x}" for x in r.get("followups", [])) or "- (없음)"
    return fix(f"# 검색 결과\n\n질문: {question}\n\n## 핵심 답변\n{r['answer']}\n\n## 근거 인용\n{cites}\n\n## 관련 문서 추천\n{rel}\n\n## 추가 질문 제안\n{fol}\n")


def tavily_search(question, k=3):
    """Tavily 상위 k개 결과. TAVILY_API_KEY 없으면 RuntimeError."""
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("TAVILY_API_KEY 환경 변수가 설정되지 않음")
    r = requests.post(TAVILY_URL, headers={"Authorization": f"Bearer {key}"},
                      json={"query": question, "max_results": k}, timeout=20)
    r.raise_for_status()
    return r.json().get("results", [])[:k]


def external_fallback(question, h, grade, internal_reason):
    """사내 정보 없음 → Tavily 검색 → answerer 답변. 사내 정책성 질문이면 호출하지 않는다."""
    if any(t in question for t in INTERNAL_KEYWORDS):
        return write_no_answer(h, f"{internal_reason}\n\n{INTERNAL_ONLY_MSG}")
    try:
        results = tavily_search(question)
        if not results:
            return write_no_answer(h, f"{internal_reason}\n외부 검색 결과도 없음")
        text, _ = answerer.generate_external(question, results)
    except Exception as e:
        return write_no_answer(h, f"{internal_reason}\n외부 검색 실패: {e}")
    srcs = "\n".join(f"- [{i}] {r.get('title', '')} {r.get('url', '')}" for i, r in enumerate(results, 1))
    out = f"# [외부 검색]\n\n질문: {question}\n\n## 핵심 답변\n{text}\n\n## 외부 출처\n{srcs}\n\n{EXTERNAL_FOOTER}\n"
    if grade == "C2":
        out = answerer.mask_c2(out)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{h}_answer.md"
    path.write_text(out, encoding="utf-8")
    return path


def track_memory(question, h, recent, answer_text):
    entry = recent.get(h, {"count": 0})
    recent[h] = {"time": datetime.now().isoformat(), "question": question, "count": entry["count"] + 1}
    save_recent(recent)
    if recent[h]["count"] >= 5:
        FAQ_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(FAQ_FILE, "a", encoding="utf-8") as f:
            f.write(f"- FAQ 후보: {question}\n")
    known = ABBR_FILE.read_text(encoding="utf-8") if ABBR_FILE.exists() else ""
    import re
    new = sorted({t for t in re.findall(r"\b[A-Z]{2,6}\b", answer_text) if t not in known})
    if new:
        ABBR_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ABBR_FILE, "a", encoding="utf-8") as f:
            for t in new:
                f.write(f"- {t} (후보)\n")


def run(question):
    res = _run(question)
    if isinstance(res, dict):
        return res
    return {"path": res, "blocked": False, "meta": {"engine": searcher.backend(), "classification": answerer.classify(question), "rerank": "claude"}}


def _run(question):
    h = qhash(question)
    grade = answerer.classify(question)  # 검색·임베딩 이전
    if grade == "C1":  # searcher/rerank/answerer/Claude 호출 없이 즉시 반환. 원문은 저장하지 않는다(해시만)
        path = write_no_answer(h, answerer.C1_REFUSAL, refusal_only=True)
        return {"path": path, "blocked": True, "meta": {"engine": "n/a", "classification": "C1", "rerank": None}}

    recent = load_recent()
    cached = OUT_DIR / f"{h}_answer.md"
    if h in recent and cached.exists() and datetime.now() - datetime.fromisoformat(recent[h]["time"]) < timedelta(hours=24):
        recent[h]["count"] += 1
        save_recent(recent)
        return cached

    start, tokens = time.time(), 0
    found = searcher.search(question)
    if found["status"] == "no_info":
        return external_fallback(question, h, grade, f"사내 문서에는 정보 없음 (검색 폴더: {SEARCHED}, 최고 유사도 {found['top_score']:.2f} < 임계값 {found['threshold']})")

    diag, result, issues = None, None, []
    for _ in range(MAX_RETRY + 1):
        result, used = answerer.generate(question, found["hits"], diag)
        tokens += used
        issues = validator.validate(result, found["hits"], SEARCHED)
        if not issues:
            break
        if tokens >= TOKEN_LIMIT or time.time() - start > 60:
            break
        diag = validator.diagnostic(question, issues)
    if issues:
        return write_no_answer(h, "검증 실패: " + "; ".join(issues))
    if "정보 없음" in result["answer"]:
        return external_fallback(question, h, grade, f"사내 문서에는 정보 없음 (검색 폴더: {SEARCHED})")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    text = render(question, result, grade == "C2")
    cached.write_text(text, encoding="utf-8")
    track_memory(question, h, recent, text)
    return cached


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", required=True)
    args = ap.parse_args()
    res = run(args.question)
    print(res["path"].read_text(encoding="utf-8"))
    meta = res["meta"]
    print(f"[meta] blocked={res['blocked']} engine={meta['engine']} classification={meta['classification']} rerank={meta['rerank']}")
