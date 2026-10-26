# specs/ · instruction.md 규칙 ↔ 코드 반영 비교

기준: `agents/doc-search/` 의 현재 코드 (main.py · answerer.py · validator.py · searcher.py · loader.py). 코드는 수정하지 않고 읽기만 했다.
`searcher.py`의 키워드 매칭 검사(architecture 8-4)만 grep으로 직접 실행했으며 결과는 0줄이었다.

## 1. system_prompt.md

| 규칙 | 반영 파일 | 함수/위치 | 반영 방식 |
|---|---|---|---|
| 5년 차 운영 매니저 정체성 | answerer.py | `build_system()` | 스펙 파일 전체를 system 프롬프트로 그대로 주입 |
| 모든 답변에 `[문서명 · 섹션]` 출처 | answerer.py, validator.py | `FORMAT`, `validate()` | 프롬프트로 지시하고, 검증에서 `[` 유무와 citations 비어 있음을 검사 |
| 답이 없으면 "사내 문서에는 정보 없음" | answerer.py, main.py | `FORMAT`, `_run()` 147·162행 | 프롬프트로 지시한 뒤, 해당 문구를 감지하면 외부 검색 단계로 분기 |
| 추측 금지(상식 답변 금지) | answerer.py, validator.py | `FORMAT`, `validate()` | 프롬프트로 지시하고, 인용문이 실제 본문에 있는지로 간접 검사 |
| 가장 구체적인 문서 우선 | searcher.py | `rerank()` | rerank가 관련도 순으로 고르는 것에 의존하며 "구체성"을 따로 검사하지는 않음 |
| 3문장 이내 + 근거 | answerer.py | `FORMAT`, `build_system()` | 프롬프트 지시만 있음 |
| 초과 시 스스로 줄여 재작성 | answerer.py | `build_system()` | 프롬프트 지시만 있음 |

## 2. retry_policy.md

| 규칙 | 반영 파일 | 함수/위치 | 반영 방식 |
|---|---|---|---|
| 출처 모두 표시 | validator.py | `validate()` | citations 비어 있음 또는 answer에 `[` 없음이면 issue로 처리 |
| 모든 사실이 문서에 있는지 | validator.py | `validate()` | 각 인용문을 공백 제거 후 hits 본문과 부분 문자열 비교 |
| "정보 없음"이면 검색 폴더 명시 | validator.py, main.py | `validate()`, `SEARCHED` | `searched_folder`가 비어 있는지만 확인 |
| 출처 4개 이상이면 재정리 | validator.py | `validate()` | 서로 다른 문서가 4개 이상이면 issue로 처리 |
| 3문장 초과 시 재작성 | 없음 | — | 미반영 (7장 참고) |
| 진단 메시지 형식 | validator.py | `diagnostic()` | `[이전 시도 검증 실패]` 형식으로 만들어 다음 시도의 프롬프트 앞에 붙임 |
| (암묵) 재시도 | main.py | `_run()` 150–159행, `MAX_RETRY=2` | 검증을 통과할 때까지 최대 3회 생성하고, 토큰이나 시간이 넘으면 중단 |

## 3. guardrails.md

| 규칙 | 반영 파일 | 함수/위치 | 반영 방식 |
|---|---|---|---|
| C1·C2 분류는 검색·임베딩 이전 | main.py | `_run()` 133행 | 캐시 확인과 `searcher.search()` 앞에서 `classify()` 호출 |
| C1은 즉시 거부(검색·rerank·LLM 호출 금지) | main.py, answerer.py | `_run()` 134–136행, `C1_REFUSAL` | 거부 메시지 파일만 쓰고 바로 반환 |
| C2는 검색 진행 후 [프로젝트 X] 마스킹 | answerer.py, main.py | `mask_c2()`, `render()`, `external_fallback()` | 출력 직전에 정규식으로 치환 |
| C1+C2 동시면 C1 우선 | answerer.py | `classify()` | C1을 먼저 검사해서 C1이면 바로 반환 |
| C1 키워드, C2 키워드·정규식 | answerer.py | `_C1`, `_C2`, `_C2_RE` | 입력 질문에 대한 부분 문자열 검사와 정규식 매칭 |
| 거부 메시지 형식 | answerer.py | `C1_REFUSAL` | 스펙 문구 그대로 상수로 둠 |
| C1 원문을 로그·캐시에 저장 안 함 | main.py | `write_no_answer(refusal_only=True)`, `qhash()` | 파일명은 해시이고 본문은 거부 문구뿐이며, recent_questions에도 기록하지 않음 |

