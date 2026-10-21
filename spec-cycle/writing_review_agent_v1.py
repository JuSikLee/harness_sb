#!/usr/bin/env python3
"""글쓰기 첨삭 에이전트 --- specs/spec_template.md 구현.

사용: python3 writing_review_agent.py [--date YYYY-MM-DD]
산출: outputs/review_{date}_{NN}.docx, runs/run_{date}_{NN}.json

아래 # ASSUMED 목록은 스펙에 적혀 있지 않아 구현자(모델)가 임의로 채운 결정이다.
스펙 빈칸을 진단할 때 이 목록을 단서로 쓴다.

# ASSUMED: LLM 호출 수단 --- "외부 API 호출 금지"이면서 "클로드 코드 Sonnet 사용"이므로,
#          API SDK 대신 로컬 `claude -p --model sonnet` CLI를 subprocess로 호출한다.
# ASSUMED: "sub-agent 사용 안 함"의 해석 --- 별도 에이전트 정의 없이 단일 스크립트가 단계별로
#          CLI를 호출하는 것은 허용으로 본다. 항목별 호출은 스레드 4개까지 병렬로 돌린다.
# ASSUMED: 문장 분할 기준 --- 줄(문단) 단위로 먼저 나눈 뒤, 마침표/물음표/느낌표(. ? !) 뒤 공백에서 자른다.
#          '#'로 시작하는 제목 줄은 문장 목록에서 제외한다(단, 제목은 맥락으로 모델에 전달하지 않는다).
# ASSUMED: 문장 인덱스는 0부터 시작하고, 문장 텍스트는 draft.md의 원문 부분 문자열 그대로 유지한다(정규화 없음).
# ASSUMED: author_intent.md 파싱 --- 줄바꿈 또는 " / "로 구분된 "키: 값" 조각을 dict로 만든다.
#          키: 값 형태가 아닌 조각은 "기타_N" 키에 담는다.
# ASSUMED: 등급 의미 --- ★ = 먼저 손봐야 할 문제, △ = 다듬으면 좋은 부분, √ = 특별한 문제 없음.
#          (스펙이 "★ 또는 △가 나온 부분"을 수정 대상으로 쓰므로 ★/△가 문제 신호라고 해석)
# ASSUMED: 진단 호출 단위 --- 4항목을 각각 별도 LLM 호출(항목별 독립 실패 가능)로 처리한다.
# ASSUMED: 진단 인용문 --- 근거가 되는 한 문장 안의 부분 문자열, 최대 80자. 모델이 준 인용이 원문에
#          없으면 해당 문장 앞 80자로 대체한다. 말줄임표는 원문 불일치를 만들므로 쓰지 않는다.
#          √ 등급 항목도 대표 문장 1개를 인용한다("모든 진단에 인용" 요구).
# ASSUMED: 수정 구간 선정 --- ★ 항목의 문제 문장을 먼저, △ 항목을 다음으로 모아 중복 제거 후 앞에서
#          최대 5개. 3개 미만이면 LLM에 추가 후보 문장을 1회 요청하고, 그래도 3개 미만이면 있는 만큼만 진행한다.
# ASSUMED: 항목당 문제 문장 후보는 최대 2개.
# ASSUMED: 대안 생성 전략 --- 문장 하나를 대상으로 앞뒤 1문장 맥락과 작성 의도를 주고,
#          (a) 의미는 유지하되 문제만 고친 안, (b) 필요 시 구조를 바꾼 안 순으로 1~2개를 낸다.
# ASSUMED: 수정안 인용문 --- 대상 문장 전체(80자 초과 시 앞 80자). 문장이 짧은 편이라 전체 인용이 안전.
# ASSUMED: 코멘트 어조 --- "함께 다듬는 동료" 시각, 합니다체, 제안형("~해 보면 어떨까요", "~로 읽힙니다").
#          평가어(잘했다/못했다/더 노력해야)와 명령형은 프롬프트로 금지한다.
# ASSUMED: 코멘트는 한 줄 = 줄바꿈 없는 한 문장, 120자 이내. 이유는 80자 이내.
# ASSUMED: 종합 단락 --- 감정 1줄 + 핵심 개선 포인트 1줄을 이어 붙여 150자 이내. 초과하거나 생성 실패 시
#          고정 문구 폴백(하드코딩)을 사용한다.
# ASSUMED: docx 서식 --- python-docx 미설치 환경이므로 zipfile로 최소 OOXML을 직접 쓴다.
#          제목=Heading1, 섹션=Heading2, 본문=일반 단락. 글꼴 맑은 고딕 10.5pt. "한 페이지"는 분량 억제로만 노력한다.
# ASSUMED: 진단 줄 형식 "문법 — △ — 코멘트", 수정안 줄 형식 "1. 「원문」 → 대안: A / B → 이유: ..." (검사 파서와 동일 형식).
# ASSUMED: 헤더 제목의 "{원본 파일명}"은 draft.md 파일명만 쓴다.
# ASSUMED: LLM 호출 한도 --- 호출당 타임아웃 240초, 실패 시 1회 재시도(총 2회). 토큰 한도는 CLI 기본값에 맡긴다.
# ASSUMED: 입력이 3,000자를 넘으면 경고만 로그에 남기고 그대로 진행한다(스펙은 "가정"이라고만 함).
# ASSUMED: 단계 부분 실패 처리 --- 실패한 항목/구간은 건너뛰고 로그에 남긴다. 실패 항목은 docx에
#          "(진단 실패)" 줄로 표시하되 등급 기호는 넣지 않으므로 verify 규칙 2가 정직하게 fail 한다.
# ASSUMED: verify 결정론 규칙의 섹션 판별 --- Heading1 1개(제목, "글쓰기 첨삭"으로 시작) + Heading2 3개 제목 일치.
# ASSUMED: 규칙 5(인용 일치)는 LLM 판단이 원칙이지만, 기계적 부분 문자열 검사 결과를 증거로 함께 주고
#          기계 검사에서 불일치가 있으면 LLM이 pass라 해도 fail로 기록한다.
# ASSUMED: 규칙 6·7 입력 --- 진단 4줄과 수정안 전체 텍스트를 그대로 LLM에 주고 JSON 판정을 받는다.
# ASSUMED: 추론 규칙에서 LLM 호출 자체가 실패하면 pass가 아니라 fail("판정 불가")로 기록한다.
# ASSUMED: 결정론 fail 시 추론 규칙은 실행하지 않고 "skipped"로 기록하며 전체 결과는 fail.
# ASSUMED: run JSON 스키마 --- {date, input, output, result, deterministic[], inference[], step_log[]}.
# ASSUMED: 날짜 --- 기본은 오늘(로컬), --date로 덮어쓸 수 있다.
# ASSUMED: 파일명 시퀀스 --- outputs/review_{date}_{NN}.docx, runs/run_{date}_{NN}.json. 두 디렉토리에서 같은 날짜의
#          _NN 파일 중 가장 큰 번호 + 1을 쓴다(없으면 01). 두 자리 0 패딩, 99 초과 시 자릿수만 늘어남.
#          시퀀스 없는 옛 형식(review_{date}.docx)은 번호 계산에 포함하지 않고 건드리지도 않는다.
#          docx와 json은 항상 같은 번호를 공유한다. 실행은 동시에 하나만 돈다고 가정(락 없음).
"""

