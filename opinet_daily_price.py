# -*- coding: utf-8 -*-
"""
오피넷(Opinet) 석유제품·LPG 가격 자동 수집 및 MoM 산출

수집 소스 (각 페이지의 CSV 다운로드 엔드포인트를 그대로 호출 / API 키 불필요)
  1) 주유소 제품별 평균판매가격 (일간)
     https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do
     -> 보통휘발유(B027), 자동차용경유(D047), 실내등유(C004)   [원/L]
  2) 차량용 부탄 평균판매가격 (일간)
     https://www.opinet.co.kr/user/dopvsavsel/dopVsAvselSelect.do
     -> 자동차부탄(K015)                                      [원/L]
  3) 충전소 LPG 평균판매가격 (※ 일간 없음, 주간이 최소 단위)
     https://www.opinet.co.kr/user/dopsravsel/DopSrAvselSelect.do
     -> 일반프로판(K021), 일반부탄(K022)                       [원/kg]

기간 : 전월 1일 ~ 당월 최신 갱신일 (최신일은 페이지의 h_maxDD/h_maxWW에서 자동 인식)
산출 : 월평균(일별 또는 주별 단순평균) 및 직전월 대비 당월 MoM 변동률
"""

import io
import os
import re
import sys
from datetime import date, timedelta

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from net_utils import make_session

BASE = "https://www.opinet.co.kr"

# (조회 페이지 URL, CSV 엔드포인트 URL)
SRC_OIL = (f"{BASE}/user/dopospdrg/dopOsPdrgSelect.do",
           f"{BASE}/user/dopospdrg/dopOsPdrgCsv.do")
SRC_BUTANE = (f"{BASE}/user/dopvsavsel/dopVsAvselSelect.do",
              f"{BASE}/user/dopvsavsel/dopVsAvselCsv.do")
SRC_LPG = (f"{BASE}/user/dopsravsel/DopSrAvselSelect.do",
           f"{BASE}/user/dopcsavsel/DopSrAvselCsv.do")  # CSV만 경로가 dopcsavsel 임에 주의

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 오피넷 제품 코드 (체크박스 name = OIL_CD_<코드>)
OIL_PRODUCTS = {"B027": "보통휘발유", "D047": "자동차용경유", "C004": "실내등유"}
# 참고: B034 고급휘발유, C042 보일러등유

# 최종 리포트에 실을 품목 순서와 메타 정보
ITEM_META = {
    "보통휘발유":   ("원/L",  "일간"),
    "자동차용경유": ("원/L",  "일간"),
    "실내등유":     ("원/L",  "일간"),
    "자동차부탄":   ("원/L",  "일간"),
    "일반프로판":   ("원/kg", "주간"),
    "일반부탄":     ("원/kg", "주간"),
}

# 석유류 그룹 가중치 (CPI 가중치 기준, 합계 46.6)
WEIGHTS = {
    "보통휘발유":   24.1,
    "자동차용경유": 16.3,
    "자동차부탄":    2.8,
    "실내등유":      2.4,
    "일반프로판":    0.9,
    "일반부탄":      0.1,
}


# ----------------------------------------------------------------------
# 공통 유틸
# ----------------------------------------------------------------------
def _session(page_url: str) -> requests.Session:
    """조회 페이지를 먼저 열어 세션 쿠키/Referer를 확보한 Session 반환."""
    return make_session(referer=page_url, warm_url=page_url)


def _hidden_fields(session: requests.Session, page_url: str) -> dict:
    """조회 페이지의 h_max* hidden 필드를 그대로 긁어온다 (서버 검증용)."""
    res = session.get(page_url, timeout=20)
    res.raise_for_status()
    res.encoding = "euc-kr"
    return dict(re.findall(r'name="(h_max\w+)"[^>]*value="([^"]*)"', res.text))


def _post_csv(session: requests.Session, csv_url: str, payload: dict) -> pd.DataFrame:
    """CSV 엔드포인트를 POST 호출해 원본 DataFrame(첫 열=기간)을 반환."""
    res = session.post(csv_url, data=payload, timeout=30)
    res.raise_for_status()
    text = res.content.decode("euc-kr", errors="replace")

    df = pd.read_csv(io.StringIO(text))
    if df.empty:
        raise RuntimeError(f"CSV 응답이 비어 있습니다.\n{text[:300]}")
    return df


def _clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    """'자동차부탄 (원/ℓ)' -> '자동차부탄' 처럼 단위 표기를 컬럼명에서 제거."""
    df = df.rename(columns=lambda c: re.sub(r"\s*\(.*?\)\s*$", "", str(c)).strip())
    return df


