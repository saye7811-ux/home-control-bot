# -*- coding: utf-8 -*-
"""공통 유틸: 경로, 로깅, CSV/JSON 입출력, 날짜/숫자 헬퍼."""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import date, datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
HIDDEN_DIR = os.path.join(BASE_DIR, "hidden")
SAMPLES_DIR = os.path.join(BASE_DIR, "samples")

LISTINGS_CSV = os.path.join(DATA_DIR, "listings.csv")
DETAILS_JSON = os.path.join(DATA_DIR, "details.json")
SCORED_CSV = os.path.join(DATA_DIR, "scored.csv")
MERGED_CSV = os.path.join(DATA_DIR, "merged.csv")
MARKET_JSON = os.path.join(DATA_DIR, "market.json")
BLOCKED_FLAG = os.path.join(DATA_DIR, "BLOCKED.txt")
EXTRACTED_JSON = os.path.join(HIDDEN_DIR, "extracted.json")
OPTION_MAP_JSON = os.path.join(DATA_DIR, "option_codes.json")
REPORT_HTML = os.path.join(BASE_DIR, "report.html")


def ensure_dirs() -> None:
    for d in (DATA_DIR, HIDDEN_DIR, SAMPLES_DIR):
        os.makedirs(d, exist_ok=True)


# ---------------------------------------------------------------------------
# 로깅 (의존성 없이 단순하게)
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] ! {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1):
    print(f"\n중단: {msg}\n", file=sys.stderr, flush=True)
    sys.exit(code)


# ---------------------------------------------------------------------------
# JSON / CSV
# ---------------------------------------------------------------------------
def read_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: str, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not rows:
        # 헤더만이라도 남겨서 다음 단계가 빈 파일과 없는 파일을 구분할 수 있게 한다
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            if fieldnames:
                csv.DictWriter(f, fieldnames=fieldnames).writeheader()
        return
    if fieldnames is None:
        seen: list[str] = []
        for r in rows:
            for k in r:
                if k not in seen:
                    seen.append(k)
        fieldnames = seen
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ---------------------------------------------------------------------------
# 숫자 / 날짜
# ---------------------------------------------------------------------------
def to_int(v, default=None):
    """'12,345km', '8,500만원', 12345.0 등을 int 로."""
    if v is None:
        return default
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        try:
            return int(v)
        except (ValueError, OverflowError):
            return default
    s = str(v).strip()
    if not s:
        return default
    # 부호 + 천단위 콤마 + 소수점을 하나의 수로 인식한다.
    # 단순히 숫자만 이어붙이면 "8145.1" 이 81451 이 되어 값이 10배로 튄다.
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", s)
    if not m:
        return default
    try:
        return int(float(m.group(0).replace(",", "")))
    except (ValueError, OverflowError):
        return default


def to_float(v, default=None):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_year_month(v) -> tuple[int | None, int | None]:
    """엔카 연식 표기를 (년, 월) 로.

    202203 / '2022-03' / '2022년 03월' / '2024년 5월' / '2022' 를 모두 지원한다.
    """
    if v is None:
        return None, None
    s = str(v).strip()
    if not s:
        return None, None

    # 붙어 있는 6자리(yyyyMM) 우선
    m6 = re.fullmatch(r"\D*(\d{4})(\d{2})\D*", s)
    if m6:
        y, mm = int(m6.group(1)), int(m6.group(2))
        if 1980 <= y <= 2100 and 1 <= mm <= 12:
            return y, mm

    # 구분자가 있는 형태: 2022-03, 2022년 3월, 2022.03 ...
    m = re.search(r"(19|20)\d{2}", s)
    if not m:
        return None, None
    y = int(m.group(0))
    if not (1980 <= y <= 2100):
        return None, None
    rest = s[m.end():]
    m2 = re.search(r"\d{1,2}", rest)
    if m2:
        mm = int(m2.group(0))
        if 1 <= mm <= 12:
            return y, mm
    return y, None
    s = str(v).strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 6:
        y, m = int(digits[:4]), int(digits[4:6])
        if 1 <= m <= 12 and 1980 <= y <= 2100:
            return y, m
    if len(digits) == 4:
        y = int(digits)
        if 1980 <= y <= 2100:
            return y, None
    return None, None


def parse_date(v) -> date | None:
    """20231205 / '2023-12-05' / '2023.12.05' 등을 date 로. 실패하면 None."""
    if v is None:
        return None
    s = str(v).strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        y, m, d = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
        if not (1980 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
            return None
        return date(y, m, d)
    except ValueError:
        return None


def age_years_from_date(d: date | None, today: date | None = None) -> float | None:
    """정확한 날짜 기준 경과 연수."""
    if d is None:
        return None
    today = today or date.today()
    return max((today - d).days / 365.25, 0.0)


def age_years(year: int | None, month: int | None, today: date | None = None) -> float | None:
    """최초등록(연식) 기준 경과 연수. 월 정보가 없으면 해당 연도 6월로 가정."""
    if not year:
        return None
    today = today or date.today()
    m = month if month and 1 <= month <= 12 else 6
    months = (today.year - year) * 12 + (today.month - m)
    return max(months / 12.0, 0.0)


def fmt_manwon(v) -> str:
    """만원 단위 정수를 '8,500만원' 으로."""
    n = to_int(v)
    return f"{n:,}만원" if n is not None else "-"


def fmt_km(v) -> str:
    n = to_int(v)
    return f"{n:,}km" if n is not None else "-"