import argparse
import json
import re
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
ITEMS = ["문법", "논리", "톤", "근거"]
GRADES = ["★", "△", "√"]
DISCLAIMER = "이 첨삭은 최종 결정이 아니며, 작성자의 의도가 우선합니다."
SECTIONS = ["전반 진단", "구간별 수정안", "종합"]
QUOTE_MAX = 80
LLM_TIMEOUT = 240
LLM_ATTEMPTS = 2
FALLBACK_SUMMARY = "함께 읽어 보니 독자를 배려한 구성이 돋보였습니다. 표시한 구간의 문장을 다듬으면 글이 더 매끄러워질 것 같습니다."

TONE_RULES = (
    "어조 규칙: 함께 글을 다듬는 동료의 시선으로, 합니다체를 쓰고 제안형('~해 보면 어떨까요', '~로 읽힐 수 있습니다')으로 말합니다. "
    "'잘했다', '못했다', '더 노력해야' 같은 평가 표현과 '~하세요', '~해야 합니다' 같은 지시·명령형, 선생이 학생에게 말하는 어조는 쓰지 않습니다. "
    "점수나 숫자 평가도 쓰지 않습니다."
)


# ---------------------------------------------------------------- LLM
def llm(prompt: str) -> str:
    last = None
    for _ in range(LLM_ATTEMPTS):
        try:
            r = subprocess.run(
                ["claude", "-p", "--model", "sonnet"],
                input=prompt, capture_output=True, text=True, timeout=LLM_TIMEOUT,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
            last = f"rc={r.returncode} {r.stderr.strip()[:200]}"
        except Exception as e:  # timeout 등
            last = repr(e)
    raise RuntimeError(f"LLM 호출 실패: {last}")


def llm_json(prompt: str) -> dict:
    last = None
    for _ in range(LLM_ATTEMPTS):
        out = llm(prompt + "\n\n도구는 쓰지 말고, 설명 없이 JSON 객체 하나만 출력하세요.")
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError as e:
                last = e
        else:
            last = "JSON 없음"
    raise RuntimeError(f"JSON 파싱 실패: {last}")


# ---------------------------------------------------------------- step 1
def split_sentences(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for s in re.split(r"(?<=[.?!])\s+", line):
            if s.strip():
                out.append(s.strip())
    return out


def parse_intent(text: str) -> dict:
    intent, n = {}, 0
    for part in re.split(r"\n|\s/\s", text):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"([^:：]{1,20})[:：]\s*(.+)", part)
        if m:
            intent[m.group(1).strip()] = m.group(2).strip()
        else:
            n += 1
            intent[f"기타_{n}"] = part
    return intent


def step1_read(folder: Path) -> dict:
    draft = (folder / "draft.md").read_text(encoding="utf-8")
    intent = (folder / "author_intent.md").read_text(encoding="utf-8")
    return {"draft": draft, "sentences": split_sentences(draft), "intent": parse_intent(intent)}


# ---------------------------------------------------------------- helpers
def numbered(sentences: list[str]) -> str:
    return "\n".join(f"[{i}] {s}" for i, s in enumerate(sentences))


def fit_quote(quote, idx, sentences) -> str:
    """인용문이 해당 문장의 부분 문자열이 되도록 보정(최대 QUOTE_MAX자)."""
    sent = sentences[idx]
    q = quote.strip() if isinstance(quote, str) else ""
    if not q or q not in sent:
        q = sent
    return q[:QUOTE_MAX]


def one_line(s, limit) -> str:
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s[:limit]


def as_index(v, n):
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i if 0 <= i < n else None


# ---------------------------------------------------------------- step 2
ITEM_FOCUS = {
    "문법": "맞춤법, 조사·어미 호응, 문장 성분 호응, 비문",
    "논리": "주장과 근거의 연결, 비약, 중복, 문단 간 흐름, 과장된 일반화",
    "톤": "작성 의도에 적힌 문체·격식·독자 눈높이와 어긋나는 부분",
    "근거": "주장을 뒷받침하는 구체적 예시·수치·출처가 있는지, 단정적 표현이 근거 없이 쓰였는지",
}


def diagnose_item(item: str, ctx: dict) -> dict:
    s = ctx["sentences"]
    prompt = f"""당신은 글쓰기 첨삭을 돕는 동료 편집자입니다. 아래 글을 '{item}' 관점({ITEM_FOCUS[item]})으로만 점검하세요.
{TONE_RULES}

[작성 의도]
{json.dumps(ctx['intent'], ensure_ascii=False)}

[본문 문장 (인덱스: 문장)]
{numbered(s)}

등급 기준: ★=먼저 손보면 좋은 문제가 있음, △=다듬으면 좋은 부분이 있음, √=특별한 문제 없음.
JSON 형식:
{{"grade": "★|△|√", "comment": "한 줄 코멘트(120자 이내, 줄바꿈 없음)", "evidence_index": 근거가 된 대표 문장 인덱스(정수), "quote": "그 문장 안에서 그대로 복사한 인용(80자 이내, 말줄임표 금지)", "issue_indices": [★/△일 때 문제 문장 인덱스 최대 2개, √이면 빈 배열]}}
코멘트에는 인용 내용을 자연스럽게 언급하세요."""
    d = llm_json(prompt)
    grade = d.get("grade")
    if grade not in GRADES:
        raise RuntimeError(f"등급 오류: {grade!r}")
    ei = as_index(d.get("evidence_index"), len(s))
    issues = [i for i in (as_index(x, len(s)) for x in d.get("issue_indices", [])) if i is not None][:2]
    if ei is None:
        ei = issues[0] if issues else 0
    quote = fit_quote(d.get("quote"), ei, s)
    comment = one_line(d.get("comment", ""), 120)
    if quote not in comment:
        comment = f"{comment} (「{quote}」)"
    return {"grade": grade, "comment": comment, "quote": quote,
            "evidence_index": ei, "issue_indices": issues if grade != "√" else []}


def step2_diagnose(ctx: dict, log: list) -> dict:
    def run(item):
        try:
            return item, diagnose_item(item, ctx)
        except Exception as e:
            log.append(f"step2 {item} 실패: {e}")
            return item, None
    with ThreadPoolExecutor(max_workers=4) as ex:
        return {k: v for k, v in ex.map(run, ITEMS) if v}


# ---------------------------------------------------------------- step 3
def step3_select(ctx: dict, diag: dict, log: list) -> list[int]:
    picked: list[int] = []
    for want in ("★", "△"):
        for item in ITEMS:
            d = diag.get(item)
            if d and d["grade"] == want:
                for i in d["issue_indices"]:
                    if i not in picked:
                        picked.append(i)
    picked = picked[:5]
    if len(picked) < 3:
        try:
            s = ctx["sentences"]
            d = llm_json(f"""아래 글에서 함께 다듬으면 좋을 문장을 {3 - len(picked)}~{5 - len(picked)}개 더 골라 주세요.
이미 고른 인덱스: {picked}
작성 의도: {json.dumps(ctx['intent'], ensure_ascii=False)}
{numbered(s)}
JSON 형식: {{"indices": [정수, ...]}}""")
            for x in d.get("indices", []):
                i = as_index(x, len(s))
                if i is not None and i not in picked and len(picked) < 5:
                    picked.append(i)
        except Exception as e:
            log.append(f"step3 보충 실패: {e}")
    if len(picked) < 3:
        log.append(f"step3 구간 {len(picked)}개만 확보")
    return picked


# ---------------------------------------------------------------- step 4
def revise_one(idx: int, ctx: dict, diag: dict) -> dict:
    s = ctx["sentences"]
    near = "\n".join(f"[{i}] {s[i]}" for i in range(max(0, idx - 1), min(len(s), idx + 2)))
    notes = [f"{k}: {v['comment']}" for k, v in diag.items() if idx in v["issue_indices"]]
    prompt = f"""당신은 함께 글을 다듬는 동료 편집자입니다. 아래 [{idx}]번 문장의 대안을 제안하세요.
{TONE_RULES}

[작성 의도]
{json.dumps(ctx['intent'], ensure_ascii=False)}

[앞뒤 맥락]
{near}

[대상 문장] {s[idx]}
[관련 진단] {' / '.join(notes) or '없음'}

대안은 1~2개. 첫 번째는 의미를 유지하며 문제만 고친 안, 두 번째는 필요할 때만 구조를 바꾼 안입니다. 작성 의도의 문체(합니다체)를 지키세요.
JSON 형식: {{"alternatives": ["대안1", "대안2(선택)"], "reason": "한 줄 이유(80자 이내, 줄바꿈 없음)"}}"""
    d = llm_json(prompt)
    alts = [one_line(a, 200) for a in d.get("alternatives", []) if str(a).strip()][:2]
    reason = one_line(d.get("reason", ""), 80)
    if not alts or not reason:
        raise RuntimeError("대안/이유 누락")
    return {"index": idx, "quote": s[idx][:QUOTE_MAX], "alternatives": alts, "reason": reason}


def step4_revise(ctx: dict, diag: dict, indices: list[int], log: list) -> list[dict]:
    def run(i):
        try:
            return revise_one(i, ctx, diag)
        except Exception as e:
            log.append(f"step4 [{i}] 실패: {e}")
            return None
    with ThreadPoolExecutor(max_workers=4) as ex:
        return [r for r in ex.map(run, indices) if r]


# ---------------------------------------------------------------- step 5
def make_summary(diag: dict, revs: list[dict], log: list) -> str:
    try:
        d = llm_json(f"""아래 첨삭 결과를 보고 종합 단락의 두 줄을 쓰세요.
{TONE_RULES}
진단: {json.dumps({k: [v['grade'], v['comment']] for k, v in diag.items()}, ensure_ascii=False)}
수정안 이유: {json.dumps([r['reason'] for r in revs], ensure_ascii=False)}
JSON 형식: {{"feeling": "글을 읽고 든 느낌 1줄(50자 이내)", "point": "핵심 개선 포인트 1줄(70자 이내)"}}""")
        text = f"{one_line(d['feeling'], 70)} {one_line(d['point'], 90)}"
        if len(text) <= 150:
            return text
        log.append(f"step5 종합 {len(text)}자 초과 -> 폴백")
    except Exception as e:
        log.append(f"step5 종합 생성 실패 -> 폴백: {e}")
    return FALLBACK_SUMMARY


def para(text, style=None) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def write_docx(path: Path, paragraphs: list[tuple[str, str | None]]):
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(para(t, s) for t, s in paragraphs)
    document = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {ns}><w:body>{body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr></w:body></w:document>'
    font = '<w:rFonts w:ascii="Malgun Gothic" w:hAnsi="Malgun Gothic" w:eastAsia="Malgun Gothic"/>'
    styles = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {ns}>
<w:docDefaults><w:rPrDefault><w:rPr>{font}<w:sz w:val="21"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="80" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="0" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="30"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="200" w:after="80"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>
</w:styles>'''
    ct = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>'
    rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    drels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/_rels/document.xml.rels", drels)


def step5_output(path: Path, draft_name: str, day: str, diag: dict, revs: list[dict], log: list):
    P: list[tuple[str, str | None]] = [
        (f"글쓰기 첨삭 --- {draft_name} --- {day}", "Heading1"),
        (DISCLAIMER, None),
        ("전반 진단", "Heading2"),
    ]
    for item in ITEMS:
        d = diag.get(item)
        P.append((f"{item} — {d['grade']} — {d['comment']}" if d else f"{item} — (진단 실패)", None))
    P.append(("구간별 수정안", "Heading2"))
    for n, r in enumerate(revs, 1):
        P.append((f"{n}. 「{r['quote']}」 → 대안: {' / '.join(r['alternatives'])} → 이유: {r['reason']}", None))
    P.append(("종합", "Heading2"))
    P.append((make_summary(diag, revs, log), None))
    write_docx(path, P)


# ---------------------------------------------------------------- verify (§6)
def read_docx(path: Path) -> list[tuple[str, str]]:
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    out = []
    for p in root.iter(f"{w}p"):
        st = p.find(f"{w}pPr/{w}pStyle")
        style = st.get(f"{w}val") if st is not None else ""
        out.append((style, "".join(t.text or "" for t in p.iter(f"{w}t"))))
    return out


def split_doc(paras):
    head, secs, cur = [], {}, None
    for style, text in paras:
        if style == "Heading2":
            cur = text.strip()
            secs[cur] = []
        elif cur is None:
            head.append((style, text))
        else:
            secs[cur].append(text)
    return head, secs


REV_RE = re.compile(r"^\s*\d+\.\s*「(.+?)」\s*→\s*대안:\s*(.+?)\s*→\s*이유:\s*(.+)$")
DIAG_RE = re.compile(r"^(문법|논리|톤|근거)\s*—\s*(.*)$")
QUOTE_RE = re.compile(r"「(.+?)」")


def verify(docx_path: Path, draft_text: str) -> dict:
    """§6 verification criteria. 결정론 규칙 1~4 -> 전부 통과해야 추론 규칙 5~7 실행."""
    det, inf = [], []
    paras = read_docx(docx_path)
    head, secs = split_doc(paras)

    def add(lst, rid, name, ok, comment):
        lst.append({"rule": rid, "name": name, "result": "pass" if ok else "fail", "comment": comment})

    # 1. 4개 섹션 존재
    has_title = any(s == "Heading1" and t.startswith("글쓰기 첨삭") for s, t in head)
    missing = [x for x in SECTIONS if x not in secs]
    add(det, 1, "4개 섹션 존재", has_title and not missing,
        "모두 존재" if has_title and not missing else f"제목 {'있음' if has_title else '없음'}, 누락 섹션: {missing}")

    # 2. 전반 진단 4항목 + 등급 기호
    lines = [t for t in secs.get("전반 진단", []) if t.strip()]
    found, bad = {}, []
    for t in lines:
        m = DIAG_RE.match(t)
        if not m:
            bad.append(t[:30])
            continue
        g = [c for c in t if c in GRADES]
        found[m.group(1)] = g
    ok2 = (len(lines) == 4 and not bad and sorted(found) == sorted(ITEMS)
           and all(len(g) >= 1 and g[0] in GRADES and DIAG_RE.match(l).group(2).strip()[:1] in GRADES
                   for l, g in zip(lines, found.values())))
    add(det, 2, "진단 4항목+등급", ok2,
        "4항목 모두 등급 기호 있음" if ok2 else f"줄 수 {len(lines)}, 항목 {sorted(found)}, 형식 불일치 {bad}")

    # 3. 수정안 3~5개, 세 부분
    revs = [t for t in secs.get("구간별 수정안", []) if t.strip()]
    parsed = [REV_RE.match(t) for t in revs]
    ok3 = 3 <= len(revs) <= 5 and all(parsed)
    add(det, 3, "수정안 3~5개·세 부분", ok3,
        f"{len(revs)}개, 모두 형식 충족" if ok3 else f"{len(revs)}개, 형식 미충족 {sum(1 for p in parsed if not p)}개")

    # 4. 헤더 면책
    ok4 = any(t.startswith("이 첨삭은 최종 결정이 아니며") for _, t in head)
    add(det, 4, "헤더 면책 문구", ok4, "있음" if ok4 else "헤더 섹션에 면책 문구 없음")

    if any(d["result"] == "fail" for d in det):
        for rid, name in ((5, "인용문 원문 일치"), (6, "등급-코멘트 일관성"), (7, "어조")):
            inf.append({"rule": rid, "name": name, "result": "skipped", "comment": "결정론 규칙 실패로 실행하지 않음"})
        return {"result": "fail", "deterministic": det, "inference": inf}

    diag_text = "\n".join(lines)
    rev_text = "\n".join(revs)

    # 5. 인용문 일치 (기계 증거 + LLM 판정)
    quotes = QUOTE_RE.findall(diag_text) + [m.group(1) for m in parsed if m]
    mism = [q for q in quotes if q not in draft_text]
    try:
        d = llm_json(f"""아래 인용문들이 원문(draft.md)에 공백·구두점까지 정확히 일치하는 형태로 존재하는지 판정하세요.
기계 검사(부분 문자열 일치) 결과 불일치 목록: {json.dumps(mism, ensure_ascii=False)}
인용문 전체: {json.dumps(quotes, ensure_ascii=False)}
[원문]
{draft_text}
JSON 형식: {{"pass": true|false, "comment": "한 줄 근거"}}""")
        ok5 = bool(d.get("pass")) and not mism
        c5 = d.get("comment", "") + (f" / 기계 검사 불일치: {mism}" if mism else "")
    except Exception as e:
        ok5, c5 = False, f"판정 불가: {e}"
    add(inf, 5, "인용문 원문 일치", ok5, c5)

    # 6. 등급-코멘트 일관성
    try:
        d = llm_json(f"""등급 기호의 의미: ★=먼저 손볼 문제 있음, △=다듬으면 좋은 부분 있음, √=특별한 문제 없음.
아래 4항목 진단에서 등급과 코멘트 내용이 서로 일관되는지 판정하세요. 예: 코멘트가 '문제가 많다'는 취지인데 √이면 불일치.
{diag_text}
JSON 형식: {{"pass": true|false, "comment": "한 줄 근거(불일치가 있다면 어느 항목인지)"}}""")
        ok6, c6 = bool(d.get("pass")), d.get("comment", "")
    except Exception as e:
        ok6, c6 = False, f"판정 불가: {e}"
    add(inf, 6, "등급-코멘트 일관성", ok6, c6)

    # 7. 어조
    try:
        d = llm_json(f"""아래 코멘트와 수정안의 어조를 검사하세요. 다음이 하나라도 있으면 fail입니다:
- 평가성 표현('잘했다', '못했다', '더 노력해야' 등)
- 선생이 학생에게 말하는 듯한 어조(훈계, 지시, 칭찬·꾸짖음)
[진단]
{diag_text}
[수정안]
{rev_text}
JSON 형식: {{"pass": true|false, "comment": "한 줄 근거(해당 표현이 있다면 인용)"}}""")
        ok7, c7 = bool(d.get("pass")), d.get("comment", "")
    except Exception as e:
        ok7, c7 = False, f"판정 불가: {e}"
    add(inf, 7, "어조", ok7, c7)

    allpass = all(d["result"] == "pass" for d in det + inf)
    return {"result": "pass" if allpass else "fail", "deterministic": det, "inference": inf}


# ---------------------------------------------------------------- main
def next_seq(day: str) -> int:
    pat = re.compile(rf"^(?:review|run)_{re.escape(day)}_(\d+)\.(?:docx|json)$")
    nums = [int(m.group(1)) for d in (ROOT / "outputs", ROOT / "runs") if d.is_dir()
            for f in d.iterdir() if (m := pat.match(f.name))]
    return max(nums, default=0) + 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=_date.today().isoformat())
    ap.add_argument("--inputs", default=str(ROOT / "inputs"))
    a = ap.parse_args()
    day = a.date
    seq = f"{next_seq(day):02d}"
    out_docx = ROOT / "outputs" / f"review_{day}_{seq}.docx"
    out_run = ROOT / "runs" / f"run_{day}_{seq}.json"
    log: list[str] = []

    try:
        ctx = step1_read(Path(a.inputs))
    except Exception as e:
        sys.exit(f"step1 실패(입력 읽기): {e}")
    if len(ctx["draft"]) > 3000:
        log.append(f"경고: 본문 {len(ctx['draft'])}자 > 3,000자 가정 초과")
    log.append(f"step1 문장 {len(ctx['sentences'])}개, 의도 키 {list(ctx['intent'])}")

    diag = step2_diagnose(ctx, log)
    log.append(f"step2 진단 성공 항목: {list(diag)}")
    indices = step3_select(ctx, diag, log)
    log.append(f"step3 수정 대상 인덱스: {indices}")
    revs = step4_revise(ctx, diag, indices, log)
    log.append(f"step4 수정안 {len(revs)}개")
    step5_output(out_docx, "draft.md", day, diag, revs, log)
    log.append(f"step5 출력: {out_docx.relative_to(ROOT)}")

    result = verify(out_docx, ctx["draft"])
    run = {"date": day, "input": ["inputs/draft.md", "inputs/author_intent.md"],
           "output": str(out_docx.relative_to(ROOT)), **result, "step_log": log}
    out_run.parent.mkdir(parents=True, exist_ok=True)
    out_run.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: run[k] for k in ("result", "deterministic", "inference")}, ensure_ascii=False, indent=2))
    print("\n".join(log))


if __name__ == "__main__":
    main()
