# QR Code MCP 서버 스펙

## 목적
사용자가 자연어로 텍스트(URL · 연락처 · Wi-Fi 정보 등)를 입력하면, QR 코드 PNG 파일을 변환해 지정한 폴더에 저장한다.

## 사용 언어와 라이브러리
- Python 3.11+
- mcp (MCP Python SDK)
- qrcode[pil] (QR 코드 생성)

## 노출 도구 (1개)
도구 이름: make_qr
입력:
  - text (string, 필수): QR로 인코딩할 텍스트
  - output_path (string, 필수): 저장할 PNG 파일의 절대 경로
출력:
  - success (bool)
  - path (string): 저장된 파일 절대 경로
  - size_bytes (int)

## 에러 케이스
- text가 빈 문자열인 경우 → error: "text is required"
- output_path가 .png로 끝나지 않는 경우 → error: "output_path must end with .png"
- 저장 폴더가 존재하지 않는 경우 → 폴더를 자동 생성한 뒤 저장
  
## 비기능 요건
- 외부 네트워크 호출 없음(완전 로컬)
- 한 번 호출 시 평균 200ms 이내 응답
