"""회의 녹취를 받아 회의록으로 정리해 출력한다.

사용법:
    python run.py transcript.txt
    cat transcript.txt | python run.py
"""
import argparse
import sys

import anthropic

from prompts import SYSTEM_PROMPT

DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 4096


def read_transcript(path):
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read()
    if sys.stdin.isatty():
        print("회의 녹취를 입력하고 Ctrl-D로 끝내세요.", file=sys.stderr)
    return sys.stdin.read()


def available_models(client):
    """현재 사용 가능한 모델 ID 목록 (Sonnet 계열 우선, 최신순)."""
    ids = [m.id for m in client.models.list(limit=100)]
    return sorted(ids, key=lambda i: "sonnet" not in i)


def generate(client, model, transcript):
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": transcript}],
    )
    return "".join(b.text for b in response.content if b.type == "text")


def main():
    parser = argparse.ArgumentParser(description="회의록 자동화 에이전트")
    parser.add_argument("file", nargs="?", help="회의 녹취 파일 (생략 시 표준 입력)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    transcript = read_transcript(args.file).strip()
    if not transcript:
        sys.exit("입력된 회의 녹취가 없습니다.")

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY 환경변수 사용
    try:
        print(generate(client, args.model, transcript))
    except (anthropic.NotFoundError, anthropic.BadRequestError) as e:
        print(f"모델 '{args.model}' 호출 실패 ({e}). 사용 가능한 모델로 변경합니다.",
              file=sys.stderr)
        candidates = [m for m in available_models(client) if m != args.model]
        if not candidates:
            sys.exit("사용 가능한 모델을 찾지 못했습니다.")
        print(f"대체 모델: {candidates[0]}", file=sys.stderr)
        print(generate(client, candidates[0], transcript))


if __name__ == "__main__":
    main()
