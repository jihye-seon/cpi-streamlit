# -*- coding: utf-8 -*-
"""
농·수산물(KAMIS) + 축산물(EKAPE) 소매가격 자동 수집 및 MoM 산출

[1] 농·수산물 — KAMIS 소매가격 일람표
    https://www.kamis.or.kr/customer/price/agricultureRetail/catalogue.do
    -> '월간(monthly)' 조회 결과를 그대로 파싱한다.
       이 화면이 당월평균(A)·전월평균(B)·등락률(A/B)을 이미 제공하므로
       KAMIS OpenAPI 인증키 없이도 필요한 값이 모두 나온다.
    수집 대상: 식량작물 / 채소류 / 특용작물 / 과일류 / 수산물 전 품목

[2] 축산물 — EKAPE 축산물 소비자가격 (ekape_livestock_price 모듈 위임)
    https://www.ekapepia.com/v3/price/consumer/periodPrice.do?menuSn=40
    -> KAMIS 화면에는 축산물이 없으므로 축평원에서 별도로 가져와 합친다.
    수집 대상: 소 / 돼지 / 닭 / 계란 / 수입 소고기 / 수입 돼지고기 / 우유

기준 월 : KAMIS 최신 조사월(자동 인식)과 그 직전월
산출    : 품목별 당월평균, 전월평균, MoM 등락률(%)
"""

import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import StringIO

import pandas as pd
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 다른 디렉터리에서 실행해도 옆에 있는 모듈을 찾도록 경로 보정
sys.path.insert(0, BASE_DIR)

from net_utils import SessionPool, make_session, pmap

try:
    import ekape_livestock_price as ekape
except ImportError:      # 축산물 모듈이 없으면 농·수산물만 수집
    ekape = None

URL = "https://www.kamis.or.kr/customer/price/agricultureRetail/catalogue.do"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# KAMIS 부류코드 (이 화면에서 조회 가능한 것만)
CATEGORIES = {
    "100": "식량작물",
    "200": "채소류",
    "300": "특용작물",
    "400": "과일류",
    "600": "수산물",
}
# 참고: 500(축산물)은 이 화면에 데이터가 없음

# KAMIS OpenAPI 인증 정보 (이 모듈에서는 사용하지 않음. 별도 API 연동 시 참고용)
KAMIS_KEY = "9e824f71-a7fd-4d4c-9dc9-cab42d2b3bc9"
KAMIS_ID = "8533"

KEY_COLS = ["부류", "품목", "품종", "단위", "등급"]

# CPI 가중치 매칭표 (A열 CPI 품목명 / B열 kamis 품목명 / C열 가중치)
WEIGHT_FILE = "CPI data_202605_v1.xlsx"
WEIGHT_SHEET = "kamis_name"

# 품목별 CPI 원지수 시계열 (A열 품목명은 kamis_name 시트 A열과 동일)
DETAIL_SHEET = "detail"
SEASONAL_YEARS = 3      # 미매칭 항목을 채울 때 참고할 과거 연수

# A/B열 이름만으로는 잡히지 않는 CPI 품목의 수동 매칭 규칙
# (1) 여러 수집 품목을 묶어 평균낼 항목
EXPLICIT_MATCH = {
    "돼지고기": ["돼지", "수입 돼지고기"],   # 국산·수입 돼지고기를 함께 평균
}
# (2) 이름에 특정 문자열이 들어가는 수집 품목을 모두 묶을 항목
#     ※ 정확히 일치하는 품목이 없을 때만 적용된다
CONTAINS_MATCH = {
    "버섯": "버섯",                          # 느타리·팽이·새송이버섯 평균
}

# '2026-07 평균' 같은 월별 평균 열을 찾기 위한 패턴
_YM_COL = re.compile(r"^\d{4}-\d{2} 평균$")

# "조회 결과가 없다"는 안내문이 표 자리에 그대로 렌더링되어 오는 경우를 걸러내기 위한 패턴
_EMPTY_HINT = re.compile(r"조사일|데이터가 없습니다|검색조건")


