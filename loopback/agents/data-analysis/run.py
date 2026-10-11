"""tests/data의 모든 파일을 처리하고 통합 보고서를 생성."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import validator
from analyzer import TokenBudget, TokenLimitExceeded, get_client
from retry import analyze_with_retry

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "tests" / "data"
EXTS = {".csv", ".xlsx", ".xlsm"}
FAIL_RATIO_LIMIT = 0.5


def build_report(results):
    lines = ["# 통합 분석 보고서 (전월 대비)", ""]
    grand = 0
    for dept, r in sorted(results.items()):
        grand += float(r["total_revenue"])
        lines += [f"## {dept}", f"- 기간: {r['period']}", f"- 총 매출: {float(r['total_revenue']):,.0f}원"]
        lines += [f"  - {x['region']}: {float(x['revenue']):,.0f}원" for x in r["by_region"]]
        lines += [f"- 요약: {r.get('summary', '')}", ""]
    lines.append(f"**전체 매출 합계: {grand:,.0f}원**")
    return "\n".join(lines)


def main():
    files = sorted(p for p in DATA_DIR.iterdir() if p.suffix.lower() in EXTS)
    client, budget = get_client(), TokenBudget()
    results, failed, stop_reason = {}, [], None

    try:
        for i, path in enumerate(files):
            rel = str(path.relative_to(ROOT))
            print(f"[{i + 1}/{len(files)}] {rel}")
            result, info = analyze_with_retry(client, budget, str(path))
            if result is None:
                failed.append((rel, info))
            else:
                results[info["department"]] = result
            if len(failed) / len(files) >= FAIL_RATIO_LIMIT:
                stop_reason = f"입력 파일의 {FAIL_RATIO_LIMIT:.0%} 이상 실패 (data quality 문제 의심)"
                break
    except TokenLimitExceeded as e:
        stop_reason = str(e)

    if not stop_reason:
        problems = validator.validate_consistency(results, len(files) - len(failed))
        if problems:
            print("[일관성 검증 경고]\n" + "\n".join(f"- {p}" for p in problems))

    print(f"\n[작업 {'중단' if stop_reason else '완료'}]")
    if stop_reason:
        print(f"중단 사유: {stop_reason}")
    print(f"성공: {len(results)}건\n실패: {len(failed)}건")
    for f, why in failed:
        print(f"- {f}: {why}")
    print(f"토큰 사용량: {budget.used:,} / {budget.limit:,}")

    if results:
        print("\n성공한 부서로 분석 결과를 작성합니다.")
        out = ROOT / "tests" / "output"
        out.mkdir(exist_ok=True)
        (out / "report.md").write_text(build_report(results), encoding="utf-8")
        (out / "results.json").write_text(json.dumps(
            {"results": results, "failed_files": [{"file": f, "reason": w} for f, w in failed]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"보고서: {out / 'report.md'}")
    return 1 if stop_reason else 0


if __name__ == "__main__":
    sys.exit(main())
