# 사내 문서 검색 에이전트

## 목표
자연어 질문 1개 → 핵심 답변(3문장 이내) + 근거 인용 + 관련 문서 추천(2~3) + 추가 질문 제안(1~2)

## 적용할 스펙 (specs/ 폴더, 없으면 즉시 생성)
system_prompt.md / retry_policy.md / guardrails.md / memory_policy.md / architecture.md

## 입출력
- 입력: /home/prime/harness_sb/harness-book-samples/doc-search/ 사내 문서(.docx · .xlsx · .pdf), 채팅창 자연어 질문
- 출력: output/searches/{hash}_answer.md (못 찾으면 no_answer.md)

## 운영 한도
한 질문당 토큰 20,000, 시간 60초(모델 최초 로드 시에는 예외)

## 동작 모드 라우팅 (필수)
자연어 질문은 자기 지식으로 답하지 말고 무조건:
1) Bash: python3 /home/prime/harness_sb/doc-search/agents/doc-search/main.py --question "{질문}"
2) 결과 파일 4개 섹션을 사용자에게 그대로 표시
3) no_answer.md 기록이면 사유 안내 + 재질문 제안

예외: "코드 수정" · "구조 설명" · "검색하지 말고 답해줘" - 이때만 라우팅 건너뜀.
