"""카드 CSV/Excel 로더 - 로컬 파일만 읽는다 (architecture.md §1·§2).

- 카드사별 헤더 어댑터 dict (확장 가능), 첫 10줄 전수 검사로 헤더 행 식별
- 식별 실패 시 (PII 마스킹된) 헤더 출력 + specs/card_signatures.json 에 후보 누적
- 이름·카드번호·전화번호 등 PII 는 필요한 컬럼만 남겨 애초에 보관하지 않고, 남는 문자열도 마스킹
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DIR = Path("/home/prime/harness_sb/harness-book-samples/card-insight")
USER_DIR = PROJECT_ROOT
SIGNATURE_FILE = PROJECT_ROOT / "specs" / "card_signatures.json"
HEADER_SCAN_ROWS = 10

# 카드사별 헤더 어댑터. 필수: date / merchant / amount. 나머지는 선택.
# verified=True 는 실제 샘플 파일로 확인된 것만. 나머지는 일반적인 헤더명 템플릿(미검증)이므로
# 실제 파일과 다르면 식별 실패 → card_signatures.json 누적 경로로 확장한다.
ISSUER_ADAPTERS: dict[str, dict] = {
    "롯데": {  # 샘플(card_expense_2026xx.xlsx) 헤더와 일치 - 발급사 표기는 추정
        "verified": True,
        "date": ["승인일"], "merchant": ["가맹점명"], "amount": ["승인금액"],
        "time": ["승인시각"], "status": ["승인구분"], "cancel_date": ["취소일"],
        "installment_months": ["할부개월"], "pay_type": ["이용구분"],
    },
    "삼성": {"verified": False, "date": ["이용일자", "이용일"], "merchant": ["이용가맹점", "가맹점명"],
            "amount": ["이용금액"], "status": ["취소여부"], "installment_months": ["할부개월"]},
    "신한": {"verified": False, "date": ["거래일자", "이용일자"], "merchant": ["가맹점명", "이용처"],
            "amount": ["이용금액", "거래금액"], "status": ["상태"], "installment_months": ["할부"]},
    "현대": {"verified": False, "date": ["이용일", "이용일자"], "merchant": ["이용가맹점", "가맹점"],
            "amount": ["이용금액"], "status": ["취소여부"], "installment_months": ["할부개월"]},
    "KB": {"verified": False, "date": ["이용일", "이용일자"], "merchant": ["이용하신곳", "가맹점명"],
          "amount": ["이용금액"], "status": ["상태"], "installment_months": ["할부개월"]},
    "우리": {"verified": False, "date": ["이용일자", "승인일자"], "merchant": ["이용가맹점명", "가맹점명"],
            "amount": ["이용금액", "승인금액"], "status": ["취소여부"], "installment_months": ["할부개월"]},
    "하나": {"verified": False, "date": ["이용일자", "거래일"], "merchant": ["이용가맹점", "가맹점명"],
            "amount": ["이용금액"], "status": ["취소여부"], "installment_months": ["할부개월"]},
    "NH농협": {"verified": False, "date": ["이용일자", "승인일자"], "merchant": ["이용가맹점", "가맹점명"],
              "amount": ["이용금액", "승인금액"], "status": ["취소여부"], "installment_months": ["할부개월"]},
}
REQUIRED = ("date", "merchant", "amount")
OPTIONAL = ("time", "status", "cancel_date", "installment_months", "pay_type")

PII_PATTERNS = [
    (re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{3,4}\b"), "[카드번호]"),
    (re.compile(r"\b01[016789][- ]?\d{3,4}[- ]?\d{4}\b"), "[전화번호]"),
    (re.compile(r"\b0\d{1,2}[- ]\d{3,4}[- ]\d{4}\b"), "[전화번호]"),
    (re.compile(r"\b\d{6}[- ]?[1-4]\d{6}\b"), "[주민번호]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[이메일]"),
]


class HeaderNotRecognized(Exception):
    pass


def mask_pii(text) -> str:
    out = str(text)
    for pat, repl in PII_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _clean(cell) -> str:
    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return ""
    return re.sub(r"\s+", "", str(cell))


def _confirmed_signature_adapters() -> dict[str, dict]:
    """card_signatures.json 에서 사용자가 확정(confirmed=true)한 컬럼 매핑을 어댑터로 추가."""
    extra: dict[str, dict] = {}
    if not SIGNATURE_FILE.exists():
        return extra
    try:
        data = json.loads(SIGNATURE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return extra
    for sig in data["signatures"] if "signatures" in data else []:
        if "confirmed" in sig and sig["confirmed"] and "issuer" in sig and "columns" in sig:
            cols = sig["columns"]
            extra[sig["issuer"]] = {k: [v] for k, v in cols.items()}
    return extra


def detect_header(rows: list[list]):
    """첫 10줄 전수 검사 → (issuer, header_row_index, {field: col_index})."""
    adapters = {**ISSUER_ADAPTERS, **_confirmed_signature_adapters()}
    for i, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        cells = [_clean(c) for c in row]
        for issuer, ad in adapters.items():
            idx: dict[str, int] = {}
            used: set[int] = set()
            for field in REQUIRED + OPTIONAL:
                if field not in ad:
                    continue
                for alias in ad[field]:
                    a = _clean(alias)
                    hit = [j for j, c in enumerate(cells) if c == a and j not in used]
                    if hit:
                        idx[field] = hit[0]
                        used.add(hit[0])
                        break
            if all(f in idx for f in REQUIRED):
                return issuer, i, idx
    raise HeaderNotRecognized(_record_unknown_signature(rows))


def _record_unknown_signature(rows: list[list]) -> str:
    """식별 실패: 첫 10줄을 마스킹해 보여주고 card_signatures.json 에 미확정 후보로 누적."""
    shown = []
    for row in rows[:HEADER_SCAN_ROWS]:
        shown.append([mask_pii(c) for c in row if _clean(c)])
    try:
        data = json.loads(SIGNATURE_FILE.read_text(encoding="utf-8")) if SIGNATURE_FILE.exists() else {}
    except (OSError, ValueError):
        data = {}
    sigs = data["signatures"] if "signatures" in data else []
    if shown not in [s["header_rows"] for s in sigs if "header_rows" in s]:
        sigs.append({"confirmed": False, "issuer": "", "columns": {}, "header_rows": shown})
    SIGNATURE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SIGNATURE_FILE.write_text(json.dumps({"signatures": sigs}, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = "\n".join(f"  {i + 1}행: {r}" for i, r in enumerate(shown))
    return (
        "카드사 헤더를 식별하지 못했습니다. 아래 상단 10줄(PII 마스킹됨)을 확인하고 카드사를 알려주세요.\n"
        f"{lines}\n"
        f"→ {SIGNATURE_FILE} 에 미확정 후보로 저장했습니다. issuer 와 columns"
        '(예: {"date": "이용일", "merchant": "가맹점", "amount": "금액"})를 채우고 confirmed=true 로 바꾸면 이후 자동 인식됩니다.'
    )


def parse_date(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return v.date()
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, (int, float)) and 30000 < float(v) < 80000:  # 엑셀 일련번호
        return (dt.datetime(1899, 12, 30) + dt.timedelta(days=int(v))).date()
    s = str(v).strip()
    m = re.fullmatch(r"(\d{4})[-./]?(\d{1,2})[-./]?(\d{1,2})(?:\D.*)?", s)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if re.fullmatch(r"\d{5}(\.0)?", s):
        return parse_date(float(s))
    return None


def parse_amount(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    s = re.sub(r"[,\s원₩]", "", str(v))
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return int(round(float(s)))
    return None


def _read_sheets(path: Path) -> list[list[list]]:
    if path.suffix.lower() == ".csv":
        for enc in ("utf-8-sig", "cp949"):
            try:
                with open(path, newline="", encoding=enc) as f:
                    return [[row for row in csv.reader(f)]]
            except UnicodeDecodeError:
                continue
        raise ValueError(f"인코딩을 읽을 수 없습니다: {path.name}")
    sheets = pd.read_excel(path, header=None, sheet_name=None, dtype=object)
    return [df.values.tolist() for df in sheets.values()]


def load_file(path: Path) -> tuple[pd.DataFrame, dict]:
    recs = []
    cancel_rows = 0
    cancel_amount = 0
    for rows in _read_sheets(path):
        issuer, hdr, idx = detect_header(rows)
        for row in rows[hdr + 1:]:
            def cell(field):
                j = idx[field] if field in idx else None
                return row[j] if j is not None and j < len(row) else None

            d = parse_date(cell("date"))
            amt = parse_amount(cell("amount"))
            if d is None or amt is None:  # 소계/합계/빈 줄 제외
                continue
            status = _clean(cell("status"))
            cancelled = "취소" in status or parse_date(cell("cancel_date")) is not None
            if cancelled:
                cancel_rows += 1
                cancel_amount += abs(amt)
                continue
            months = parse_amount(cell("installment_months"))
            pay = _clean(cell("pay_type"))
            installment = "할부" if (months and months > 0) or "할부" in pay else "일시불"
            recs.append({
                "date": d,
                "time": _clean(cell("time")),
                "merchant_raw": mask_pii(str(cell("merchant")).strip()),
                "amount": amt,
                "installment": installment,
                "issuer": issuer,
                "source": path.name,
            })
    df = pd.DataFrame(recs, columns=["date", "time", "merchant_raw", "amount", "installment", "issuer", "source"])
    df = _pair_refunds(df)
    return df, {"cancel_rows": cancel_rows, "cancel_amount": cancel_amount}


def _pair_refunds(df: pd.DataFrame) -> pd.DataFrame:
    """음수 금액(환불) 행은 같은 가맹점·같은 금액의 양수 행과 상쇄해 둘 다 제거."""
    if df.empty or (df["amount"] >= 0).all():
        return df
    drop: set[int] = set()
    for i in df.index[df["amount"] < 0]:
        cand = df[(df["merchant_raw"] == df.at[i, "merchant_raw"]) & (df["amount"] == -df.at[i, "amount"])]
        cand = [j for j in cand.index if j not in drop]
        if cand:
            drop.update([i, cand[0]])
    return df.drop(index=list(drop)).reset_index(drop=True)


def discover_files() -> list[Path]:
    """사용자 폴더의 .csv/.xlsx 를 우선, 없으면 샘플 폴더 사용."""
    user = sorted(p for p in USER_DIR.glob("*") if p.suffix.lower() in (".csv", ".xlsx") and not p.name.startswith("~$"))
    if user:
        return user
    return sorted(p for p in SAMPLE_DIR.glob("*") if p.suffix.lower() in (".csv", ".xlsx") and not p.name.startswith("~$"))


def load_all() -> tuple[pd.DataFrame, dict]:
    files = discover_files()
    if not files:
        raise FileNotFoundError(f"카드 내역 파일(.csv/.xlsx)이 없습니다: {USER_DIR} 또는 {SAMPLE_DIR}")
    frames = []
    meta = {"files": [p.name for p in files], "cancel_rows": 0, "cancel_amount": 0}
    for p in files:
        df, m = load_file(p)
        frames.append(df)
        meta["cancel_rows"] += m["cancel_rows"]
        meta["cancel_amount"] += m["cancel_amount"]
    out = pd.concat(frames, ignore_index=True)
    out["month"] = out["date"].map(lambda d: f"{d.year:04d}-{d.month:02d}")
    return out, meta
