#!/usr/bin/env python3
"""글쓰기 첨삭 에이전트 --- specs/spec_template.md (v2) 구현.

사용: python3 writing_review_agent.py [--date YYYY-MM-DD] [--inputs DIR]
산출: outputs/review_{date}_v2_{NN}.docx, runs/run_{date}_v2_{NN}.json

# ASSUMED: 스펙에 없어 구현자가 채운 결정
# ASSUMED: 진단 JSON의 문제 문장 인덱스(issue_indices)는 ★/△일 때만 받고, 근거 문장 인덱스는 항상 받는다.
# ASSUMED: 코멘트가 120자를 넘으면 인용을 40자로 줄이고 코멘트 앞부분을 잘라 인용 표기를 보존한다.
# ASSUMED: 수정안의 대안·이유에서는 모델이 만든 「」를 제거해 인용 개수를 항상 일정하게 유지한다.
# ASSUMED: 수정안 대안 1개의 길이는 200자로 자른다.
# ASSUMED: 규칙 5의 기대 인용 수 = 4(진단) + 수정안 개수.
# ASSUMED: 모델 응답에서 JSON은 첫 '{'부터 마지막 '}'까지를 파싱하고, 실패하면 같은 호출을 재시도한다.
# ASSUMED: 중간 산출물 저장 없이 실행마다 메모리에서만 처리한다.
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
SECTIONS = ["전반 진단", "구간별 수정안", "종합"]
DISCLAIMER = "이 첨삭은 최종 결정이 아니며, 작성자의 의도가 우선합니다."
FALLBACK_SUMMARY = "함께 읽어 보니 독자를 배려한 구성이 돋보였습니다. 표시한 구간의 문장을 다듬으면 글이 더 매끄러워질 것 같습니다."
QUOTE_MAX, COMMENT_MAX, REASON_MAX, SUMMARY_MAX = 80, 120, 80, 150
LLM_TIMEOUT, LLM_ATTEMPTS, WORKERS = 240, 2, 4

GRADE_DEF = "★=먼저 손봐야 할 문제(독자가 오해하거나 문장이 성립하지 않는 수준), △=다듬으면 좋은 부분, √=특별한 문제 없음"
TONE_RULES = (
    "어조: 함께 글을 다듬는 동료의 시선으로 합니다체와 제안형('~해 보면 어떨까요', '~로 읽힙니다')을 씁니다. "
    "'잘했다', '못했다', '더 노력해야' 같은 평가어와 '~하세요', '~해야 합니다' 같은 지시·명령형 어미는 쓰지 않습니다. "
    "점수·숫자 평가도 쓰지 않습니다."
)
ITEM_FOCUS = {
    "문법": "맞춤법, 조사·어미 호응, 문장 성분 호응, 비문",
    "논리": "주장과 근거의 연결, 비약, 중복, 문단 간 흐름, 과장된 일반화",
    "톤": "작성 의도에 적힌 문체·격식·독자 눈높이와 어긋나는 부분",
    "근거": "주장을 뒷받침하는 구체적 예시·수치·출처의 유무, 근거 없는 단정",
}


# ---------------------------------------------------------------- LLM
def llm(prompt: str) -> str:
    last = None
    for _ in range(LLM_ATTEMPTS):
        try:
            r = subprocess.run(["claude", "-p", "--model", "sonnet"], input=prompt,
                               capture_output=True, text=True, timeout=LLM_TIMEOUT)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
            last = f"rc={r.returncode} {r.stderr.strip()[:200]}"
        except Exception as e:
            last = repr(e)
    raise RuntimeError(f"LLM 호출 실패: {last}")


def llm_json(prompt: str) -> dict:
    last = None
    for _ in range(LLM_ATTEMPTS):
        out = llm(prompt + "\n\n도구는 쓰지 말고, 설명 없이 JSON 객체 하나만 출력하세요.")
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            last = "JSON 없음"
            continue
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            last = e
    raise RuntimeError(f"JSON 파싱 실패: {last}")


# ---------------------------------------------------------------- helpers
def one_line(s, limit: int) -> str:
    return re.sub(r"\s+", " ", str(s)).strip()[:limit]


def no_brackets(s) -> str:
    return str(s).replace("「", "").replace("」", "")


def as_index(v, n):
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i if 0 <= i < n else None


def numbered(sentences) -> str:
    return "\n".join(f"[{i}] {s}" for i, s in enumerate(sentences))


# ---------------------------------------------------------------- step 1
def split_sentences(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out += [s.strip() for s in re.split(r"(?<=[.?!])\s+", line) if s.strip()]
    return out


def parse_intent(text: str) -> dict:
    intent, n = {}, 0
    for part in re.split(r"\n|\s/\s", text):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"([^:：]{1,20})[:：]\s*(.+)", part, re.S)
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


# ---------------------------------------------------------------- step 2
def diagnose_item(item: str, ctx: dict) -> dict:
    s = ctx["sentences"]
    d = llm_json(f"""당신은 글쓰기 첨삭을 돕는 동료 편집자입니다. 아래 글을 '{item}' 관점({ITEM_FOCUS[item]})으로만 점검하세요.
{TONE_RULES}

