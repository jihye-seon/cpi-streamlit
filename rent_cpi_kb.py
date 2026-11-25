# -*- coding: utf-8 -*-
"""
KB 주간 전세지수를 활용한 집세 CPI 당월 예측

검증 출처
  집세CPI_28주선행_2024이후.zip 의 validate_rent_cpi_audited.py 를 앱 접목용으로
  옮긴 것이다. 2024-01~2026-10 34개월 워크포워드에서 측정된 집세 CPI MAE(지수포인트):

      앱 현행 SARIMA(3,1,0)(0,0,1,12)   0.02129
      CPI 자기회귀 릿지만                0.01859
      + KB 가격 AR(2)   (이 모듈)        0.01822
      + 28주 전 수급 변화                0.01822   <- 기여도 소수점 6자리라 제외

  28주 수급 변수는 개선 효과가 없어 옮기지 않았다. 원 코드에는 남아 있다.

구조 (2단계)
  1단계  미공개 주의 전세지수 로그변화율을 직전 2주 변화율로 회귀(AR(2))해 외삽하고,
         당월 공개주 실제값과 미공개주 예측값을 평균해 KB 월평균을 만든다.
  2단계  CPI 최근 변화율 2개(cpi_last, cpi_3m)와 KB 월평균의 3개월 평균 로그변화율
         (kb_smooth)을 릿지 회귀에 넣어 당월 CPI 변화율을 추정하고, 마지막 발표
         CPI에 적용해 지수를 복원한다. 학습표본은 2019-01부터 예측월 직전까지
         매월 확대하고, 표준화 기준도 그때까지의 학습자료에서 다시 계산한다.

데이터 신선도
  KB 지연을 1~52주로 늘려 재측정한 결과 MAE는 0.01822(최신) -> 0.01832(26주)
  -> 0.01901(39주) -> 0.01913(52주)였다. 26주까지는 사실상 영향이 없고,
  39주를 넘으면 KB를 쓰지 않은 릿지(0.01859)보다 나빠진다. 그래서 26주 경고,
  39주 사용중단 임계를 둔다.
"""

import os

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KB_CSV = os.path.join(BASE_DIR, "kb_jeonse.csv")

PRICE_COL = "전세지수"
SUPPLY_COL = "전세수급지수"

# 2단계 학습표본 시작월 (원 검증과 동일)
TRAINING_START = "2019-01-01"
# 릿지 정규화 계수 (원 검증과 동일)
RIDGE_LAMBDA = 5.0
# 2단계 특징
FEATURES = ["cpi_last", "cpi_3m", "kb_smooth"]
# 학습에 필요한 최소 표본 수
MIN_TRAIN_MONTHS = 36
# KB 공개 지연 가정 (기준일 + N일)
KB_DELAY_DAYS = 4
# CPI 발표일 기본 가정 (익월 N일) — 아래 달력에 없는 달에 적용
CPI_DEFAULT_RELEASE_DAY = 7

# KB 신선도 임계 (주)
STALE_WARN_WEEKS = 26
STALE_DISABLE_WEEKS = 39

# ----------------------------------------------------------------------
# CPI 실제 발표일
#
# 없는 달은 '익월 7일'로 떨어진다. 통계청 공표일정이 7일과 며칠 어긋나면
# 예측 시점의 정보집합이 달라지므로, 해가 바뀌면 여기에 추가한다.
# (키 = 조사 대상월 1일, 값 = 발표일)
# ----------------------------------------------------------------------
CPI_RELEASE_CALENDAR = {
    "2026-01-01": "2026-02-03", "2026-02-01": "2026-03-06",
    "2026-03-01": "2026-04-02", "2026-04-01": "2026-05-06",
    "2026-05-01": "2026-06-02", "2026-06-01": "2026-07-02",
    "2026-07-01": "2026-08-04", "2026-08-01": "2026-09-02",
    "2026-09-01": "2026-10-02", "2026-10-01": "2026-11-03",
}


class RentKBError(RuntimeError):
    """KB 기반 집세 예측을 낼 수 없는 상태 (앱은 SARIMA로 되돌아간다)."""