_DAY_RE = re.compile(r"(\d{2,4})년\s*(\d{1,2})월\s*(\d{1,2})일")
_WEEK_RE = re.compile(r"(\d{2,4})년\s*(\d{1,2})월\s*(\d+)주")


def _parse_period(label: str):
    """
    오피넷 기간 라벨을 (연월키, 정렬키)로 변환.
      '2026년06월01일' / '26년06월01일' -> ('2026-06', '2026-06-01')
      '26년06월2주'                     -> ('2026-06', '2026-06-W2')
    """
    label = str(label).strip()

    m = _DAY_RE.match(label)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        y = y + 2000 if y < 100 else y
        return f"{y}-{mo:02d}", f"{y}-{mo:02d}-{d:02d}"

    m = _WEEK_RE.match(label)
    if m:
        y, mo, w = (int(x) for x in m.groups())
        y = y + 2000 if y < 100 else y
        return f"{y}-{mo:02d}", f"{y}-{mo:02d}-W{w}"

    raise ValueError(f"기간 라벨을 해석할 수 없습니다: {label!r}")


def _to_indexed(df: pd.DataFrame) -> pd.DataFrame:
    """첫 열(기간)을 파싱해 MultiIndex(연월, 기간)로 변환하고 값은 숫자화."""
    df = _clean_columns(df)
    period_col = df.columns[0]

    parsed = df[period_col].map(_parse_period)
    df = df.drop(columns=[period_col]).apply(pd.to_numeric, errors="coerce")
    df.index = pd.MultiIndex.from_tuples(list(parsed), names=["연월", "기간"])
    return df.sort_index()


# ----------------------------------------------------------------------
# 1. 최신 갱신일 조회
# ----------------------------------------------------------------------
def get_latest_date(session: requests.Session = None) -> date:
    """주유소 제품별 페이지의 h_maxDD(=일간 최신 데이터 일자)를 읽어온다."""
    page_url, _ = SRC_OIL
    session = session or _session(page_url)
    s = _hidden_fields(session, page_url).get("h_maxDD")
    if not s:
        raise RuntimeError("오피넷 페이지에서 최신 갱신일(h_maxDD)을 찾지 못했습니다.")
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


# ----------------------------------------------------------------------
# 2-a. 주유소 제품별 일별 가격 (휘발유 / 경유 / 등유)
# ----------------------------------------------------------------------
def fetch_oil_daily(start: date, end: date) -> pd.DataFrame:
    """보통휘발유·자동차용경유·실내등유의 일별 전국 평균 판매가격."""
    page_url, csv_url = SRC_OIL
    s = _session(page_url)

    payload = {
        "all_chk_cnt": "5",
        "INIF_FLAG": "N",
        "chk_cnt": str(len(OIL_PRODUCTS)),
        "TERM": "D",  # D=일간, W=주간, M=월간, Q=분기, Y=연간
        "sta_dt": start.strftime("%Y%m%d"),
        "end_dt": end.strftime("%Y%m%d"),
        "STA_Y": f"{start.year}", "STA_M": f"{start.month:02d}", "STA_D": f"{start.day:02d}",
        "END_Y": f"{end.year}",   "END_M": f"{end.month:02d}",   "END_D": f"{end.day:02d}",
        "equal": "Y",
    }
    payload.update(_hidden_fields(s, page_url))
    for code in OIL_PRODUCTS:
        payload[f"OIL_CD_{code}"] = "Y"

    return _to_indexed(_post_csv(s, csv_url, payload))


# ----------------------------------------------------------------------
# 2-b. 차량용 부탄 일별 가격 (자동차부탄)
# ----------------------------------------------------------------------
def fetch_butane_daily(start: date, end: date) -> pd.DataFrame:
    """자동차부탄(K015)의 일별 전국 평균 판매가격."""
    page_url, csv_url = SRC_BUTANE
    s = _session(page_url)

    payload = {
        "TERM": "D",
        "sta_dt": start.strftime("%Y%m%d"),
        "end_dt": end.strftime("%Y%m%d"),
        "STA_Y": f"{start.year}", "STA_M": f"{start.month:02d}", "STA_D": f"{start.day:02d}",
        "END_Y": f"{end.year}",   "END_M": f"{end.month:02d}",   "END_D": f"{end.day:02d}",
        "OIL_CD_K015_P": "Y",   # 자동차부탄
        "OIL_CD_K021_P": "",
        "OIL_CD_K022_P": "",
    }
    payload.update(_hidden_fields(s, page_url))

    return _to_indexed(_post_csv(s, csv_url, payload))


