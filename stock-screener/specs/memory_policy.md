# Memory Policy

### 종목 추천 메모리
- memory/stock/long_term/recommendation_history.json - 추천 일자·종목·이유·1주/1개월 후 실제 등락
  → 같은 종목 재추천 시 과거 적중률 자동 첨부
- memory/stock/long_term/sector_trends.md - 산업 트렌드 누적 (AI·반도체·금리 등)
  → 같은 트렌드 5회 이상 등장 → "지속 트렌드" 후보 자동 등록
- memory/stock/working/recent_news_cache.json - 최근 24시간 뉴스 캐시 (중복 호출 방지)
