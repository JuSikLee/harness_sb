"""CSV/Excel 파일 로드 및 깨진 파일 감지."""
import csv
import io
import re
from pathlib import Path

# 회사 내부에서 약속한 컬럼명 (한국어 / 영문 별칭)
REQUIRED_COLUMNS = {
    "region": ("지역", "region"),
    "revenue": ("매출액", "revenue"),
    "last_month": ("전월", "last_month"),
}


class LoadError(Exception):
    """재시도해도 풀리지 않는 입력 파일 문제."""


def department_name(path):
    """sales_a.csv -> A부서"""
    m = re.match(r"sales_([a-z0-9]+)", Path(path).stem, re.I)
    return f"{m.group(1).upper()}부서" if m else Path(path).stem


def _read_csv(path):
    raw = Path(path).read_bytes()
    if b"\x00" in raw:
        raise LoadError("CSV 형식이 아님 (바이너리 데이터)")
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise LoadError("인코딩이 깨짐")
    if "�" in text:
        raise LoadError("인코딩이 깨짐")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    return rows


def _read_excel(path):
    try:
        import openpyxl
    except ImportError as e:
        raise LoadError("openpyxl 미설치로 Excel을 읽을 수 없음") from e
    try:
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
        return [["" if c is None else str(c) for c in r] for r in ws.iter_rows(values_only=True)
                if any(c is not None for c in r)]
    except Exception as e:
        raise LoadError(f"Excel 파일이 손상됨: {e}") from e


def load_file(path):
    """{'department', 'header', 'rows', 'csv_text'} 반환. 문제 시 LoadError."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".csv":
        rows = _read_csv(path)
    elif ext in (".xlsx", ".xlsm"):
        rows = _read_excel(path)
    else:
        raise LoadError(f"지원하지 않는 확장자: {ext}")

    if not rows:
        raise LoadError("데이터가 비어 있음")
    header = [h.strip() for h in rows[0]]
    body = rows[1:]
    if len(header) < 2 or any(len(r) != len(header) for r in body):
        raise LoadError("양식 불일치 (CSV 형식이 아님)")
    if not body:
        raise LoadError("데이터가 비어 있음")

    missing = [k for k, names in REQUIRED_COLUMNS.items() if not any(n in header for n in names)]
    if missing:
        raise LoadError(f"약속된 컬럼이 없음: {', '.join(missing)}")

    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows([header] + body)
    return {"department": department_name(path), "header": header, "rows": body,
            "csv_text": buf.getvalue()}