# ----------------------------------------------------------------------
# 2-c. 충전소 LPG 주간 가격 (일반프로판 / 일반부탄)
#      ※ 이 페이지는 일간 데이터가 없고 주간(W)이 최소 단위
# ----------------------------------------------------------------------
def fetch_lpg_weekly(start: date, end: date) -> pd.DataFrame:
    """
    일반프로판(K021)·일반부탄(K022)의 주간 전국 평균 판매가격.
    start/end는 날짜를 받지만 실제로는 '해당 월 전체 주차'를 요청한다
    (시작월 1주 ~ 종료월 5주, 존재하지 않는 주차는 서버가 알아서 잘라냄).
    """
    page_url, csv_url = SRC_LPG
    s = _session(page_url)

    payload = {
        "TERM": "W",  # 일간 없음 -> 주간이 최소 단위
        "STA_Y": f"{start.year}", "STA_M": f"{start.month:02d}", "STA_W": "1",
        "END_Y": f"{end.year}",   "END_M": f"{end.month:02d}",   "END_W": "5",
        "OIL_CD_K021_P": "Y",   # 일반프로판
        "OIL_CD_K022_P": "Y",   # 일반부탄
    }
    payload.update(_hidden_fields(s, page_url))

    return _to_indexed(_post_csv(s, csv_url, payload))


# ----------------------------------------------------------------------
# 3. 월평균 및 MoM 산출
# ----------------------------------------------------------------------
def monthly_average(df: pd.DataFrame) -> pd.DataFrame:
    """MultiIndex(연월, 기간) 데이터를 연월 단위 단순평균으로 집계."""
    return df.groupby(level="연월").mean()


def build_mom(monthly: pd.DataFrame, prev_ym: str, curr_ym: str) -> pd.DataFrame:
    """월평균 테이블 -> 직전월 대비 당월 MoM(%) 테이블."""
    missing = [ym for ym in (prev_ym, curr_ym) if ym not in monthly.index]
    if missing:
        raise RuntimeError(f"{', '.join(missing)} 데이터가 수집되지 않았습니다.")

    prev, curr = monthly.loc[prev_ym], monthly.loc[curr_ym]

    out = pd.DataFrame({
        "단위": [ITEM_META.get(i, ("", ""))[0] for i in monthly.columns],
        "주기": [ITEM_META.get(i, ("", ""))[1] for i in monthly.columns],
        f"{prev_ym} 평균": prev,
        f"{curr_ym} 평균": curr,
        "차이": curr - prev,
        "MoM(%)": (curr - prev) / prev * 100,
    })
    out.index.name = "품목"
    return out


def build_weighted_mom(mom: pd.DataFrame, weights: dict = None):
    """
    품목별 MoM(%)에 가중치를 적용한 석유류 그룹 전체 MoM(%)을 산출.

        그룹 MoM = Σ(품목 MoM × 가중치) / Σ(가중치)

    ※ 엄밀히는 가중치를 '가격 지수'에 적용해 지수를 합성한 뒤 변동률을 구해야 하지만,
      편의상 변동률에 직접 가중평균을 적용한다.

    반환: (기여도 테이블, 그룹 MoM(%))
    """
    weights = weights or WEIGHTS

    missing = [k for k in weights if k not in mom.index]
    if missing:
        raise RuntimeError(f"가중치 품목이 수집 결과에 없습니다: {', '.join(missing)}")

    out = pd.DataFrame({
        "가중치": pd.Series(weights),
        "MoM(%)": mom["MoM(%)"],
    }).loc[list(weights)]

    total_w = out["가중치"].sum()
    out["가중치비중(%)"] = out["가중치"] / total_w * 100
    out["기여도(%p)"] = out["MoM(%)"] * out["가중치"] / total_w
    out.index.name = "품목"

    group_mom = out["기여도(%p)"].sum()
    return out, group_mom


def _order_items(df: pd.DataFrame) -> pd.DataFrame:
    """ITEM_META 순서대로 컬럼 정렬 (목록에 없는 항목은 뒤에 유지)."""
    known = [c for c in ITEM_META if c in df.columns]
    rest = [c for c in df.columns if c not in known]
    return df[known + rest]