[작성 의도]
{json.dumps(ctx['intent'], ensure_ascii=False)}

[본문 문장 (인덱스: 문장)]
{numbered(s)}

등급 기준: {GRADE_DEF}.
JSON 형식:
{{"grade": "★|△|√", "comment": "한 줄 코멘트(120자 이내, 줄바꿈 없음, 합니다체)", "evidence_index": 근거가 된 대표 문장 인덱스(정수), "quote": "그 문장 안에서 그대로 복사한 인용(80자 이내, 말줄임표 금지)", "issue_indices": [★/△일 때 문제 문장 인덱스 최대 2개, √이면 빈 배열]}}""")
    grade = d.get("grade")
    if grade not in GRADES:
        raise RuntimeError(f"등급 오류: {grade!r}")
    issues = [i for i in (as_index(x, len(s)) for x in d.get("issue_indices", [])) if i is not None][:2]
    ei = as_index(d.get("evidence_index"), len(s))
    if ei is None:
        ei = issues[0] if issues else 0
    q = no_brackets(d.get("quote", "")).strip()
    if not q or q not in s[ei]:
        q = s[ei][:QUOTE_MAX]
    q = q[:QUOTE_MAX]
    base = one_line(no_brackets(d.get("comment", "")), COMMENT_MAX)
    comment = base.replace(q, f"「{q}」", 1) if q in base else f"{base} (「{q}」)"
    if len(comment) > COMMENT_MAX:
        q = q[:40]
        comment = f"{base[:max(0, COMMENT_MAX - len(q) - 5)].rstrip()} (「{q}」)"
    return {"grade": grade, "comment": comment, "evidence_index": ei,
            "issue_indices": issues if grade != "√" else []}


def step2_diagnose(ctx: dict, log: list) -> dict:
    def run(item):
        try:
            return item, diagnose_item(item, ctx)
        except Exception as e:
            log.append(f"step2 {item} 실패: {e}")
            return item, None
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return {k: v for k, v in ex.map(run, ITEMS) if v}


# ---------------------------------------------------------------- step 3
def step3_select(ctx: dict, diag: dict, log: list) -> list[int]:
    picked: list[int] = []
    for want in ("★", "△"):
        for item in ITEMS:
            d = diag.get(item)
            if d and d["grade"] == want:
                picked += [i for i in d["issue_indices"] if i not in picked]
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
    d = llm_json(f"""당신은 함께 글을 다듬는 동료 편집자입니다. 아래 [{idx}]번 문장의 대안을 제안하세요.
{TONE_RULES}

[작성 의도]
{json.dumps(ctx['intent'], ensure_ascii=False)}

[앞뒤 맥락]
{near}

[대상 문장] {s[idx]}
[관련 진단] {' / '.join(notes) or '없음'}

