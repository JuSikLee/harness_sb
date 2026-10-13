"""자료 한 건을 처리(LLM 호출 -> 결과 저장)하고, 처리한 자료들을 종합하는 핵심 로직."""
import json
import re

from compressor import estimate_tokens, fit_body, summarize200

MODEL = "claude-sonnet-5-5"
MAX_OUTPUT = 2000
SYNTH_MAX_OUTPUT = 4000
PAST_DIGEST_CHARS = 800   # 자료별 호출에 넣는 이전 리서치 발췌 길이 (단기 컨텍스트는 좁게)
PAST_FULL_CHARS = 6000    # 종합 단계에 넣는 이전 리서치 길이

SYSTEM_PROMPT = "당신은 AI 관련 학과의 박사 과정 연구원입니다. 자료를 비판적으로 검토하고 핵심을 추출합니다."

OUTPUT_SPEC = """아래 JSON 객체만 출력하세요 (설명, 코드블록 금지).
{
  "summary": "자료의 핵심을 3~4문장으로 요약",
  "key_findings": ["핵심 발견 (최대 3개, 각 한 문장)"],
  "keywords": ["핵심 키워드 (최대 5개)"],
  "critique": "근거의 질 · 편향 · 한계에 대한 비판적 검토 (2문장 이내)",
  "new_vs_past": "[이전 리서치 요약]이 주어졌다면 그와 비교해 새로 알게 된 점 또는 달라진 점. 없으면 빈 문자열"
}"""


def get_client():
    import anthropic  # ANTHROPIC_API_KEY 환경변수 사용
    return anthropic.Anthropic()


def call_model(client, mgr, user, max_tokens):
    resp = client.messages.create(model=MODEL, max_tokens=max_tokens, system=mgr.system_prompt,
                                  messages=[{"role": "user", "content": user}])
    mgr.record(resp.usage)
    return "".join(b.text for b in resp.content if b.type == "text")


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("응답에 JSON이 없음")
    return json.loads(m.group(0))


def _strs(value, limit):
    items = value if isinstance(value, list) else []
    return [str(x).strip() for x in items if str(x).strip()][:limit]


def build_context_block(mgr, wm, lt, past_text):
    """매 호출에 항상 들어가는 단기 컨텍스트: 관심 분야 · 키워드 · 이전 리서치 · 작업 메모리 발췌."""
    parts = [f"[사용자 장기 관심 분야]\n{lt.interests_summary()}"]
    keywords = wm.keywords(50)
    if keywords:
        parts.append(f"[이전에 처리한 자료의 핵심 키워드]\n{', '.join(keywords)}")
    if past_text:
        parts.append("[이전 리서치 요약 (같은 주제)]\n" + past_text[:PAST_DIGEST_CHARS])
    excerpt = []
    if mgr.ctx.last_result:
        excerpt.append(f"- 직전 자료 분석 요약: {summarize200(mgr.ctx.last_result)}")
    excerpt += [f"- 지금까지의 발견: {f}" for f in wm.recent_findings(5)]
    if excerpt:
        parts.append("[작업 메모리 발췌]\n" + "\n".join(excerpt))
    return "\n\n".join(parts)


def process_source(client, mgr, wm, lt, topic, source, body, past_text=None):
    """자료 한 건 처리: 호출 -> 결과를 sources.jsonl · progress.json · 단기 노트에 반영."""
    mgr.begin_source()
    head = (f"리서치 주제: {topic}\n\n{build_context_block(mgr, wm, lt, past_text)}\n\n"
            f"[분석 대상 자료]\n제목: {source['title']}\n출처: {source['url']}\n본문:\n")
    body = fit_body(body, estimate_tokens(head + OUTPUT_SPEC), MAX_OUTPUT, mgr.source_limit)
    mgr.ctx.current_body = body
    user = f"{head}{body}\n\n주제와의 관련성을 기준으로 이 자료 한 건만 분석하세요.\n\n{OUTPUT_SPEC}"

    mgr.check_task_limit(estimate_tokens(user) + MAX_OUTPUT)
    text = call_model(client, mgr, user, MAX_OUTPUT)
    result = parse_json(text)

    summary = str(result.get("summary", "")).strip()
    findings = _strs(result.get("key_findings"), 3)
    keywords = _strs(result.get("keywords"), 5)
    critique = str(result.get("critique", "")).strip()
    new_vs_past = str(result.get("new_vs_past", "")).strip()

    wm.mark_processed(source["url"], summary, keywords, critique)
    wm.add_findings(findings)
    mgr.ctx.last_result = text
    note = [f"### {source['title']}", f"- 출처: {source['url']}", f"- 요약: {summary}",
            "- 핵심 발견: " + "; ".join(findings), f"- 비판적 검토: {critique}"]
    if new_vs_past:
        note.append(f"- 이전 리서치 대비: {new_vs_past}")
    mgr.ctx.notes.append("\n".join(note))
    return result


def synthesize(client, mgr, wm, lt, topic, past_text=None):
    """처리한 자료들을 하나의 최종 요약(마크다운)으로 종합. 이전 리서치가 있으면 함께 주입."""
    rows = [r for r in wm.read_sources() if r["processed"]]
    blocks = [f"### {r['title']} ({r['url']})\n- 요약: {r['summary']}\n"
              f"- 비판적 검토: {r.get('critique', '')}" for r in rows]
    parts = [f"리서치 주제: {topic}", f"[사용자 장기 관심 분야]\n{lt.interests_summary()}"]
    authors = lt.load_preferred_authors()
    if authors:
        parts.append("[사용자가 신뢰하는 저자]\n" + "\n".join(f"- {a}" for a in authors))
    if past_text:
        parts.append("[이전 리서치 요약 (같은 주제)]\n" + past_text[:PAST_FULL_CHARS])
    parts.append("[이번에 처리한 자료]\n" + "\n\n".join(blocks))
    parts.append("[지금까지의 핵심 발견]\n" + "\n".join(f"- {f}" for f in
                                                  wm.progress["key_findings_so_far"]))
    task = ("위 자료들을 종합해 마크다운 최종 요약을 작성하세요. 구성: "
            "## 개요 / ## 핵심 발견 / ## 비판적 검토 / ## 한계와 추가 조사 과제")
    if past_text:
        task += (" 이전 리서치 요약이 있으므로 '## 이전 리서치 대비 변화' 절을 추가해, "
                 "이미 알던 내용과 이번에 새로 알게 된 내용을 구분하세요.")
    user = "\n\n".join(parts) + f"\n\n{task} 자료에 없는 사실은 만들지 마세요."

    mgr.check_task_limit(estimate_tokens(user) + SYNTH_MAX_OUTPUT)
    return call_model(client, mgr, user, SYNTH_MAX_OUTPUT).strip()
