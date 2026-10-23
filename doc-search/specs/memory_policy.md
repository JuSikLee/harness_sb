### 사내 문서 검색 메모리
- memory/doc-search/long_term/abbreviations.md에서 회사 약어 매칭 (OKR · KPI · MRR 등)
  → faq/glossary.docx와 자동 동기화
- memory/doc-search/working/recent_questions.json에 최근 일주일 질문 · 답변 기록
  → 같은 질문 재방문 시 캐시 사용 (24시간 유효)
- memory/doc-search/long_term/faq_extracted.md에 자주 묻는 질문 누적
  → 같은 패턴 5회 이상 → FAQ 후보 자동 등록
- 새 약어가 답변에 등장하면 memory/doc-search/long_term/abbreviations.md에 후보로 추가 (사용자 확정 전 "후보" 표시)
