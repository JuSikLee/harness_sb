# 리서치 에이전트 - 메모리 정책

## 1. 단기 메모리 - 한 호출의 컨텍스트
한 번의 모델 호출에 들어가는 컨텍스트를 다음 원칙으로 관리합니다.

- 시스템 프롬프트(리서치 에이전트 역할 정의)는 항상 포함
- 현재 처리 중인 자료 한 건의 본문 전체 포함
- 직전 호출의 결과는 포함하지 않음 (필요한 부분만 작업 메모리에서 발췌)
- 다른 자료의 본문은 포함하지 않음 (한 호출 = 한 자료)
- 사용자의 장기 관심 분야 요약(200자 이내)은 항상 포함
- 이전에 처리한 자료들의 핵심 키워드 목록(50개 이내)은 항상 포함

원칙: 한 호출의 컨텍스트는 가능한 한 좁게 유지하되, 누적 학습에 필요한 핵심 신호는 짧은 형태로 유지합니다.

## 2. 작업 메모리 - 한 리서치의 진행 상황
한 리서치 작업이 진행되는 동안 다음 외부 파일들에 진행 상황을 기록합니다.

### memory/research/working/{topic}/progress.json
```json
{
    "topic": "AI 에이전트의 운영 환경",
    "started_at": "2026-04-29T10:00:00",
    "last_updated": "2026-04-29T16:30:00",
    "status": "in_progress",
    "phase": "분석",
    "phases_completed": ["수집", "정렬"],
    "phases_remaining": ["분석", "요약", "정리"],
    "sources_collected": 12,
    "sources_processed": 7,
    "key_findings_so_far": ["finding 1", "finding 2"]
}
```

### memory/research/working/{topic}/sources.jsonl
수집한 자료 목록. 각 줄에 하나의 자료 정보 (URL, 제목, 요약, 처리 여부).

### memory/research/working/{topic}/notes.md
진행 중에 만들어지는 분석 노트. 사용자가 다음 세션에서 열어서 검토하기 위한 누적 메모.

### 갱신 시점
- 자료 수집: sources.jsonl에 한 줄씩 추가
- 자료 처리: progress.json의 sources_processed 증가
- 단계 전환: phase · phases_completed 갱신
- 핵심 발견: key_findings_so_far에 추가

### 재개(resume)
- 같은 주제로 다시 시작하면 progress.json을 읽고 마지막 phase에서 이어감
- 이미 처리한 자료는 다시 처리하지 않음 (sources.jsonl의 처리 여부 확인)

## 3. 장기 메모리 - 세션을 가로지르는 정보
다음 정보는 memory/research/long_term/ 폴더에 영구 보관합니다.

### memory/research/long_term/user_interests.md
사용자의 관심 분야 · 자주 다루는 주제. 시간이 갈수록 갱신됩니다.

예:
- 주요 관심: AI 에이전트, Harness Engineering, LLM 운영 환경
- 자주 인용하는 출처: 앤트로픽 블로그, 오픈AI 글, Martin Fowler 사이트
- 선호하는 자료 형태: 케이스 스터디 + 통계 데이터

### memory/research/long_term/preferred_authors.md
신뢰하거나 자주 인용하는 저자 목록.

예:
- Mitchell Hashimoto (HashiCorp 공동 창업자, AGENTS.md 패턴 제안)
- Birgitta Boeckeler (Thoughtworks, Martin Fowler 사이트)
- Andrej Karpathy (전 Tesla AI, 오픈AI)

### memory/research/long_term/past_research/{topic}.md
이전에 완료한 리서치의 최종 요약. 같은 주제가 다시 등장하면 이 파일을 먼저 읽고 어디까지 알고 있는지를 파악.

### 갱신 정책
- 한 리서치 작업이 완료되면 사용자에게 "이 결과를 장기 메모리에 저장할까요?"를 묻고, 승인 시 past_research/{topic}.md 생성
- user_interests.md는 5번의 리서치마다 자동으로 갱신 (자주 다룬 주제 추가)
- 사용자가 명시적으로 "이 저자 자주 인용한다"고 알리면 preferred_authors.md에 즉시 추가

## 4. 컨텍스트 압축 · 재시작 정책

### 압축 시점과 방법

토큰 사용량이 100,000을 넘기 전에 다음 압축을 적용합니다.

- 이미 처리한 자료의 본문은 컨텍스트에서 제거 (작업 메모리의 sources.jsonl에 요약은 남아 있음)
- 직전 호출의 결과는 핵심만 200자 요약으로 대체
- 단기 메모리에서 오래된 분석 노트는 작업 메모리(notes.md)에 옮기고 컨텍스트에서 제거

### Context Reflect - 새 세션으로 재시작

누적 토큰이 200,000을 넘으면 같은 세션을 계속 쓰지 않고 새 세션으로 재시작합니다.
새 세션 시작 시:
- progress.json의 핵심 부분을 시스템 프롬프트에 첨부
- sources.jsonl에서 처리해야 할 다음 자료만 단기 메모리에 로드
- 단기 메모리는 비운 상태에서 시작

이 흐름은 메모리 누수가 났을 때 가비지 컬렉션을 미친 듯이 돌리기보다 프로세스를 다시 시작하는 것과 같은 발상입니다.

### 비용 한도

- 한 리서치 작업당 최대 누적 토큰: 1,000,000
- 한 세션의 토큰: 200,000 (Context Reflect 시점)
- 한 자료 처리당 최대 토큰: 30,000
