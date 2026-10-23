# 사내 문서 검색 에이전트 - 기술 아키텍처 (필수 규약)

## 1. 검색 방식 (절대 규칙)
- 벡터 임베딩 기반 의미 검색만 사용. 키워드/정규식 매칭 금지.
- searcher.py 코드에 re.compile, str.split, str.count, str.find 등 키워드/정규식 매칭 코드 금지.
- 모든 매칭은 cosine_similarity(query_embedding, section_embedding) 한 가지 방식뿐.

## 2. 임베딩 모델
- 기본: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
- 옵션: 환경변수 EMBEDDING_BACKEND=voyage 시 voyage-multilingual-2 사용
- 출력 차원: 384(MiniLM) 또는 1024(voyage)

## 3. 인덱스 저장
- 위치: /home/prime/harness_sb/doc-search/.cache/vectors/
- 형식: numpy .npy(임베딩) + .json(섹션 메타)
- 캐시 무효화: 파일 SHA256 해시(mtime보다 안전)

## 4. 청킹 규칙 (loader.py)
- 모든 포맷의 표(table)는 반드시 행 단위로 별도 청크. 헤더 행을 각 데이터 행에 prepend
- 섹션이 600자 초과 시 문단/문장 경계로 ≤300자 재분할
- 모든 청크에 헤딩 경로(예: "glossary.docx > 2. 사업 · 제품 약어") prefix 포함
- .docx 표는 doc.tables, .xlsx 시트는 첫 행 헤더 처리, .pdf는 pdfplumber.extract_tables() 우선

## 5. 검색 파이프라인 (searcher.py)
- 질문 임베딩 → 코사인 유사도 top-12 → Claude API rerank → 최종 3~5개
- 임계값: MiniLM은 0.25, voyage는 0.4 미만이면 "정보 없음"
- 환경변수 SIMILARITY_THRESHOLD로 오버라이드 가능

## 6. 의존성 (필수)
- pip install sentence-transformers numpy scikit-learn anthropic python-docx openpyxl pdfplumber
- voyage 사용 시 voyageai 추가, requirements.txt에 모두 명시

## 7. LLM 호출 모델 (필수)
rerank(searcher.py)와 답변 생성(answerer.py)의 Claude 모델은 코드에서 `os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")` 형태로 읽어, 환경변수 ANTHROPIC_MODEL로 교체할 수 있게 한다.

## 8. 자체 검증 체크리스트 (5개 .py 작성 직후 반드시 통과)
1) pip list에 sentence-transformers, numpy, scikit-learn 모두 존재
2) SentenceTransformer 모델 로드 시 (1, 384) shape 출력
3) loader.py 단독 실행 결과 섹션 수 ≥ 100 (미만이면 표 청킹 실패 → loader.py 재작성)
4) `grep -E "re\.(compile|search|match|findall)|\.(split|count|find)\(" agents/doc-search/searcher.py` 결과 0줄 (한 줄이라도 나오면 키워드 매칭 → searcher.py 재작성). 이 검사는 클로드 코드가 직접 실행한다. PowerShell이나 Terminal에 그대로 붙여 넣지 말 것.
5) `python main.py --question "MRR이 뭔가요?"` 결과 답변에 "Monthly Recurring Revenue" 또는 "월간 반복 매출" 포함

5개 중 하나라도 실패하면 해당 코드 즉시 재작성. 모두 통과해야 다음 단계 진행.