대안은 1~2개. 첫 번째는 의미를 유지하며 문제만 고친 안, 두 번째는 필요할 때만 구조를 바꾼 안입니다. 작성 의도의 문체를 지키세요.
JSON 형식: {{"alternatives": ["대안1", "대안2(선택)"], "reason": "한 줄 이유(80자 이내, 줄바꿈 없음)"}}""")
    alts = [one_line(no_brackets(a), 200) for a in d.get("alternatives", []) if str(a).strip()][:2]
    reason = one_line(no_brackets(d.get("reason", "")), REASON_MAX)
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
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return [r for r in ex.map(run, indices) if r]


# ---------------------------------------------------------------- step 5
def make_summary(diag: dict, revs: list[dict], log: list) -> str:
    try:
        d = llm_json(f"""아래 첨삭 결과를 보고 종합 단락의 두 줄을 쓰세요.
{TONE_RULES}
진단: {json.dumps({k: [v['grade'], v['comment']] for k, v in diag.items()}, ensure_ascii=False)}
수정안 이유: {json.dumps([r['reason'] for r in revs], ensure_ascii=False)}
JSON 형식: {{"feeling": "글을 읽고 든 느낌 1줄(50자 이내)", "point": "핵심 개선 포인트 1줄(70자 이내)"}}""")
        text = f"{one_line(no_brackets(d['feeling']), 70)} {one_line(no_brackets(d['point']), 90)}"
        if len(text) <= SUMMARY_MAX:
            return text
        log.append(f"step5 종합 {len(text)}자 초과 -> 고정 문구")
    except Exception as e:
        log.append(f"step5 종합 생성 실패 -> 고정 문구: {e}")
    return FALLBACK_SUMMARY


def para(text: str, style: str | None = None) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def write_docx(path: Path, paragraphs: list[tuple[str, str | None]]):
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(para(t, s) for t, s in paragraphs)
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {ns}><w:body>{body}'
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" '
                'w:left="1134" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr></w:body></w:document>')
    font = '<w:rFonts w:ascii="맑은 고딕" w:hAnsi="맑은 고딕" w:eastAsia="맑은 고딕"/>'
    styles = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {ns}>
<w:docDefaults><w:rPrDefault><w:rPr>{font}<w:sz w:val="21"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="80" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="30"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="200" w:after="80"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>
</w:styles>'''
    xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    ct = (f'{xml}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>')
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    rels = (f'{xml}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="word/document.xml"/></Relationships>')
    drels = (f'{xml}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             f'<Relationship Id="rId1" Type="{rel}/styles" Target="styles.xml"/></Relationships>')
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
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DIAG_RE = re.compile(r"^(문법|논리|톤|근거) — ([★△√]) — (.+)$")
REV_RE = re.compile(r"^\d+\. 「(.+?)」 → 대안: (.+?) → 이유: (.+)$")
QUOTE_RE = re.compile(r"「(.+?)」")


def read_docx(path: Path) -> list[tuple[str, str]]:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    out = []
    for p in root.iter(f"{W}p"):
        st = p.find(f"{W}pPr/{W}pStyle")
        out.append((st.get(f"{W}val") if st is not None else "", "".join(t.text or "" for t in p.iter(f"{W}t"))))
    return out


def verify(docx_path: Path, draft_text: str) -> dict:
    det, inf = [], []

    def add(lst, rid, name, ok, comment):
        lst.append({"rule": rid, "name": name, "result": "pass" if ok else "fail", "comment": comment})

    head, secs, cur = [], {}, None
    for style, text in read_docx(docx_path):
        if style == "Heading2":
            cur = text.strip()
            secs[cur] = []
        elif cur is None:
            head.append((style, text))
        else:
            secs[cur].append(text)

    h1 = [t for s, t in head if s == "Heading1"]
    missing = [x for x in SECTIONS if x not in secs]
    ok1 = len(h1) == 1 and h1[0].startswith("글쓰기 첨삭") and not missing
    add(det, 1, "4개 섹션 존재", ok1, "모두 존재" if ok1 else f"제목 {h1}, 누락 섹션 {missing}")

    lines = [t for t in secs.get("전반 진단", []) if t.strip()]
    ms = [DIAG_RE.match(t) for t in lines]
    ok2 = len(lines) == 4 and all(ms) and sorted(m.group(1) for m in ms if m) == sorted(ITEMS)
    add(det, 2, "진단 4항목+등급", ok2, "4항목 모두 등급 기호 있음" if ok2 else f"줄 수 {len(lines)}, 형식 불일치 {sum(1 for m in ms if not m)}")

    revs = [t for t in secs.get("구간별 수정안", []) if t.strip()]
    parsed = [REV_RE.match(t) for t in revs]
    ok3 = 3 <= len(revs) <= 5 and all(parsed)
    add(det, 3, "수정안 3~5개·세 부분", ok3, f"{len(revs)}개, 모두 형식 충족" if ok3 else f"{len(revs)}개, 형식 미충족 {sum(1 for p in parsed if not p)}개")

    ok4 = any(t.startswith("이 첨삭은 최종 결정이 아니며") for _, t in head)
    add(det, 4, "헤더 면책 문구", ok4, "있음" if ok4 else "헤더 섹션에 면책 문구 없음")

    if any(d["result"] == "fail" for d in det):
        for rid, name in ((5, "인용문 원문 일치"), (6, "등급-코멘트 일관성"), (7, "어조")):
            inf.append({"rule": rid, "name": name, "result": "skipped", "comment": "결정론 규칙 실패로 실행하지 않음"})
        return {"result": "fail", "deterministic": det, "inference": inf}

    diag_text, rev_text = "\n".join(lines), "\n".join(revs)

    # 5. 인용문 일치: 「」로 감싼 문자열 전부 + 기계 검사
    quotes = QUOTE_RE.findall(diag_text) + QUOTE_RE.findall(rev_text)
    expected = len(ITEMS) + len(revs)
    mism = [q for q in quotes if q not in draft_text]
    try:
        d = llm_json(f"""아래 인용문들이 원문(draft.md)에 공백·구두점까지 정확히 일치하는 형태로 존재하는지 판정하세요.
