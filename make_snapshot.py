# -*- coding: utf-8 -*-
"""
실시간 시세 스냅샷 생성기

국내 공공 사이트 일부는 해외 클라우드 IP를 TLS 단계에서 차단한다.
(Streamlit Cloud에서 KAMIS 접속 시 SSLV3_ALERT_HANDSHAKE_FAILURE)
클라이언트에서 우회할 수 없는 차단이므로, 접속이 되는 환경(사내망/로컬 PC)에서
이 스크립트를 돌려 결과를 파일로 떨궈 두고 앱이 그것을 읽도록 한다.

사용법
    python make_snapshot.py
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

SNAPSHOT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "live_snapshot.json")


def build_petro() -> dict:
    r = opinet_src.get_price_mom(verbose=False)
    months = [str(i) for i in r["monthly"].index]
    return {
        "mom": float(r["group_mom"]),
        "curr": months[-1],
        "prev": months[-2],
        "detail": r["weighted"].reset_index().to_dict(orient="records"),
    }


def build_agri() -> dict:
    r = kamis_src.get_price_mom(verbose=False)
    y, m = r["year"], r["month"]
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    return {
        "mom": float(r["group_mom"]),
        "mom_actual": float(r["group_mom_actual"]),
        "curr": f"{y}-{m:02d}",
        "prev": f"{py}-{pm:02d}",
        "coverage": {k: float(v) for k, v in r["coverage"].items()},
        "survey_date": r.get("survey_date"),
        "detail": r["weighted"].to_dict(orient="records"),
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    snap = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    for key, fn, label in (("petro", build_petro, "석유제품"),
                           ("agri", build_agri, "농축수산물")):
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
