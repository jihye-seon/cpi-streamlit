# -*- coding: utf-8 -*-
"""
축평원(EKAPE) 축산물 소비자가격 자동 수집 및 MoM 산출

출처: https://www.ekapepia.com/v3/price/consumer/periodPrice.do?menuSn=40
      -> 조회단위 '월(MONTH)' 결과 화면을 그대로 파싱한다.
         이 화면은 GET 쿼리스트링만으로 조회가 되므로 별도 인증이 필요 없다.

수집 대상 (축종 7종 · 부위/규격은 화면 기본값 = 목록 첫 항목)
  4301 소            : 안심          [원/100g]
  4304 돼지          : 삼겹살        [원/100g]
  9901 닭            : 육계(kg)      [원/Kg]
  9903 계란          : XL30구        [원/30구]
  4401 수입 소고기    : 갈비(냉동)    [원/100g]
  4402 수입 돼지고기  : 삼겹살(냉동)  [원/100g]
  9908 우유          : 흰우유        [원/1L]
  ※ 전 부위/규격이 필요하면 get_price_mom(all_specs=True)

가격 기준 : 지역별 열은 쓰지 않고 전국 '평균' 열만 사용
기준 월   : 최신 조사월(자동 인식)과 그 직전월
산출      : 축종별 당월평균, 전월평균, MoM 등락률(%)
"""

import os
import re
import sys
from datetime import date
from io import StringIO

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from net_utils import SessionPool, make_session, pmap

BASE = "https://www.ekapepia.com"
PRICE_URL = f"{BASE}/v3/price/consumer/periodPrice.do"
SPEC_URL = f"{BASE}/supPrice/getRtsalSelectCode.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MENU_SN = "40"

# 축종코드 -> 표시명 (화면의 라디오 버튼 순서와 동일)
LIVESTOCK_TYPES = {
    "4301": "소",
    "4304": "돼지",
    "9901": "닭",
    "9903": "계란",
    "4401": "수입 소고기",
    "4402": "수입 돼지고기",
    "9908": "우유",
}

KEY_COLS = ["축종", "부위/규격", "단위", "등급"]

# 축종 × 부위/규격이 20건 안팎이라 순차로 돌면 대부분의 시간이 응답 대기다.
# 동시 요청 수 — 서버에 과한 부담을 주지 않으면서 대기를 겹치는 선.
MAX_WORKERS = 6


# ----------------------------------------------------------------------
# 공통 유틸
# ----------------------------------------------------------------------
def _session() -> requests.Session:
    """조회 페이지를 먼저 열어 세션 쿠키를 확보한 Session 반환."""
    return make_session(referer=PRICE_URL,
                        warm_url=f"{PRICE_URL}?menuSn={MENU_SN}&boardInfoNo=")


def _ym_str(y: int, m: int) -> str:
    return f"{y}-{m:02d}"


def _shift_ym(y: int, m: int, delta: int) -> tuple:
    """(년, 월)에서 delta개월 이동."""
    idx = y * 12 + (m - 1) + delta
    return idx // 12, idx % 12 + 1


_ROW_RE = re.compile(r"(\d{2})년\s*(\d{1,2})월")


def _parse_row_label(label: str):
    """'26년 07월' -> '2026-07'. 전년/평년 등 월이 아닌 행은 None."""
    m = _ROW_RE.match(str(label).strip())
    if not m:
        return None
    return _ym_str(2000 + int(m.group(1)), int(m.group(2)))


# ----------------------------------------------------------------------
# 1. 축종별 부위/규격 목록
# ----------------------------------------------------------------------
def get_spec_list(type_code: str, session: requests.Session = None) -> list:
    """축종코드의 부위/규격 목록을 [(코드, 명칭), ...]으로 반환."""
    session = session or _session()

    params = {"ctsed": type_code}
    if type_code == "9901":          # 닭은 수집구분(colct=A)이 있어야 목록이 나온다
        params["colct"] = "A"

    res = session.get(SPEC_URL, params=params, timeout=20)
    res.raise_for_status()

    out = []
    for spec in res.json().get("codeList", []):
        code, name = spec.get("PRDLST_CODE"), spec.get("MARK_NM")
        if type_code == "4402" and name == "삼겹살":   # 화면 표기와 동일하게 보정
            name = "삼겹살(냉동)"
        out.append((code, name))
    return out