# ----------------------------------------------------------------------
# 입력
# ----------------------------------------------------------------------
def load_kb(path=None):
    """kb_jeonse.csv -> (전세지수 Series, 전세수급지수 Series). 월요일 기준일만 허용."""
    path = path or KB_CSV
    if not os.path.exists(path):
        raise RentKBError(
            "KB 자료 파일이 없습니다: " + os.path.basename(path)
            + "\nextract_kb_jeonse.py 를 실행해 kb.db에서 추출하세요.")
    try:
        raw = pd.read_csv(path, index_col=0, parse_dates=True, encoding="utf-8-sig")
    except Exception as exc:
        raise RentKBError("KB 자료 파일을 읽을 수 없습니다: %s" % exc)

    if PRICE_COL not in raw.columns:
        raise RentKBError("'%s' 열이 없습니다. (열: %s)" % (PRICE_COL, list(raw.columns)))

    price = pd.to_numeric(raw[PRICE_COL], errors="coerce").dropna()
    if SUPPLY_COL in raw.columns:
        supply = pd.to_numeric(raw[SUPPLY_COL], errors="coerce").dropna()
    else:
        supply = pd.Series(dtype=float)

    if price.empty:
        raise RentKBError("'%s' 자료가 비어 있습니다." % PRICE_COL)
    if not price.index.is_unique or not price.index.is_monotonic_increasing:
        raise RentKBError("KB 기준일이 중복이거나 정렬되어 있지 않습니다.")
    if (price <= 0).any() or not np.isfinite(price).all():
        raise RentKBError("KB 전세지수에 0 이하 또는 결측이 있습니다.")
    if not (price.index.dayofweek == 0).all():
        raise RentKBError("KB 기준일이 월요일이 아닙니다.")
    return price, supply


def to_month_start(series):
    """
    앱 엑셀의 월말 인덱스(2026-10-31)를 월초(2026-10-01)로 맞춘다.

    원 검증 코드가 월초 연속 계열을 전제로 하고, 발표일 계산도 월초 기준이다.
    """
    out = pd.to_numeric(pd.Series(series).dropna(), errors="coerce").dropna()
    out.index = pd.DatetimeIndex(out.index).to_period("M").to_timestamp()
    out = out[~out.index.duplicated(keep="last")].sort_index()
    if out.empty:
        raise RentKBError("집세 CPI 계열이 비어 있습니다.")
    expected = pd.date_range(out.index.min(), out.index.max(), freq="MS")
    if not out.index.equals(expected):
        missing = expected.difference(out.index)
        raise RentKBError("집세 CPI에 결측월이 있습니다: %s"
                          % [d.strftime("%Y-%m") for d in missing[:5]])
    return out


def availability(price, cpi):
    """각 관측의 공개일을 돌려준다. 예측 시점에 무엇을 알 수 있었는지 가른다."""
    kb_release = pd.Series(price.index + pd.Timedelta(days=KB_DELAY_DAYS),
                           index=price.index)
    cpi_release = pd.Series(
        cpi.index + pd.offsets.MonthBegin(1)
        + pd.Timedelta(days=CPI_DEFAULT_RELEASE_DAY - 1), index=cpi.index)
    known = pd.Series({pd.Timestamp(k): pd.Timestamp(v)
                       for k, v in CPI_RELEASE_CALENDAR.items()})
    cpi_release.update(known)
    return kb_release, cpi_release


