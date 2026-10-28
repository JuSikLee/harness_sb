# 주식 종목 추천 에이전트 - 기술 아키텍쳐 (필수 규칙)

## 1. 데이터 소스 (절대 규칙)
- 주가/거래량/시가총액: yfinance 라이브러리만 사용 (한국 .KS/.KQ + 미국 NYSE/NASDAQ 모두 지원)
- 뉴스: Tavily API (TAVILY_API_KEY 환경변수) - 종목 통화에 따라 검색 언어 자동 전환 (KRW → 한국어, USD → 영어)
- 공시 보조: 한국은 DART, 미국은 SEC EDGAR
- 어떤 증권사 API(키움 OpenAPI·KIS·Creon·NH·IBKR·TD 등)도 호출 금지

## 2. 1차 후보 추출 (candidate_filter.py)
- 유니버스: KOSPI200 + KOSDAQ150 + S&P500 약 850종목 (pykrx + wikipedia 동적 로드, 실패시 한국 30 + 미국 30 fallback 리스트)
- 유니버스 캐싱: data/cache/universe_{YYYYMMDD}.json (같은 날 재사용)
- 최근 5일 상승률 ≥ 5% AND 거래량 5일 평균 대비 ≥ 1.5배
- 시가총액 조회: 한국(.KS/.KQ)은 pykrx 시총을 우선 사용(유니버스 구축 시 받은 값 재사용), 미국은 yfinance(fast_info → 실패 시 info 보조). 실패 시 1회 재시도.
- 시총 미확정(N/A)은 fail-closed: 시총을 확정하지 못한 종목은 시총 필터를 "통과"시키지 않고 후보에서 제외한다(시총을 모르면 임계값 충족 여부를 알 수 없으므로). 'if mcap > 0 and mcap < floor' 처럼 미확정=통과시키는 fail-open 금지.
- 결과적으로 시총이 N/A인 종목이 추천 목록 · 보고서에 남아서는 안 된다.
- 통화 판별: 종목 접미사(.KS/.KQ → KRW, 그 외 → USD)
- 임계값 환경변수: MIN_RETURN_5D(기본 0.05), MIN_VOLUME_RATIO(기본 1.5), MIN_MCAP_KRW(기본 1e11), MIN_MCAP_USD(기본 5e8)
- 시장 다양성: 한국 ≤ 60% AND 미국 ≤ 60% (한쪽 쏠림 방지)
- 상위 20개 추출

## 3. 뉴스·이벤트 분석 (news_analyzer.py)
- 각 종목 최근 5일 뉴스를 Tavily로 수집 (한국 종목은 한국어 + 종목명, 미국 종목은 영어 + ticker 회사명)
- 클로드 API로 이벤트 분류 (실적 · 계약 · 정책 · M&A · 기타)
- 일회성 vs 구조적 분류 (Claude API rerank)

## 4. 산업 트렌드 매칭 (trend_classifier.py)
- 종목명 · 뉴스 본문을 임베딩 후 사전 정의 트렌드 카테고리(AI · 반도체 · EV 2차전지 · 금리 · 바이오 · 방산 · 클라우드 · 핀테크)와 코사인 유사도
- 임계값 0.4 이상이면 1차 매칭
- 섹터 게이트 (필수): 트렌드는 "뉴스가 그 주제를 언급한가"가 아니라 "회사의 실제 업종이 그 테마다"를 기준으로 확정한다.
  - 각 종목의 실제 업종을 yfinance(Ticker.info의 sector · industry)로 조회한다(종목당 1회, 캐시 권장)
  - 텍스트 유사도가 임계값을 넘어도, 회사의 실제 섹터가 테마와 맞지 않으면 그 트렌드는 부여하지 않는다:
    - AI · 반도체 · 클라우드 → 섹터가 Technology / Semiconductors / IT 일 때만 인정
    - EV 2차전지 → Technology / 자동차 · 부품 / 화학 · 소재
    - 바이오 → Healthcare
    - 방산 → Industrials / Aerospace & Defense
    - 핀테크 → 결제 · 핀테크가 실제 주력 사업일 때만 (단순 은행 · 지주 · 보험 제외)
    - 금리 → 매크로 테마이므로 섹터 게이트 예외(모든 섹터 허용)
  - Financial Services(은행 · 지주 · 보험) 종목은 반도체 · AI를 매크로 재료로 언급하더라도 AI · 반도체 · 클라우드 테마로 분류하지 않는다.
  - 섹터 조회 실패 시: 해당 종목은 텍스트 매칭만으로 테마를 확정하지 말고 "트렌드 미확정(섹터 미확인)"으로 표시한다.

## 5. 지속 가능성 검증 (sustainability_validator.py)
- retry_policy.md 검증 항목 코드화
- 기준: 구조적 이벤트 OR 산업 트렌드 매칭 OR 기관/외국인 순매수 (한국은 DART, 미국은 SEC EDGAR 13F 보조)
- 위 3가지 중 0개면 추천 후보에서 제외

## 6. 출력 (report_writer.py)
- output/recommendations/{YYYYMMDD}_top10_{NN}.docx
- python-docx 사용. 표 형식 + 면책 문구 첫 · 마지막 단락 (한국어 + 영어 병기)
- 각 종목: 종목명 · 시장(KOSPI/KOSDAQ/NYSE/NASDAQ) · 통화(KRW/USD) · 시총 · 상승률 · 상승 이유 · 산업 트렌드 · 하방 리스크 · 추천 등급

## 7. 의존성 (필수)
- pip install yfinance tavily-python pandas python-docx anthropic scikit-learn sentence-transformers pykrx beautifulsoup4

## 8. 자체 검증 체크리스트 (6개 .py 작성 직후 반드시 통과)
1) pip list에 yfinance, tavily-python, python-docx, pykrx 모두 존재
2) grep -E "키움|Creon|KIS|증권사|smtplib|order_buy|order_sell" agents/stock-screener/*.py 결과 0줄 (자동 거래 코드 없음). 이 검사는 클로드 코드가 직접 실행합니다. Terminal 또는 PowerShell에 그대로 붙여 넣지 마세요.
3) python main.py --request "오늘 추천 10개" 실행 시 output/recommendations/*.docx 정상 생성
4) 생성된 .docx 첫 줄 · 마지막 줄에 면책 문구(한 · 영) 포함 확인
5) python main.py --request "삼성전자 자동 매수해줘" 실행 시 가드레일 차단 (거부 메시지 출력)
6) 추천 결과에 한국 · 미국 종목이 모두 1개 이상 포함 (시장 다양성)
7) 통화 표시(KRW/USD)가 종목별로 정확히 매칭 (혼동 없음)

7개 중 하나라도 실패하면 해당 코드 즉시 재작성. 모두 통과해야 다음 단계 진행.