# ----------------------------------------------------------------------
# 4. 메인 진입점
# ----------------------------------------------------------------------
def get_price_mom(base_date: date = None, verbose: bool = True):
    """
    base_date를 주지 않으면 오피넷 최신 갱신일을 자동으로 사용한다.

    반환: dict
      'daily_oil'    - 휘발유/경유/등유 일별
      'daily_butane' - 자동차부탄 일별
      'weekly_lpg'   - 일반프로판/일반부탄 주별
      'monthly'      - 전 품목 월평균 (일별 또는 주별 단순평균)
      'mom'          - 직전월 대비 당월 MoM 테이블
      'weighted'     - 가중치·기여도 테이블
      'group_mom'    - 가중합산 석유류 그룹 MoM(%)
    """
    latest = base_date or get_latest_date()

    curr_start = latest.replace(day=1)
    prev_end = curr_start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)

    prev_ym = f"{prev_start:%Y-%m}"
    curr_ym = f"{curr_start:%Y-%m}"

    if verbose:
        print(f"📅 오피넷 최신 갱신일 : {latest:%Y-%m-%d}")
        print(f"   직전월 구간       : {prev_start:%Y-%m-%d} ~ {prev_end:%Y-%m-%d}")
        print(f"   당월 구간         : {curr_start:%Y-%m-%d} ~ {latest:%Y-%m-%d}")
        print()

    daily_oil = fetch_oil_daily(prev_start, latest)
    daily_butane = fetch_butane_daily(prev_start, latest)
    weekly_lpg = fetch_lpg_weekly(prev_start, latest)

    monthly = _order_items(pd.concat(
        [monthly_average(daily_oil),
         monthly_average(daily_butane),
         monthly_average(weekly_lpg)],
        axis=1,
    ))

    mom = build_mom(monthly, prev_ym, curr_ym)
    weighted, group_mom = build_weighted_mom(mom)

    if verbose:
        daily = _order_items(daily_oil.join(daily_butane, how="outer"))
        n_prev = (daily.index.get_level_values("연월") == prev_ym).sum()
        n_curr = (daily.index.get_level_values("연월") == curr_ym).sum()

        print("=" * 84)
        print(f" [일간] 주유소 제품별 · 차량용 부탄  ({prev_ym} {n_prev}일 / {curr_ym} {n_curr}일)")
        print("=" * 84)
        with pd.option_context("display.max_rows", 200, "display.width", 140):
            print(daily.droplevel("연월").to_string(float_format=lambda x: f"{x:,.2f}"))

        print()
        print("=" * 84)
        print(" [주간] 충전소 LPG (일간 미제공 → 주간 기준)")
        print("=" * 84)
        print(weekly_lpg.droplevel("연월").to_string(float_format=lambda x: f"{x:,.2f}"))

        print()
        print("=" * 84)
        print(" 월평균 가격")
        print("=" * 84)
        print(monthly.to_string(float_format=lambda x: f"{x:,.2f}"))

        print()
        print("=" * 84)
        print(f" MoM 변동률 : {curr_ym} vs {prev_ym}")
        print("=" * 84)
        print(mom.to_string(formatters={
            f"{prev_ym} 평균": "{:,.2f}".format,
            f"{curr_ym} 평균": "{:,.2f}".format,
            "차이": "{:>+9.2f}".format,
            "MoM(%)": "{:>+7.2f}%".format,
        }))
        print("-" * 84)
        print(f"※ {curr_ym}은 {latest:%m월 %d일}까지의 부분 평균 기준(속보치)입니다.")
        print("※ 일반프로판·일반부탄은 주간 고시가라 월중 변동이 없어 주간평균 = 오피넷 월간값입니다.")
        print("=" * 84)

        print()
        print("=" * 84)
        print(f" 🔥 석유류 그룹 가중합산 MoM : {curr_ym} vs {prev_ym}")
        print("=" * 84)
        print(weighted.to_string(formatters={
            "가중치": "{:>6.1f}".format,
            "MoM(%)": "{:>+7.2f}%".format,
            "가중치비중(%)": "{:>7.2f}%".format,
            "기여도(%p)": "{:>+8.3f}".format,
        }))
        print("-" * 84)
        print(f"✔️ 가중치 합계                  : {weighted['가중치'].sum():.1f}")
        print(f"🔥 석유류 그룹 가중합산 MoM      : {group_mom:>+.2f}%")
        print("=" * 84)

    return {
        "daily_oil": daily_oil,
        "daily_butane": daily_butane,
        "weekly_lpg": weekly_lpg,
        "monthly": monthly,
        "mom": mom,
        "weighted": weighted,
        "group_mom": group_mom,
    }


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    result = get_price_mom()

    # 필요 시 저장
    # result["monthly"].to_csv("opinet_monthly.csv", encoding="utf-8-sig")
    # result["mom"].to_csv("opinet_mom.csv", encoding="utf-8-sig")