기계 검사(부분 문자열 일치) 결과 불일치 목록: {json.dumps(mism, ensure_ascii=False)}
인용문 전체: {json.dumps(quotes, ensure_ascii=False)}
[원문]
{draft_text}
JSON 형식: {{"pass": true|false, "comment": "한 줄 근거"}}""")
        ok5 = bool(d.get("pass")) and not mism and len(quotes) == expected
        c5 = f"{d.get('comment', '')} / 검사된 인용 {len(quotes)}개(기대 {expected}개)"
        if mism:
            c5 += f" / 기계 검사 불일치: {mism}"
    except Exception as e:
        ok5, c5 = False, f"판정 불가: {e}"
    add(inf, 5, "인용문 원문 일치", ok5, c5)

    # 6. 등급-코멘트 일관성
    try:
        d = llm_json(f"""등급 기호의 의미: {GRADE_DEF}.
아래 4항목 진단에서 등급과 코멘트 내용이 서로 일관되는지 판정하세요. 예: 코멘트가 '문제가 많다'는 취지인데 √이면 불일치.
4항목의 등급이 모두 같다면 각 코멘트가 그 등급의 정의를 충족하는지 항목별로 확인하세요.
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
- 코멘트·수정안에 쓰인 '~하세요', '~해야 합니다' 같은 지시·명령형 어미, 선생이 학생에게 말하는 듯한 훈계·칭찬·꾸짖음
단, 작성 의도에 적힌 문체 기준을 설명하는 표현은 예외로 봅니다.
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
    pat = re.compile(rf"^(?:review|run)_{re.escape(day)}_v2_(\d+)\.(?:docx|json)$")
    nums = [int(m.group(1)) for d in (ROOT / "outputs", ROOT / "runs") if d.is_dir()
            for f in d.iterdir() if (m := pat.match(f.name))]
    return max(nums, default=0) + 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=_date.today().isoformat())
    ap.add_argument("--inputs", default=str(ROOT / "inputs"))
    a = ap.parse_args()
    day, seq = a.date, f"{next_seq(a.date):02d}"
    out_docx = ROOT / "outputs" / f"review_{day}_v2_{seq}.docx"
    out_run = ROOT / "runs" / f"run_{day}_v2_{seq}.json"
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