# ----------------------------------------------------------------------
# 1단계 — 주간 전세지수 경로
# ----------------------------------------------------------------------
def weekly_path(visible, month):
    """
    month 의 주간 전세지수 경로를 만든다.
    공개된 주는 실제값, 미공개 주는 AR(2) 외삽값.
    반환: ([(기준일, 값, 'available'|'forecast')], 마지막 공개 실제값)
    """
    history = 100 * np.log(visible.asfreq("W-MON"))
    growth = history.diff()
    data = pd.concat([growth, growth.shift(), growth.shift(2)], axis=1)
    train = data.dropna()
    if len(train) < 30:
        raise RentKBError("AR(2)를 적합할 주간 관측이 부족합니다(30주 미만).")

    matrix = train.to_numpy()
    coef = np.linalg.lstsq(np.column_stack([np.ones(len(train)), matrix[:, 1:]]),
                           matrix[:, 0], rcond=None)[0]

    opens = pd.date_range(month, month + pd.offsets.MonthEnd(), freq="W-MON")
    if len(opens) == 0:
        raise RentKBError("%s에 월요일 기준일이 없습니다." % month.strftime("%Y-%m"))

    known = history.dropna()
    # 결측주가 섞여 있으면 주당 변화율로 환산해 모멘텀을 잡는다
    momentum = known.diff() / (known.index.to_series().diff().dt.days / 7)
    r1, r2 = momentum.iloc[-1], momentum.iloc[-2]
    level = known.iloc[-1]

    forecasts = {}
    for day in pd.date_range(known.index[-1] + pd.Timedelta(weeks=1),
                             opens[-1], freq="W-MON"):
        rate = float(coef @ [1.0, r1, r2])
        level += rate
        forecasts[day] = float(np.exp(level / 100))
        r1, r2 = rate, r1

    points = [(day, float(visible.at[day]), "available") if day in visible.index
              else (day, forecasts[day], "forecast") for day in opens
              if day in visible.index or day in forecasts]
    if not points:
        raise RentKBError("%s의 주간 경로를 만들 수 없습니다." % month.strftime("%Y-%m"))
    return points, float(visible.iloc[-1])


# ----------------------------------------------------------------------
# 2단계 — 특징 생성과 릿지 회귀
# ----------------------------------------------------------------------
def make_row(price, cpi, kb_release, cpi_release, month, cutoff):
    """cutoff 시점에 알 수 있었던 정보만으로 month 의 특징 한 줄을 만든다."""
    visible = price.loc[kb_release <= cutoff]
    known = cpi.loc[cpi_release <= cutoff]
    if visible.empty:
        raise RentKBError("%s 시점에 공개된 KB 자료가 없습니다."
                          % cutoff.strftime("%Y-%m-%d"))
    if len(known) < 4:
        raise RentKBError("CPI 과거 관측이 4개월 미만입니다.")
    if known.index[-1] >= month:
        raise RentKBError("%s 시점에 이미 %s CPI가 발표되어 있습니다(예측 대상월이 아님)."
                          % (cutoff.strftime("%Y-%m-%d"), month.strftime("%Y-%m")))

    points, _ = weekly_path(visible, month)
    kb = float(np.mean([v for _, v, _ in points]))

    # 1개월 전·3개월 전 KB 월평균도 같은 정보집합으로 재구성한다
    prior = []
    for back in (1, 3):
        values, _ = weekly_path(visible, month - pd.DateOffset(months=back))
        prior.append(float(np.mean([v for _, v, _ in values])))
    last, third = prior

    last_cpi = float(known.iloc[-1])
    return dict(
        month=month, cutoff=cutoff,
        anchor_month=known.index[-1], anchor=last_cpi,
        cpi_last=100 * np.log(last_cpi / float(known.iloc[-2])),
        cpi_3m=100 * np.log(last_cpi / float(known.iloc[-4])) / 3,
        kb_smooth=100 * np.log(kb / third) / 3,
        kb_pred=kb, kb_prev=last,
        kb_available_weeks=sum(s == "available" for _, _, s in points),
        kb_forecast_weeks=sum(s == "forecast" for _, _, s in points),
    )


def fit_predict(training, row, features=FEATURES):
    """표준화 + 릿지. 반환은 복원된 당월 지수."""
    x = training[list(features)].to_numpy(dtype=float)
    y = training["target"].to_numpy(dtype=float)
    mean = x.mean(axis=0)
    scale = np.maximum(x.std(axis=0), 1e-9)
    x = (x - mean) / scale
    coef = np.linalg.solve(x.T @ x + RIDGE_LAMBDA * np.eye(len(features)),
                           x.T @ (y - y.mean()))
    point = np.array([float(row[f]) for f in features])
    estimate = y.mean() + ((point - mean) / scale) @ coef
    return float(row["anchor"] * np.exp(estimate / 100))