# ----------------------------------------------------------------------
# 2. 월간 가격 조회
# ----------------------------------------------------------------------
def fetch_month_price(type_code: str, spec_code: str, start_ym: str, end_ym: str,
                      session: requests.Session = None) -> dict:
    """
    한 축종·부위의 월간 조회 결과를 파싱한다.
    grade는 빈 값으로 보내 서버 기본 등급을 따르고, 실제 적용된 등급을 응답에서 되읽는다.

    '전년' 행은 조회 종료월(end_ym)의 전년동월 값이므로 별도로 담는다.

    반환: {'prices': {'2026-07': 15104, ...}, 'last_year': 12693,
           'unit': '원/100g', 'grade': '03'}
    """
    session = session or _session()

    html = session.get(PRICE_URL, params={
        "menuSn": MENU_SN,
        "aggregationUnit": "MONTH",
        "startDate": start_ym,
        "endDate": end_ym,
        "livestockType": type_code,
        "spec": spec_code,
        "grade": "",
    }, timeout=30).text

    tables = pd.read_html(StringIO(html))
    if not tables or "구분" not in tables[0].columns or "평균" not in tables[0].columns:
        return {"prices": {}, "last_year": None, "unit": None, "grade": None}

    df = tables[0]
    prices, last_year = {}, None
    for label, avg in zip(df["구분"], df["평균"]):
        val = pd.to_numeric(str(avg).replace(",", "").strip(), errors="coerce")
        if pd.isna(val):
            continue

        ym = _parse_row_label(label)
        if ym is not None:
            prices[ym] = float(val)
        elif str(label).strip() == "전년":     # '평년' 행은 쓰지 않음
            last_year = float(val)

    unit = re.search(r"단위:\s*([^<\s]{1,20})", html)
    grade = re.search(r"searchParams\.set\('grade',\s*'([^']*)'\)", html)

    return {
        "prices": prices,
        "last_year": last_year,
        "unit": unit.group(1).strip() if unit else None,
        "grade": grade.group(1) if grade else None,
    }


def fetch_all(start_ym: str, end_ym: str, session: requests.Session = None,
              verbose: bool = True, all_specs: bool = False,
              max_workers: int = MAX_WORKERS) -> pd.DataFrame:
    """
    축종별 월별 가격을 수집한다.
    all_specs=False(기본)이면 축종마다 부위/규격 목록의 첫 항목(=화면 기본 선택)만 받는다.

    부위/규격 목록 조회와 가격 조회를 각각 병렬로 돌린다. 결과 순서는 순차
    수집과 동일하게 LIVESTOCK_TYPES 순서를 유지한다.
    """
    pool = SessionPool(_session)
    pool.preset(session)          # 호출부가 준 세션은 현재 스레드에서 그대로 쓴다

    # ---- 1단계: 축종별 부위/규격 목록 (7건) ----
    def _specs(type_code):
        found = get_spec_list(type_code, pool.session)
        return found if all_specs else found[:1]

    targets = []
    for type_code, specs, err in pmap(_specs, list(LIVESTOCK_TYPES), max_workers):
        type_name = LIVESTOCK_TYPES[type_code]
        if err is not None:
            print(f"      ⚠️ {type_name} 부위/규격 목록 조회 실패 — 건너뜁니다: {err}")
            continue
        if verbose:
            print(f"   [{type_name}] {', '.join(n for _, n in specs)} 조회 중...")
        targets += [(type_code, type_name, code, name) for code, name in specs]

    # ---- 2단계: (축종 × 부위/규격)별 월간 가격 ----
    def _price(target):
        type_code, _, spec_code, _ = target
        return fetch_month_price(type_code, spec_code, start_ym, end_ym, pool.session)

    rows = []
    for target, r, err in pmap(_price, targets, max_workers):
        _, type_name, _, spec_name = target

        if err is not None:
            print(f"      ⚠️ {type_name} - {spec_name} 조회 실패 — 건너뜁니다: {err}")
            continue
        if not r["prices"]:
            print(f"      ⚠️ {type_name} - {spec_name} 데이터 없음")
            continue

        rows.append({
            "축종": type_name,
            "부위/규격": spec_name,
            "단위": r["unit"],
            "등급": r["grade"] or "-",
            "전년동월": r["last_year"],   # 기준은 조회 종료월(end_ym)
            **r["prices"],
        })

    if not rows:
        raise RuntimeError("축산물 가격을 한 건도 수집하지 못했습니다.")

    return pd.DataFrame(rows)