## 4. memory_policy.md

| 규칙 | 반영 파일 | 함수/위치 | 반영 방식 |
|---|---|---|---|
| 24시간 캐시 | main.py | `_run()` 139–143행 | recent의 time이 24시간 이내이고 answer 파일이 있으면 캐시 반환 |
| 최근 일주일 기록 | main.py | `load_recent()`, `save_recent()`, `track_memory()` | 7일 지난 항목은 읽을 때 걸러내고, question, time, count를 JSON에 저장 |
| 5회 이상이면 FAQ 후보 등록 | main.py | `track_memory()` | 같은 해시의 count가 5 이상이면 `faq_extracted.md`에 한 줄 추가 |
| 새 약어는 "(후보)"로 추가 | main.py | `track_memory()` | 답변에서 `[A-Z]{2,6}`을 뽑아 `abbreviations.md`에 없으면 `(후보)`로 추가 |
| 약어 매칭, glossary 자동 동기화 | 없음 | — | 미반영 (7장 참고) |

## 5. architecture.md

| 규칙 | 반영 파일 | 함수/위치 | 반영 방식 |
|---|---|---|---|
| §1 임베딩 검색만, 키워드 매칭 금지 | searcher.py | `search()` | `cosine_similarity` 한 가지만 사용하고, 금지 패턴 grep은 0줄 |
| §2 MiniLM 기본, voyage 옵션 | searcher.py | `backend()`, `embed()`, `MINILM`, `VOYAGE` | `EMBEDDING_BACKEND` 환경변수로 선택 |
| §3 `.cache/vectors`, npy+json, SHA256 | searcher.py, loader.py | `build_index()`, `corpus_hash()` | 파일별 SHA256을 합친 코퍼스 해시가 같으면 캐시를 재사용 |
| §4 표를 행 단위 청크로 만들고 헤더를 앞에 붙임 | loader.py | `Collector.add_table()` | 헤더가 `헤더: 값` 쌍으로 각 행에 붙음 |
| §4 600자 초과 시 300자 이하로 재분할 | loader.py | `split_text()`, `MAX_SECTION`, `CHUNK_LIMIT` | 문장 경계로 자르고, 경계가 없는 긴 문장은 300자에서 강제 분할 |
| §4 헤딩 경로 prefix | loader.py | `Collector._make()` | `doc > h1 > h2\n본문`을 임베딩 텍스트로 사용 |
| §4 포맷별 처리 | loader.py | `load_docx()`, `load_xlsx()`, `load_pdf()` | docx는 본문 순서대로 표를 처리, xlsx는 첫 다중 셀 행을 헤더로, pdf는 `find_tables()`를 먼저 처리 |
| §5 top-12, rerank 3~5, 임계값, 환경변수 | searcher.py | `TOP_K`, `FINAL_MIN`/`FINAL_MAX`, `threshold()`, `rerank()` | MiniLM 0.25, voyage 0.4, `SIMILARITY_THRESHOLD`로 오버라이드 |
| §6 의존성 명시 | requirements.txt | — | 필수 패키지와 voyageai가 모두 들어 있음 |
| §7 `ANTHROPIC_MODEL` | searcher.py, answerer.py | `rerank()`, `generate()`, `generate_external()` | 세 호출 모두 `os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")` |
| §8 자체 검증 체크리스트 | 없음(프로세스 규칙) | — | 코드에는 없고, grep 항목(8-4)만 실행해 0줄 확인 |

## 6. instruction.md (CLAUDE.md와 같은 내용)

