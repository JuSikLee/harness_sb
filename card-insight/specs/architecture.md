# 카드 사용 내역 분석 에이전트 - 기술 아키텍처 (필수 규칙)

## 0. 자연어 라우팅 (parse_intent - main.py)
- 입력: 사용자 자연어 한 문장 (CLI 인자 없음)
- 추출 항목:
  · 대상 월 (예: "4월", "이번 달", "지난달". 미지정 시 카드 파일에서 가장 최근 월 자동 감지)
  · 비교 기준 월 ("A월 대비 B월" → 대상 월=B, 기준 월=A. 기준 월 자료가 없으면 대체 없이 오류 안내)
  · 요청 종류 (리포트 / 전월 비교 / 카테고리 학습 / 디버그 / 단정 표현 거부)
  · 강조 토픽 (특정 카테고리 언급 시 - "카페", "외식" 등)
- 라우팅 결과를 dict로 반환 후 해당 모듈 호출
- "끊어"·"단호하게"·"강하게" 같은 어조 단어 감지 시 advisor.py의 단정 표현 차단 모드 자동 활성화

## 1. 데이터 소스 (절대 규칙)
- 입력: 로컬 CSV/Excel만 사용 (/home/prime/harness_sb/harness-book-samples/card-insight/ 샘플 또는 /home/prime/harness_sb/card-insight/ 폴더)
- 외부 데이터 호출 금지 (카드사 API·은행 OpenBanking 등 모두 차단)
- LLM 호출 시 가맹점명은 익명화 토큰(M001~)으로 치환, 금액은 1만 원 단위 반올림 후 전송 (정확한 값은 로컬에만 보관)

## 2. CSV 파싱·정규화 (csv_loader.py + merchant_normalizer.py)
- 카드사별 헤더 어댑터: 삼성·신한·현대·KB·롯데·우리·하나·NH농협 (확장 가능 dict 구조)
- 헤더가 1행이 아닐 수 있으니 첫 10줄 전수 검사로 식별
- 식별 실패 시 사용자에게 헤더 출력 후 카드사 확인 → specs/card_signatures.json에 누적
- 가맹점명 정규화: 정규식 + 사전 매칭 ("스타벅스 강남점"·"STARBUCKS#KANG" → "스타벅스")
- 매칭 실패 시 LLM(anthropic) 보조 (익명화 토큰만 전달)

## 3. 카테고리 분류 (category_classifier.py)
- 사전 정의 카테고리: 식비·카페·교통·쇼핑·의료·여가·구독·생활비·기타 (확장 가능)
- 1차: merchant_category_map.json 사전 매칭
- 2차: 매칭 실패 항목만 LLM(anthropic)을 사용하여 분류 (익명화 토큰 + 일반 카테고리 후보 제시)
- 3차: 신뢰도 < 0.4 항목은 "미분류"로 표시만 하고 진행 (실행 중 사용자에게 되묻지 않음)

## 4. 통계 계산 (statistics_engine.py - Python only)
- 카테고리별 합계·평균·표준편차
- 전월 대비 증가율 (memory/card/long_term/monthly_summary.json 기준). 기준 월이 직전 월이 아니면 "{A}월 대비"로 표기
- 자가 평균 대비 z-score (3개월 이상 누적 시)
- 과소비 Top 3 선정 기준: 전월 대비 증가율 우선 (절대 금액 아님), 최소 5만 원 이상 지출 카테고리에서만
- 절대 LLM에 산수 맡기지 않음

## 5. 절약 제안 생성 (advisor.py)
- statistics_engine 결과(요약 수치만, 가맹점명 없이) → LLM
- 출력 검증: 각 제안에 정량 근거("전월 대비 +XX%")가 1개 이상 포함됐는지
- 단정 표현 후처리 차단 ("끊어"·"사지 마" → "검토 권장")

## 6. 출력 (report_writer.py)
- output/card-report/{YYYYMMDD}_card_report_{NN}.docx
- python-docx 사용. 다음 구조:
  1) 요약 (총 사용액·평균·전월 대비)
  2) 카테고리별 표 (금액·비중·전월 대비)
  3) 주 사용처 Top 5
  4) 과소비 Top 3 (왜 과소비인지 정량 근거 포함)
  5) 절약 가능 영역 (각 영역에 줄이면 월 약 X원 절감 추정)
  6) 면책 첫·마지막 단락

## 7. 의존성 (필수)
- pip install pandas openpyxl python-docx anthropic

## 8. 자체 검증 체크리스트 (7개 .py 작성 직후 반드시 통과)
1) pip list에 pandas, openpyxl, python-docx 존재
2) grep -E "requests\.|httpx|urlopen|urllib|aiohttp|socket\.|\.port\(|\.get\(" agents/card-insight/ 결과: 외부 호출은 anthropic 라이브러리만 (그 외 0줄). 이 검사는 클로드 코드가 직접 실행합니다. PowerShell 또는 Terminal에 그대로 붙여 넣지 마세요.
3) python main.py "4월 지출 정리해줘" 실행 시 output/card-report/*.docx 정상 생성
4) 생성된 .docx 첫·마지막 줄에 면책 문구 포함, 카테고리 합계 = 총 사용액 검증
5) 앤트로픽 라이브러리 호출 로그에서 가맹점명 원문이 보이지 않는지 (익명화 토큰 M001~만 보여야 함)
6) python main.py "카페 끊으라고 강하게 말해줘" 실행 시 단정 표현 거부 ("검토 권장" 어조로 변환)
7) main.py가 CLI 인자 없이 자연어 한 문장만 받아 동작하는지 (--month 같은 인자 형태 사용 금지, request="자연어" 한 인자만 허용)

7개 중 하나라도 실패하면 해당 코드 즉시 재작성. 모두 통과해야 다음 단계 진행.
