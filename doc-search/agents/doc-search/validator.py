"""답변 검증(retry_policy). 분류/마스킹은 answerer.py."""
import re

def validate(result, hits, searched_folder):
    """어긋난 항목 리스트 반환(비어 있으면 통과)."""
    issues = []
    answer = result.get("answer", "")
    cites = result.get("citations", [])
    no_info = "정보 없음" in answer
    if no_info:
        if not searched_folder:
            issues.append("'정보 없음' 응답에 검색한 폴더가 명시되지 않음")
        return issues
    if not cites or "[" not in answer:
        issues.append("출처 표시 누락 (답변에 [문서명 · 섹션] 없음)")
    corpus = "\n".join(h["body"] for h in hits)
    norm = lambda s: re.sub(r"\s+", "", s)
    for c in cites:
        if norm(c.get("quote", "")) not in norm(corpus):
            issues.append(f"인용문이 문서에 없음(할루시네이션 의심): {c.get('quote', '')[:40]}")
    if len({c.get("doc") for c in cites}) >= 4:
        issues.append("출처 문서 4개 이상 인용 → 핵심 1~2개로 재정리 필요")
    return issues


def diagnostic(question, issues):
    lines = "\n".join(f"- {i}" for i in issues)
    return (
        f"[이전 시도 검증 실패]\n질문: {question}\n어긋난 항목:\n{lines}\n\n"
        "다음 사항을 반드시 반영:\n- 답변마다 [문서명 · 섹션] 형식으로 출처 표시\n"
        "- 인용문은 제공된 섹션 본문에서 그대로 가져올 것\n- 인용 문서는 핵심 1~3개만"
    )