# ----------------------------------------------------------------------
# 공통 유틸
# ----------------------------------------------------------------------
def _session() -> requests.Session:
    """조회 페이지를 먼저 열어 세션 쿠키를 확보한 Session 반환."""
    return make_session(referer=URL, warm_url=URL)


def _payload(action: str, **kw) -> dict:
    """catalogue.do 조회 폼의 전체 필드를 기본값으로 채운 payload."""
    p = {
        "action": action,
        "actionUrl": URL,
        "itemcategorycode": "",
        "yyyy": "", "mm": "", "week": "", "regday": "",
        "start_mmdd": "", "period": "", "startday": "", "endday": "",
        "itemcode": "", "kindcode": "", "productrankcode": "",
        "marketclscode": "", "marketcode": "", "countycode": "",
        "convert_kg_yn": "N",
    }
    p.update(kw)
    return p


def _read_table(html: str) -> pd.DataFrame:
    """응답 HTML에서 첫 번째 표를 읽는다. 안내문만 있으면 빈 DataFrame."""
    tables = pd.read_html(StringIO(html))
    if not tables:
        return pd.DataFrame()

    df = tables[0]
    if len(df) == 1 and _EMPTY_HINT.search(str(df.iloc[0, 0])):
        return pd.DataFrame()
    return df