| 규칙 | 반영 파일 | 함수/위치 | 반영 방식 |
|---|---|---|---|
| 출력 4개 섹션 | main.py | `render()` | 핵심 답변, 근거 인용, 관련 문서, 추가 질문 순으로 조립 |
| 관련 문서 2~3개, 추가 질문 1~2개 | answerer.py | `FORMAT` | 프롬프트 지시만 있음 |
| 입력 폴더 | loader.py | `DOC_ROOT` | 지정 경로의 `.docx`, `.xlsx`, `.pdf`를 재귀 탐색 |
| 출력 `{hash}_answer.md` / `no_answer.md` | main.py | `OUT_DIR`, `write_no_answer()`, `qhash()` | sha256 앞 12자를 파일명에 사용 |
| 토큰 20,000 / 60초 | main.py | `TOKEN_LIMIT`, `_run()` 157행 | 재시도 루프 안에서만 확인 |
| 동작 모드 라우팅, specs/ 없으면 생성 | CLAUDE.md | — | 코드가 아니라 에이전트 운영 지침이며 `main.py`에는 없음 |

## 7. 미반영 / 일부만 반영된 규칙

| 규칙 | 상태 | 실제 동작과 차이 |
|---|---|---|
| 3문장 초과 시 재작성 (system_prompt, retry_policy) | 미반영 | `validate()`에 문장 수 검사가 없어서, 모델이 4문장 이상 내놓아도 재시도가 일어나지 않음 |
| 관련 문서 2~3개, 추가 질문 1~2개 | 일부 | 프롬프트로만 지시하고 검증은 없음. 같은 문서가 관련 문서에 두 번 나온 적이 있음 |
| 출처 문서 개수 기준 | 불일치 | 스펙은 "핵심 1~2개", 검증은 4개 이상일 때만 issue, 진단과 프롬프트는 "1~3개"라고 안내함 |
| "정보 없음" 시 검색 폴더 명시 검증 | 일부 | `SEARCHED`가 항상 값이 있는 상수라 이 검사는 사실상 항상 통과함 |
| 모든 사실이 문서에 있는지 | 일부 | 인용문(quote)만 비교하고, 답변 문장 자체는 검증하지 않음 |
| C2 마스킹 범위 | 일부 | 프로젝트명 정규식 패턴만 치환함. "고객 데이터"나 코드네임 키워드로 걸린 질문은 분류만 되고 마스킹할 문자열이 없을 수 있음 |
| C1·C2 정의를 glossary.docx에서 참조 | 미반영 | 키워드가 `answerer.py`에 하드코딩돼 있고 glossary는 읽지 않음 |
| C2 질문의 외부 검색 | 가드레일 공백 | 외부 검색 단계에서 C2 질문 원문을 마스킹 없이 Tavily로 보내고, 마스킹은 결과 출력에만 적용됨 (C1은 앞에서 차단됨) |
| 약어 매칭과 glossary 자동 동기화 | 미반영 | 새 약어를 후보로 추가하는 것만 있고, 질문에 약어 사전을 적용하거나 glossary와 동기화하지 않음 |
| "같은 패턴" 5회 이상 | 일부 | 질문 문자열이 정확히 같을 때(해시 일치)만 세고, 비슷한 질문은 합산하지 않음 |
| recent_questions에 질문과 답변 기록 | 일부 | 질문, 시각, 횟수만 저장하고 답변 본문은 output 파일에 있음. 외부 답변과 no_answer는 기록하지 않음 |
| 토큰 한도 20,000 | 일부 | rerank 호출과 외부 답변 생성의 토큰은 합산하지 않으며, 한도는 재시도 여부를 정할 때만 쓰임 |
| 시간 한도 60초 | 일부 | 재시도 사이에만 확인하고 하드 타임아웃이 없음. "모델 최초 로드 예외"도 코드로 구분하지 않음 |
| architecture §8 체크리스트 3(섹션 ≥100)과 5(MRR 질문) | 코드 없음 | 검증 절차일 뿐이고 이번에 다시 실행하지는 않음 |
| 새로 추가한 `requests` 의존성 | 누락 | `main.py`가 import하지만 `requirements.txt`에는 없음 |
