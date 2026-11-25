# -*- coding: utf-8 -*-
"""
실시간 시세 스냅샷 생성기

국내 공공 사이트 일부는 해외 클라우드 IP를 TLS 단계에서 차단한다.
(Streamlit Cloud에서 KAMIS 접속 시 SSLV3_ALERT_HANDSHAKE_FAILURE)
클라이언트에서 우회할 수 없는 차단이므로, 접속이 되는 환경(사내망/로컬 PC)에서
이 스크립트를 돌려 결과를 파일로 떨궈 두고 앱이 그것을 읽도록 한다.

사용법
    python make_snapshot.py            # 기준월 = 엑셀 최신월 + 1개월 (앱과 동일)
    python make_snapshot.py 2026-07    # 기준월 직접 지정
    -> live_snapshot.json 생성 (git에 커밋해서 배포)

앱 동작
    1) 실시간 수집을 먼저 시도하고
    2) 실패한 소스만 이 스냅샷 값으로 대체한다
"""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import opinet_daily_price as opinet_src
import kamis_retail_price as kamis_src

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_FILE = os.path.join(BASE_DIR, "live_snapshot.json")

# 앱(cpi_estimation_9.py)이 읽는 원본 엑셀. 기준월을 앱과 동일하게 맞추기 위해 사용.
EXCEL_FILENAME = os.path.join(BASE_DIR, "CPI data_202605_v1.xlsx")


def resolve_target_ym() -> str:
    """
    기준월(YYYY-MM)을 결정한다.
      1) CLI 인자로 주면 그대로 사용
      2) 없으면 엑셀 CPI_Data 최신월 + 1개월 (앱의 target_date와 동일한 규칙)
    """
    if len(sys.argv) > 1:
        return sys.argv[1].strip()

    import pandas as pd

    df = pd.read_excel(EXCEL_FILENAME, sheet_name="CPI_Data", header=0)
    last = pd.to_datetime(df["Date"], errors="coerce").dropna().max()
    return (last + pd.DateOffset(months=1)).strftime("%Y-%m")


def build_petro(target_ym: str) -> dict:
    r = opinet_src.get_price_mom(target_ym=target_ym, verbose=False)
    return {
        "mom": float(r["group_mom"]),
        "curr": r["target_ym"],
        "prev": r["prev_ym"],
        "base_date": str(r["base_date"]),
        "is_partial": bool(r["is_partial"]),
        "detail": r["weighted"].reset_index().to_dict(orient="records"),
    }


def build_agri(target_ym: str = None) -> dict:
    ty, tm = opinet_src.parse_target_ym(target_ym) if target_ym else (None, None)
    r = kamis_src.get_price_mom(year=ty, month=tm, verbose=False)
    y, m = r["year"], r["month"]
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    return {
        "mom": float(r["group_mom"]),
        "mom_actual": float(r["group_mom_actual"]),
        "curr": f"{y}-{m:02d}",
        "prev": f"{py}-{pm:02d}",
        "is_partial": bool(r["is_partial"]),
        "coverage": {k: float(v) for k, v in r["coverage"].items()},
        "survey_date": r.get("survey_date"),
        "detail": r["weighted"].to_dict(orient="records"),
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        target_ym = resolve_target_ym()
    except Exception as e:
        print(f"⚠️ 기준월 자동 인식 실패({e}) → 오피넷 최신월 기준으로 수집합니다.")
        target_ym = None

    print(f"📅 기준월 : {target_ym or '자동(최신월)'}")

    snap = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "target_ym": target_ym}

    for key, fn, label in (("petro", lambda: build_petro(target_ym), "석유제품"),
                           ("agri", lambda: build_agri(target_ym), "농축수산물")):
        print(f"🔄 {label} 수집 중...")
        try:
            snap[key] = fn()
            print(f"   ✔️ {label} {snap[key]['curr']} : {snap[key]['mom']:+.3f}%")
        except Exception as e:
            snap[key] = None
            print(f"   ❌ {label} 실패: {e}")

    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1, default=str)

    print(f"\n💾 저장 완료 → {SNAPSHOT_FILE}")
    print("   git add live_snapshot.json && git commit && git push 로 배포하세요.")


if __name__ == "__main__":
    main()