def _strip_tags(html: str) -> str:
    """안내문이 <a> 태그로 쪼개져 오므로 태그를 제거하고 공백을 정규화한다."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _to_num(s: pd.Series) -> pd.Series:
    """'62,387' -> 62387.0, '-' -> NaN"""
    return pd.to_numeric(
        s.astype(str).str.replace(",", "", regex=False).str.strip().replace("-", None),
        errors="coerce",
    )


# ----------------------------------------------------------------------
# 1. 최신 조사 시점 인식
# ----------------------------------------------------------------------
def get_latest_month(session: requests.Session = None) -> tuple:
    """
    아직 조사되지 않은 월을 조회하면 KAMIS가 표 대신
    '가장 최근 조사일은 YYYY년 MM월 입니다'를 돌려주는 것을 이용한다.
    당해 12월을 찔러보고, 안내문이 없으면(=12월 데이터가 이미 있으면) 그대로 12월.
    반환: (년, 월)
    """
    session = session or _session()
    year = date.today().year

    html = session.post(URL, data=_payload("monthly", yyyy=str(year), mm="12"), timeout=30).text

    m = re.search(r"가장 최근 조사일은\s*(\d{4})년\s*(\d{1,2})월", _strip_tags(html))
    if m:
        return int(m.group(1)), int(m.group(2))

    if not _read_table(html).empty:
        return year, 12

    raise RuntimeError("KAMIS 페이지에서 최신 조사월을 확인하지 못했습니다.")


def get_latest_survey_date(session: requests.Session = None) -> str:
    """일간 조회의 안내문에서 최신 조사일(YYYY.MM.DD)을 알아낸다. 실패 시 None."""
    session = session or _session()
    probe = date.today().replace(month=12, day=31).isoformat()
    html = session.post(URL, data=_payload("daily", regday=probe), timeout=30).text

    m = re.search(r"가장 최근 조사일은\s*(\d{4})\.(\d{2})\.(\d{2})", _strip_tags(html))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


# ----------------------------------------------------------------------
# 2. 월간 소매가격 일람표 수집
# ----------------------------------------------------------------------
def fetch_monthly(year: int, month: int, category: str = "",
                  session: requests.Session = None) -> pd.DataFrame:
    """
    특정 부류(category='' 이면 전체)의 월간 일람표를 원본 컬럼 그대로 반환.
    컬럼: 품목, 품종, 단위, 등급, 당월평균(A), 전월평균(B), 전년동월평균, 등락률(A/B), 월평년
    """
    session = session or _session()
    html = session.post(
        URL,
        data=_payload("monthly", yyyy=str(year), mm=f"{month:02d}",
                      itemcategorycode=category),
        timeout=30,
    ).text
    return _read_table(html)


def fetch_monthly_all(year: int, month: int, session: requests.Session = None) -> pd.DataFrame:
    """
    부류별로 나눠 조회한 뒤 '부류' 라벨을 붙여 하나로 합친다.
    부류 5건은 서로 독립이라 병렬로 받는다 (결과 순서는 CATEGORIES 순서를 유지).
    """
    pool = SessionPool(_session)
    pool.preset(session)

    frames = []
    for code, df, err in pmap(lambda c: fetch_monthly(year, month, c, pool.session),
                              list(CATEGORIES), max_workers=len(CATEGORIES)):
        name = CATEGORIES[code]
        if err is not None:
            print(f"   ⚠️ {name}({code}) 조회 실패 — 건너뜁니다: {err}")
            continue
        if df.empty:
            print(f"   ⚠️ {name}({code}) 데이터 없음 — 건너뜁니다.")
            continue
        df = df.copy()
        df.insert(0, "부류", name)
        frames.append(df)

    if not frames:
        raise RuntimeError(f"{year}년 {month}월 데이터를 한 건도 수집하지 못했습니다.")

    return pd.concat(frames, ignore_index=True)


# ----------------------------------------------------------------------
# 3. MoM 테이블 구성
# ----------------------------------------------------------------------
def build_mom(raw: pd.DataFrame, year: int, month: int) -> pd.DataFrame:
    """
    원본 일람표 -> 당월평균 / 전월평균 / MoM 등락률 테이블.

    KAMIS가 제공하는 등락률은 소수 1자리로 반올림되어 있어,
    MoM(%)은 당월평균·전월평균에서 직접 계산하고 원본값은 검증용으로 함께 남긴다.
    """
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    curr_lbl = f"{year}-{month:02d}"
    prev_lbl = f"{prev_y}-{prev_m:02d}"

    out = raw[KEY_COLS].copy()
    out[f"{curr_lbl} 평균"] = _to_num(raw["당월평균(A)"])
    out[f"{prev_lbl} 평균"] = _to_num(raw["전월평균(B)"])
    out["전년동월평균"] = _to_num(raw["전년동월평균"])

    curr, prev = out[f"{curr_lbl} 평균"], out[f"{prev_lbl} 평균"]
    out["차이"] = curr - prev
    out["MoM(%)"] = (curr - prev) / prev * 100
    out["YoY(%)"] = (curr - out["전년동월평균"]) / out["전년동월평균"] * 100
    out["등락률(KAMIS)"] = _to_num(raw["등락률(A/B)"])

    return out


def fetch_livestock_mom(year: int, month: int, verbose: bool = True,
                        all_specs: bool = False) -> pd.DataFrame:
    """
    EKAPE 축산물 MoM을 가져와 KAMIS 테이블과 같은 열 구조로 맞춘다.
      축종      -> 품목
      부위/규격 -> 품종
      부류      -> '축산물' 고정
    KAMIS 고유 열인 '등락률(KAMIS)'은 해당 값이 없으므로 비워 둔다.
    """
    if ekape is None:
        print("   ⚠️ ekape_livestock_price 모듈을 찾을 수 없어 축산물을 건너뜁니다.")
        return pd.DataFrame()

    if verbose:
        print("🔄 EKAPE 축산물 소비자가격 수집 중...")

    try:
        res = ekape.get_price_mom(year, month, verbose=False, all_specs=all_specs)
    except Exception as e:
        print(f"   ⚠️ 축산물 수집 실패 — 건너뜁니다: {e}")
        return pd.DataFrame()

    df = res["mom"].rename(columns={"축종": "품목", "부위/규격": "품종"}).copy()
    df.insert(0, "부류", "축산물")
    df["등락률(KAMIS)"] = float("nan")   # dtype을 float으로 맞춰 concat 경고를 피한다

    if verbose:
        print(f"   ✔️ 축산물 {len(df)}행 수집 완료")

    return df


# ----------------------------------------------------------------------
# 4. CPI 가중치 매칭 및 가중합산
# ----------------------------------------------------------------------
def load_weights(path: str = None, sheet: str = WEIGHT_SHEET) -> pd.DataFrame:
    """
    가중치 시트를 읽는다.
      A열 품목        - CPI 품목명
      B열 kamis 품목명 - 수집 결과에서 찾을 이름 (비어 있을 수 있음)
      C열 가중치
    """
    path = path or os.path.join(BASE_DIR, WEIGHT_FILE)

    df = pd.read_excel(path, sheet_name=sheet, header=None, skiprows=1,
                       usecols=[0, 1, 2], names=["품목", "kamis명", "가중치"])
    df["가중치"] = pd.to_numeric(df["가중치"], errors="coerce")

    return df.dropna(subset=["품목", "가중치"]).reset_index(drop=True)


def _resolve_names(name_a, name_b, available: set):
    """
    CPI 품목명 하나를 수집 결과의 '품목' 이름 목록으로 해석한다.

      0) EXPLICIT_MATCH 에 지정된 항목이면 그 목록을 쓰고        -> 기준 '지정'
      1) A열(품목)과 완전히 일치하는 항목이 있으면 그것을 쓰고    -> 기준 'A'
      2) 없으면 B열(kamis 품목명)과 일치하는 항목을 찾고          -> 기준 'B'
      3) 그래도 없고 CONTAINS_MATCH 대상이면 부분일치로 묶고      -> 기준 '포함'
      4) 아무것도 없으면 빈 목록                                  -> 기준 '-'

    반환: (수집 품목명 리스트, 매칭 기준 문자열)
    """
    if name_a in EXPLICIT_MATCH:
        names = [n for n in EXPLICIT_MATCH[name_a] if n in available]
        if names:
            return names, "지정"

    if name_a in available:
        return [name_a], "A"

    if pd.notna(name_b) and name_b in available:
        return [name_b], "B"

    if name_a in CONTAINS_MATCH:
        pattern = CONTAINS_MATCH[name_a]
        names = sorted(n for n in available if pattern in n)
        if names:
            return names, "포함"

    return [], "-"


def match_weights(mom: pd.DataFrame, weights: pd.DataFrame) -> pd.DataFrame:
    """
    가중치 항목을 수집 결과의 '품목'에 매칭한다.

    한 CPI 품목에 여러 수집 행이 걸리면(품종·등급·부위별) 그 행들의 MoM을
    단순평균한다. 예) 고등어 = 국산(염장) + 국산(신선냉장) 大/中 + 수입산(염장)
                     국산쇠고기 = 안심·등심·양지·갈비·설도 5개 부위

    평균 대상 행의 단위가 서로 다르면(예: 1손 vs 1마리) 가격 평균은 의미가
    없으므로 단위를 '혼합'으로 표시하고 가격 열은 비워 둔다. MoM 평균은 유효.
    """
    ym_cols = sorted(c for c in mom.columns if _YM_COL.match(str(c)))
    if len(ym_cols) < 2:
        raise RuntimeError(f"월별 평균 열을 찾지 못했습니다: {list(mom.columns)}")
    prev_col, curr_col = ym_cols[0], ym_cols[-1]

    available = set(mom["품목"].dropna())
    nan = float("nan")

    rows = []
    for _, w in weights.iterrows():
        names, via = _resolve_names(w["품목"], w["kamis명"], available)

        sub = mom[mom["품목"].isin(names)] if names else mom.iloc[:0]
        valid = sub[sub["MoM(%)"].notna()]          # 전월값이 없는 행은 평균에서 제외

        units = list(dict.fromkeys(valid["단위"].dropna()))
        one_unit = len(units) == 1

        rows.append({
            "품목(CPI)": w["품목"],
            "매칭명": ", ".join(names),
            "기준": via,
            "n": len(valid),
            "부류": ", ".join(dict.fromkeys(sub["부류"])) if len(sub) else "",
            "단위": units[0] if one_unit else ("혼합" if units else ""),
            prev_col: valid[prev_col].mean() if one_unit else nan,
            curr_col: valid[curr_col].mean() if one_unit else nan,
            "MoM(%)": valid["MoM(%)"].mean() if len(valid) else nan,
            "가중치": w["가중치"],
        })

    return pd.DataFrame(rows)


def load_detail_index(path: str = None, sheet: str = DETAIL_SHEET) -> pd.DataFrame:
    """
    'detail' 시트의 품목별 CPI 원지수 시계열을 읽는다.
    2행이 헤더(A:품목, B:가중치, C:근원, D:생활물가, E열부터 월별 날짜)이고
    3행부터 품목별 원지수가 들어 있다.

    반환: index=품목, columns='YYYY-MM', values=원지수
    """
    path = path or os.path.join(BASE_DIR, WEIGHT_FILE)
    raw = pd.read_excel(path, sheet_name=sheet, header=None)

    header = raw.iloc[1]
    date_cols = {}
    for i in range(4, raw.shape[1]):
        ts = pd.to_datetime(header[i], errors="coerce")
        if pd.notna(ts):
            date_cols[i] = f"{ts.year}-{ts.month:02d}"

    if not date_cols:
        raise RuntimeError("detail 시트에서 월별 날짜 열을 찾지 못했습니다.")

    body = raw.iloc[2:]
    data = body[list(date_cols)].apply(pd.to_numeric, errors="coerce")
    data.columns = list(date_cols.values())
    data.index = body[0].astype(str).str.strip()

    return data[~data.index.duplicated(keep="first")]


def seasonal_mom(detail: pd.DataFrame, year: int, month: int,
                 n_years: int = SEASONAL_YEARS):
    """
    같은 달 전환의 최근 n개년 MoM 평균을 구한다.

    기준월이 2026-07이면 2025-07/2025-06, 2024-07/2024-06, 2023-07/2023-06의
    MoM을 각각 구해 단순평균한다. 두 달 중 하나라도 값이 없는 연도는 건너뛴다.

    반환: (품목별 평균 MoM(%) Series, 실제로 사용된 연도 리스트)
    """
    series, used = {}, []
    for k in range(1, n_years + 1):
        y = year - k
        py, pm = (y - 1, 12) if month == 1 else (y, month - 1)
        curr, prev = f"{y}-{month:02d}", f"{py}-{pm:02d}"

        if curr in detail.columns and prev in detail.columns:
            series[y] = (detail[curr] - detail[prev]) / detail[prev] * 100
            used.append(y)

    if not series:
        return pd.Series(dtype=float), []

    return pd.concat(series.values(), axis=1).mean(axis=1), used


def fill_missing_seasonal(matched: pd.DataFrame, detail: pd.DataFrame,
                          year: int, month: int, n_years: int = SEASONAL_YEARS):
    """
    MoM을 못 구한 항목을 최근 n개년 같은 달 평균 MoM으로 채운다.
    매칭 자체가 안 된 항목과, 이름은 맞았지만 전월값이 없어 MoM이 안 나온
    계절 품목 모두가 대상이다.

    '산출근거' 열에 실측/보완 여부를 남긴다.
    """
    out = matched.copy()
    avg, used = seasonal_mom(detail, year, month, n_years)

    out["산출근거"] = "실측"
    out.loc[out["MoM(%)"].isna(), "산출근거"] = "-"

    if avg.empty:
        return out, []

    tag = f"{n_years}Y평균({'·'.join(str(y) for y in sorted(used))})"
    need = out["MoM(%)"].isna()
    fill = out.loc[need, "품목(CPI)"].map(avg)

    out.loc[need, "MoM(%)"] = fill.values
    out.loc[need & out["MoM(%)"].notna(), "산출근거"] = tag

    return out, used


def build_weighted_mom(matched: pd.DataFrame):
    """
    매칭 테이블 -> 농축수산물 그룹 가중합산 MoM.

        그룹 MoM = Σ(품목 MoM × 가중치) / Σ(매칭된 품목의 가중치)

    매칭에 실패했거나 MoM을 못 구한 항목은 분자·분모 모두에서 빠진다
    (= 매칭된 항목만으로 재정규화). 석유류와 마찬가지로 지수 합성이 아닌
    변동률의 가중평균이라 엄밀한 값은 아니다.

    반환: (기여도가 추가된 테이블, 그룹 MoM(%), 커버리지 dict)
    """
    out = matched.copy()
    valid = out["MoM(%)"].notna()

    matched_w = out.loc[valid, "가중치"].sum()
    total_w = out["가중치"].sum()

    out["기여도(%p)"] = float("nan")
    out.loc[valid, "기여도(%p)"] = out.loc[valid, "MoM(%)"] * out.loc[valid, "가중치"] / matched_w

    group_mom = out.loc[valid, "기여도(%p)"].sum()

    coverage = {
        "매칭 항목수": int(valid.sum()),
        "전체 항목수": len(out),
        "매칭 가중치": matched_w,
        "전체 가중치": total_w,
        "가중치 커버리지(%)": matched_w / total_w * 100,
    }
    return out, group_mom, coverage


def get_price_mom(year: int = None, month: int = None, verbose: bool = True,
                  top_n: int = 10, include_livestock: bool = True,
                  livestock_all_specs: bool = True,
                  weight_file: str = None, fill_seasonal: bool = True):
    """
    메인 진입점.
    year/month를 주면 그 달을 기준월로 삼아 '직전월 대비 기준월' MoM을 산출한다.
    (기준월이 이미 마감된 달이면 월 전체 평균, 진행 중인 달이면 최신 조사일까지의
     부분 평균이 KAMIS 월간 일람표에 담겨 온다.)
    주지 않으면 KAMIS 최신 조사월을 자동으로 사용한다.
    include_livestock=True면 EKAPE 축산물을 같은 기준월로 받아 함께 붙인다.
    가중합산에서 축종별 전 부위를 평균내야 하므로 livestock_all_specs 기본값은 True.

    반환: dict
      'raw'        - KAMIS 원본 일람표
      'mom'        - 농·수산물 + 축산물 통합 MoM 테이블
      'livestock'  - 축산물만 추린 MoM 테이블
      'weighted'   - CPI 가중치 매칭 및 기여도 테이블
      'group_mom'  - 농축수산물 가중합산 MoM(%)
      'coverage'   - 매칭 커버리지 요약
      'year', 'month', 'survey_date'
      'latest_year', 'latest_month' - KAMIS 최신 조사월
      'is_partial' - 기준월이 아직 진행 중이라 부분 평균인지 여부
    """
    session = _session()

    # 최신 조사월과 최신 조사일은 서로 독립인 조회라 함께 던진다.
    # (세션은 스레드끼리 공유하지 않는다 — 조사일 쪽은 자기 세션을 새로 만든다.)
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_survey = ex.submit(get_latest_survey_date)
        latest_y, latest_m = get_latest_month(session)
        try:
            survey_date = fut_survey.result()
        except Exception:
            survey_date = None      # 조사일은 표시용이라 없어도 산출은 진행한다

    if year is None or month is None:
        year, month = latest_y, latest_m
    elif (year, month) > (latest_y, latest_m):
        raise RuntimeError(
            f"기준월 {year}-{month:02d} 데이터가 아직 없습니다 "
            f"(KAMIS 최신 조사월 {latest_y}-{latest_m:02d})."
        )

    # 기준월이 아직 달력상 진행 중이면 부분 평균(속보치).
    # KAMIS 최신 조사월과 비교하면 안 된다 — 8월 초에도 최신 조사월은 7월(마감)이다.
    _today = date.today()
    is_partial = (year, month) == (_today.year, _today.month)

    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    curr_lbl = f"{year}-{month:02d}"
    prev_lbl = f"{prev_y}-{prev_m:02d}"

    if verbose:
        print(f"📅 기준월(당월)      : {curr_lbl}"
              f"{' (진행 중 · 속보치)' if is_partial else ' (마감)'}")
        print(f"   KAMIS 최신 조사월 : {latest_y}-{latest_m:02d}"
              + (f" (최신 조사일 {survey_date})" if survey_date else ""))
        print(f"   비교 기준        : {curr_lbl} vs {prev_lbl}")
        print()

    # KAMIS(농·수산물)와 EKAPE(축산물)는 아예 다른 사이트라 동시에 받는다.
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_live = (ex.submit(fetch_livestock_mom, year, month, verbose,
                              livestock_all_specs)
                    if include_livestock else None)

        raw = fetch_monthly_all(year, month, session)
        livestock = fut_live.result() if fut_live is not None else pd.DataFrame()

    mom = build_mom(raw, year, month)

    if not livestock.empty:
        mom = pd.concat([mom, livestock[mom.columns]], ignore_index=True)

    if verbose:
        n_ok = mom["MoM(%)"].notna().sum()

        print()
        print("=" * 104)
        print(f" 소매가격 MoM : {curr_lbl} vs {prev_lbl}  "
              f"(총 {len(mom)}행 / MoM 산출 가능 {n_ok}행)")
        print("=" * 104)
        with pd.option_context("display.max_rows", 300, "display.width", 200):
            print(mom.to_string(index=False, na_rep="-", formatters={
                f"{curr_lbl} 평균": lambda x: f"{x:,.0f}",
                f"{prev_lbl} 평균": lambda x: f"{x:,.0f}",
                "전년동월평균": lambda x: f"{x:,.0f}",
                "차이": lambda x: f"{x:+,.0f}",
                "MoM(%)": lambda x: f"{x:+.2f}",
                "YoY(%)": lambda x: f"{x:+.2f}",
                "등락률(KAMIS)": lambda x: f"{x:+.1f}",
            }))

        valid = mom.dropna(subset=["MoM(%)"])
        cols = KEY_COLS + [f"{prev_lbl} 평균", f"{curr_lbl} 평균", "MoM(%)"]

        print()
        print("=" * 104)
        print(f" 상승 상위 {top_n}")
        print("=" * 104)
        print(valid.nlargest(top_n, "MoM(%)")[cols].to_string(index=False))

        print()
        print("=" * 104)
        print(f" 하락 상위 {top_n}")
        print("=" * 104)
        print(valid.nsmallest(top_n, "MoM(%)")[cols].to_string(index=False))

        if not livestock.empty:
            print()
            print("=" * 104)
            print(f" 축산물 (EKAPE) : {curr_lbl} vs {prev_lbl}")
            print("=" * 104)
            print(livestock[cols].to_string(index=False))

    # ---- CPI 가중치 매칭 및 가중합산 ----
    weighted, group_mom, coverage = pd.DataFrame(), float("nan"), {}
    group_mom_actual, coverage_actual, used_years = float("nan"), {}, []
    try:
        weights = load_weights(weight_file)
        matched = match_weights(mom, weights)

        # 실측만으로 계산한 값 (보완 전)
        _, group_mom_actual, coverage_actual = build_weighted_mom(matched)

        if fill_seasonal:
            detail = load_detail_index(weight_file)
            matched, used_years = fill_missing_seasonal(matched, detail, year, month)

        weighted, group_mom, coverage = build_weighted_mom(matched)
    except Exception as e:
        print(f"\n   ⚠️ 가중치 적용 실패 — 건너뜁니다: {e}")

    if verbose and not weighted.empty:
        print()
        print("=" * 104)
        print(f" 🔥 농축수산물 가중치 매칭 : {curr_lbl} vs {prev_lbl}")
        print("=" * 104)
        with pd.option_context("display.max_rows", 200, "display.width", 200):
            print(weighted.to_string(index=False, na_rep="-", formatters={
                prev_lbl + " 평균": lambda x: f"{x:,.0f}",
                curr_lbl + " 평균": lambda x: f"{x:,.0f}",
                "MoM(%)": lambda x: f"{x:+.2f}",
                "가중치": lambda x: f"{x:,.1f}",
                "기여도(%p)": lambda x: f"{x:+.4f}",
            }))

        if "산출근거" in weighted.columns:
            filled = weighted[weighted["산출근거"].str.contains("Y평균", na=False)]
            if not filled.empty:
                print()
                print("-" * 104)
                print(f" 과거 {SEASONAL_YEARS}개년 평균으로 보완한 {len(filled)}개 항목 "
                      f"(가중치 {filled['가중치'].sum():.1f}) "
                      f"— 참고연도 {'·'.join(str(y) for y in sorted(used_years))}")
                print("-" * 104)
                print("  " + ", ".join(f"{r['품목(CPI)']} {r['MoM(%)']:+.2f}%"
                                       for _, r in filled.iterrows()))

        miss = weighted[weighted["MoM(%)"].isna()]
        if not miss.empty:
            print()
            print("-" * 104)
            print(f" 여전히 비어 있는 {len(miss)}개 항목 (가중치 {miss['가중치'].sum():.1f})")
            print("-" * 104)
            print("  " + ", ".join(f"{r['품목(CPI)']}({r['가중치']:g})"
                                   for _, r in miss.iterrows()))

        print()
        print("=" * 104)
        print(f" 🔥 농축수산물 가중합산 MoM : {group_mom:+.2f}%")
        print("=" * 104)
        print(f"   반영 항목        : {coverage['매칭 항목수']} / {coverage['전체 항목수']}개")
        print(f"   반영 가중치      : {coverage['매칭 가중치']:.1f} / "
              f"{coverage['전체 가중치']:.1f} "
              f"(커버리지 {coverage['가중치 커버리지(%)']:.1f}%)")
        if coverage_actual:
            print(f"   └ 실측만 기준    : {group_mom_actual:+.2f}% "
                  f"(항목 {coverage_actual['매칭 항목수']}개 / "
                  f"가중치 {coverage_actual['매칭 가중치']:.1f}, "
                  f"커버리지 {coverage_actual['가중치 커버리지(%)']:.1f}%)")
        print("   ※ 값이 없는 항목은 분자·분모에서 모두 제외한 재정규화 기준입니다.")

    if verbose:
        print()
        print("-" * 104)
        if is_partial:
            print(f"※ {curr_lbl}은 "
                  + (f"KAMIS {survey_date}까지의 " if survey_date else "")
                  + "부분 평균 기준(속보치)입니다.")
        else:
            print(f"※ {curr_lbl}은 마감된 달이라 월 전체 평균 기준입니다.")
        print("※ 농·수산물은 KAMIS 소매가격, 축산물은 EKAPE 소비자가격(전국 평균)에서 가져옵니다.")
        print("※ 두 출처는 조사 기관·방식이 달라 가격 수준을 직접 비교하지 마세요. MoM 비교용입니다.")
        print("=" * 104)

    return {"raw": raw, "mom": mom, "livestock": livestock,
            "weighted": weighted, "group_mom": group_mom, "coverage": coverage,
            "group_mom_actual": group_mom_actual, "coverage_actual": coverage_actual,
            "seasonal_years": used_years,
            "year": year, "month": month, "survey_date": survey_date,
            "latest_year": latest_y, "latest_month": latest_m,
            "is_partial": is_partial}


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # 사용법: python kamis_retail_price.py [기준월]   (예: python kamis_retail_price.py 2026-07)
    _y = _m = None
    if len(sys.argv) > 1:
        import opinet_daily_price as _op
        _y, _m = _op.parse_target_ym(sys.argv[1])

    result = get_price_mom(year=_y, month=_m)

    # 필요 시 저장
    # result["mom"].to_excel("KAMIS_소매가격_MoM.xlsx", index=False)