# ----------------------------------------------------------------------
# 3. MoM 테이블 구성
# ----------------------------------------------------------------------
def build_mom(raw: pd.DataFrame, curr_ym: str, prev_ym: str,
              with_yoy: bool = False) -> pd.DataFrame:
    """
    수집 결과 -> 당월평균 / 전월평균 / MoM 등락률 테이블.

    '전년동월' 열은 조회 종료월 기준이므로, 당월이 종료월과 같을 때만
    (with_yoy=True) YoY를 함께 낸다.
    """
    for ym in (curr_ym, prev_ym):
        if ym not in raw.columns:
            raise RuntimeError(f"{ym} 데이터가 수집되지 않았습니다.")

    out = raw[KEY_COLS].copy()
    out[f"{prev_ym} 평균"] = raw[prev_ym]
    out[f"{curr_ym} 평균"] = raw[curr_ym]
    if with_yoy:
        out["전년동월평균"] = raw["전년동월"]
    out["차이"] = raw[curr_ym] - raw[prev_ym]
    out["MoM(%)"] = (raw[curr_ym] - raw[prev_ym]) / raw[prev_ym] * 100
    if with_yoy:
        out["YoY(%)"] = (raw[curr_ym] - raw["전년동월"]) / raw["전년동월"] * 100

    return out


def get_price_mom(year: int = None, month: int = None, verbose: bool = True,
                  all_specs: bool = False):
    """
    메인 진입점.
    year/month를 주지 않으면 수집된 데이터 중 가장 최근 월을 당월로 잡는다.
    all_specs=True면 축종별 전 부위/규격을, 기본값(False)이면 첫 항목만 수집한다.

    반환: dict
      'raw'  - 축종·부위별 월별 가격 (수집 원본)
      'mom'  - 당월평균 / 전월평균 / MoM 테이블
      'curr_ym', 'prev_ym'
    """
    session = _session()

    # 최신월을 모르므로 넉넉히 3개월 창으로 받아온 뒤, 실제로 값이 있는 최근 2개월을 쓴다
    today = date.today()
    end_y, end_m = (year, month) if (year and month) else (today.year, today.month)
    start_y, start_m = _shift_ym(end_y, end_m, -2)

    if verbose:
        print(f"🔄 EKAPE 축산물 소비자가격 조회 "
              f"({_ym_str(start_y, start_m)} ~ {_ym_str(end_y, end_m)})\n")

    raw = fetch_all(_ym_str(start_y, start_m), _ym_str(end_y, end_m), session,
                    verbose, all_specs)

    ym_cols = sorted(c for c in raw.columns if re.fullmatch(r"\d{4}-\d{2}", str(c)))
    if len(ym_cols) < 2:
        raise RuntimeError(f"비교 가능한 월이 부족합니다: {ym_cols}")

    if year and month:
        curr_ym = _ym_str(year, month)
        prev_ym = _ym_str(*_shift_ym(year, month, -1))
    else:
        curr_ym, prev_ym = ym_cols[-1], ym_cols[-2]

    # '전년동월'은 조회 종료월 기준이므로 당월과 일치할 때만 YoY를 낸다
    mom = build_mom(raw, curr_ym, prev_ym,
                    with_yoy=(curr_ym == _ym_str(end_y, end_m)))

    if verbose:
        print()
        print("=" * 92)
        print(f" EKAPE 축산물 소비자가격 MoM : {curr_ym} vs {prev_ym}  (총 {len(mom)}행)")
        print("=" * 92)
        with pd.option_context("display.max_rows", 100, "display.width", 160):
            print(mom.to_string(index=False, na_rep="-", formatters={
                f"{prev_ym} 평균": lambda x: f"{x:,.0f}",
                f"{curr_ym} 평균": lambda x: f"{x:,.0f}",
                "전년동월평균": lambda x: f"{x:,.0f}",
                "차이": lambda x: f"{x:+,.0f}",
                "MoM(%)": lambda x: f"{x:+.2f}",
                "YoY(%)": lambda x: f"{x:+.2f}",
            }))

        if all_specs:
            print()
            print("=" * 92)
            print(" 축종별 단순평균 MoM")
            print("=" * 92)
            by_type = mom.groupby("축종", sort=False)["MoM(%)"].mean()
            print(by_type.to_string(float_format=lambda x: f"{x:+.2f}%"))

        print()
        print("-" * 92)
        print(f"※ {curr_ym}은 조사 진행 중인 부분 평균(속보치)일 수 있습니다.")
        print("※ 가격은 지역별 열을 제외한 전국 '평균' 열 기준이며, 등급은 화면 기본 등급을 따릅니다.")
        print("※ 2026-07-01 광주·전남 통합으로 지역별 열 구성이 달라졌으나 '평균'은 영향받지 않습니다.")
        print("=" * 92)

    return {"raw": raw, "mom": mom, "curr_ym": curr_ym, "prev_ym": prev_ym}


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    result = get_price_mom()

    # 필요 시 저장
    # result["mom"].to_excel("EKAPE_축산물_MoM.xlsx", index=False)
