# -*- coding: utf-8 -*-
"""
kb.db -> kb_jeonse.csv 추출

kb.db(29MB)에는 25개 지역 × 12개 계열이 들어 있지만, 집세 CPI 예측에 쓰는 것은
전국 전세지수와 전국 전세수급지수 두 계열(약 1,000행)뿐이다. 그 두 계열만 뽑아
37KB CSV로 남긴다. 앱은 이 CSV만 읽으므로 kb.db를 배포에 포함할 필요가 없다.

새 kb.db를 받았을 때 한 번 실행한다.

    python extract_kb_jeonse.py --kb-db "경로/kb.db"

--kb-db 를 생략하면 아래 KB_DB_CANDIDATES 를 차례로 찾는다.
"""

import argparse
import os
import sqlite3
import sys
from contextlib import closing

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(BASE_DIR, "kb_jeonse.csv")

# --kb-db 없이 실행했을 때 찾아볼 위치 (먼저 발견된 것을 쓴다)
KB_DB_CANDIDATES = [
    os.path.join(BASE_DIR, "kb.db"),
    os.path.expanduser("~/Downloads/Telegram Desktop/kb.db"),
    os.path.expanduser("~/Downloads/kb.db"),
]

# (테이블, 구분열, 구분값, 출력 컬럼명)
SERIES = [
    ("kb_weekly", "series", "전세지수", "전세지수"),
    ("kb_survey", "key", "전세수급지수", "전세수급지수"),
]
REGION = "전국"


def find_kb_db(explicit=None):
    if explicit:
        if not os.path.exists(explicit):
            raise FileNotFoundError(f"kb.db를 찾을 수 없습니다: {explicit}")
        return explicit
    for path in KB_DB_CANDIDATES:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "kb.db를 찾을 수 없습니다. --kb-db 로 경로를 지정하세요.\n"
        "찾아본 위치:\n  " + "\n  ".join(KB_DB_CANDIDATES))


def extract(kb_db_path: str) -> pd.DataFrame:
    """kb.db에서 전국 전세지수·전세수급지수를 뽑아 날짜 기준으로 합친다."""
    uri = "file:" + os.path.abspath(kb_db_path).replace("\\", "/") + "?mode=ro"
    frames = []
    with closing(sqlite3.connect(uri, uri=True)) as db:
        for table, key_col, key_val, out_col in SERIES:
            df = pd.read_sql_query(
                f"SELECT date, value FROM {table} WHERE region=? AND {key_col}=? ORDER BY date",
                db, params=(REGION, key_val))
            if df.empty:
                raise ValueError(f"{table}에 {REGION}/{key_val} 자료가 없습니다.")
            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
            df["value"] = pd.to_numeric(df["value"])
            frames.append(df.rename(columns={"value": out_col}).set_index("date")[out_col])

    out = pd.concat(frames, axis=1).sort_index()
    out.index.name = "date"

    # 주간 기준일은 월요일이어야 한다 (모델이 W-MON 으로 리샘플링한다)
    bad = out.index[out.index.dayofweek != 0]
    if len(bad):
        raise ValueError(f"월요일이 아닌 기준일이 {len(bad)}건 있습니다: {bad[:3].tolist()}")

    return out.reset_index()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kb-db", default=None, help="kb.db 경로 (생략하면 자동 탐색)")
    parser.add_argument("--output", default=OUTPUT_FILE, help="출력 CSV 경로")
    args = parser.parse_args()

    kb_db_path = find_kb_db(args.kb_db)
    out = extract(kb_db_path)
    out.to_csv(args.output, index=False, encoding="utf-8-sig", date_format="%Y-%m-%d")

    latest = pd.Timestamp(out["date"].iloc[-1])
    weeks_old = (pd.Timestamp.today().normalize() - latest).days / 7
    size = os.path.getsize(args.output)
    print(f"원본   : {kb_db_path} ({os.path.getsize(kb_db_path):,} bytes)")
    print(f"출력   : {args.output} ({size:,} bytes)")
    print(f"기간   : {out['date'].iloc[0]} ~ {out['date'].iloc[-1]}  ({len(out)}행)")
    print(f"최신주 : {latest.date()} - 오늘 기준 {weeks_old:.0f}주 경과")
    if weeks_old > 39:
        print("[경고] 39주를 넘었습니다. 이 상태면 KB 반영이 역효과이므로 앱에서 옵션이 꺼집니다.")
    elif weeks_old > 26:
        print("[주의] 26주를 넘었습니다. 새 kb.db로 교체하는 편이 좋습니다.")


if __name__ == "__main__":
    sys.exit(main())
