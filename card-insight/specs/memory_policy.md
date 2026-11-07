# Memory Policy

### 카드 분석 메모리
- memory/card/long_term/monthly_summary.json - 월별 카테고리 합계·평균 누적 (전월·전분기 비교 기준)
- memory/card/long_term/merchant_category_map.json → 가맹점 → 카테고리 매핑 사전 (신뢰도 0.9 이상 자동 확장 저장, 0.9 미만은 "후보"(confirmed=false) 플래그로 저장)
- memory/card/working/this_month_anonymized.json - 이번 달 익명화 트랜젝션 캐시 (처리 후 7일 자동 삭제)
- 새 가맹점이 등장하면: 신뢰도 0.9 이상이면 사용자 확인 없이 자동으로 매핑 사전에 영구 추가, 0.9 미만이면 추천 분류를 "후보"(confirmed=false)로 표시해 저장(이번 분석에는 적용). 어떤 경우에도 1/2/3 선택 메뉴로 사용자에게 묻지 않는다(블로킹 질문 금지).
