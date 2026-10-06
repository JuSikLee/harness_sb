from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parents[2] / "specs" / "system_prompt.md"

SYSTEM_PROMPT = SPEC_PATH.read_text(encoding="utf-8")