# ----------------------------------------------------------------------
# 공개 API
# ----------------------------------------------------------------------
def forecast(cpi_series, kb_csv=None, asof=None):
    """
    집세 CPI 당월 예측.

    cpi_series - 앱의 집세 CPI 계열 (월말·월초 인덱스 모두 허용)
    kb_csv     - kb_jeonse.csv 경로 (생략하면 모듈과 같은 폴더)
    asof       - 예측 시점 (생략하면 오늘). 이 시점에 공개된 자료만 쓴다.

    반환 dict의 주요 키
      mom              집세 MoM (%)  <- 앱 입력값에 넣는 값
      index            예측 지수
      mom_cpi_only     KB를 쓰지 않은 릿지의 MoM (%) — 비교용
      anchor_month     앵커로 쓴 발표 CPI의 월
      kb_latest        KB 최신 기준주
      kb_stale_weeks   KB 경과 주수
      kb_available_weeks / kb_forecast_weeks   당월 공개주 / 외삽주
      usable           False면 너무 묵어서 쓰지 말아야 하는 상태
    """
    price, _ = load_kb(kb_csv)
    cpi = to_month_start(cpi_series)
    if asof is not None:
        asof = pd.Timestamp(asof).normalize()
    else:
        asof = pd.Timestamp.today().normalize()

    kb_release, cpi_release = availability(price, cpi)
    target = cpi.index[-1] + pd.DateOffset(months=1)

    # 예측 시점의 '일'을 학습 행과 예측 행에 똑같이 적용한다.
    # 원 검증은 15일 기준으로 측정됐다(MAE 0.01822). 1일 기준은 직전월 CPI가
    # 아직 발표되지 않아 앵커가 두 달 멀어지고 오차가 두 배(0.03572)로 뛴다.
    day = int(min(max(asof.day, 1), 28))

    rows = []
    for month in pd.date_range(TRAINING_START, target, freq="MS"):
        cutoff = month + pd.Timedelta(days=day - 1)
        if cutoff > asof:
            continue
        try:
            rows.append(make_row(price, cpi, kb_release, cpi_release, month, cutoff))
        except RentKBError:
            if month == target:
                raise
            continue              # 학습 구간의 개별 실패는 건너뛴다

    if not rows:
        raise RentKBError("학습 표본을 만들 수 없습니다.")

    table = pd.DataFrame(rows)
    table["target_release"] = table["month"].map(cpi_release)
    table["actual"] = table["month"].map(cpi)
    table["target"] = 100 * np.log(table["actual"] / table["anchor"])

    picked = table.loc[table["month"].eq(target)]
    if picked.empty:
        raise RentKBError(
            "%s 예측 행을 만들 수 없습니다 (예측 시점 %s이 너무 이릅니다)."
            % (target.strftime("%Y-%m"), asof.strftime("%Y-%m-%d")))
    row = picked.iloc[-1].to_dict()

    # 학습표본: 예측월 이전이고, 정답이 예측 시점에 이미 발표된 달만
    train = table.loc[table["month"].lt(target)
                      & table["target_release"].le(row["cutoff"])]
    train = train.dropna(subset=["target"])
    if len(train) < MIN_TRAIN_MONTHS:
        raise RentKBError("학습 표본이 부족합니다(%d개월, %d개월 필요)."
                          % (len(train), MIN_TRAIN_MONTHS))

    index_kb = fit_predict(train, row, FEATURES)
    index_cpi_only = fit_predict(train, row, ["cpi_last", "cpi_3m"])

    kb_latest = price.index[-1]
    stale_weeks = (asof - kb_latest).days / 7.0

    return dict(
        target_month=target,
        mom=(index_kb / row["anchor"] - 1) * 100,
        index=index_kb,
        mom_cpi_only=(index_cpi_only / row["anchor"] - 1) * 100,
        index_cpi_only=index_cpi_only,
        anchor_month=row["anchor_month"],
        anchor=row["anchor"],
        cutoff=row["cutoff"],
        train_count=len(train),
        kb_monthly_avg=row["kb_pred"],
        kb_latest=kb_latest,
        kb_stale_weeks=stale_weeks,
        kb_available_weeks=int(row["kb_available_weeks"]),
        kb_forecast_weeks=int(row["kb_forecast_weeks"]),
        stale_warn=stale_weeks > STALE_WARN_WEEKS,
        usable=stale_weeks <= STALE_DISABLE_WEEKS,
        release_calendar_covers_target=any(
            pd.Timestamp(k).year == target.year for k in CPI_RELEASE_CALENDAR),
    )
