import streamlit as st
import pandas as pd
import numpy as np
import datetime
#import sys
import plotly.graph_objects as go
import warnings
import os  # 파일 존재 여부 확인용 추가

# SARIMA 모델용 라이브러리
from statsmodels.tsa.statespace.sarimax import SARIMAX

warnings.filterwarnings("ignore")

# --- [1] 페이지 레이아웃 설정 ---
st.set_page_config(page_title="CPI 대시보드", layout="wide")

st.title("📊 한국 CPI 시뮬레이터 ")
st.markdown("한국 근월 CPI 예측 및 향후 경로의 시뮬레이션을 진행합니다.")
st.markdown("---")

# ==========================================
# 📂 [DATA LOADING] 내장형 엑셀 파일 자동 로드
# ==========================================
st.sidebar.header("📁 데이터 소스 정보")

# 💡 내장할 엑셀 파일명 지정 (app.py와 같은 폴더에 위치해야 합니다)
EXCEL_FILENAME = "CPI data_202605_v1.xlsx"

@st.cache_data
def process_embedded_excel(file_path):
    try:
        # 파일이 실제로 디렉토리에 존재하는지 검증
        if not os.path.exists(file_path):
            return None, None, None
            
        # cpi_data 로드 (날짜 가공 및 인덱스 설정)
        df_cpi = pd.read_excel(file_path, sheet_name="CPI_Data", header=0)
        df_cpi['Date'] = pd.to_datetime(df_cpi['Date'], errors='coerce')
        df_cpi = df_cpi.dropna(subset=['Date']).sort_values('Date')
        df_cpi.set_index('Date', inplace=True)
        
        # cpi_weights 로드 (딕셔너리 구조로 변환)
        df_weights = pd.read_excel(file_path, sheet_name="CPI_Weights", header=0)
        weights = df_weights.set_index(df_weights.columns[0])[df_weights.columns[1]].to_dict()
        
        core_component_items = []
        if len(df_weights.columns) > 2:
            core_flag_col = df_weights.columns[2]
            for _, row in df_weights.iterrows():
                item_name = row.iloc[0]
                flag_value = row.iloc[2]
                if pd.notna(flag_value) and str(flag_value).strip() in {'1', '1.0', 'True', 'true'}:
                    core_component_items.append(item_name)
        
        # Oil_krw_Data 시트 로드
        df_macro = pd.read_excel(file_path, sheet_name="Oil_krw_Data", header=0)
        df_macro['Date'] = pd.to_datetime(df_macro['Date'], errors='coerce')
        df_macro = df_macro.dropna(subset=['Date'])
        df_macro.set_index('Date', inplace=True)

        rename_macro_cols = {}
        for col in df_macro.columns:
            normalized_col = str(col).strip().lower().replace('/', '').replace('_', '')
            if normalized_col == 'wti':
                rename_macro_cols[col] = 'WTI'
            elif normalized_col in {'usdkrw', 'krwusd'}:
                rename_macro_cols[col] = 'USDKRW'
            elif normalized_col == 'brent':
                rename_macro_cols[col] = 'Brent'
        df_macro = df_macro.rename(columns=rename_macro_cols)
        
        macro_cols = ['WTI', 'Brent', 'USDKRW']
        available_macro_cols = [col for col in macro_cols if col in df_macro.columns]
        df_macro = df_macro[available_macro_cols]

        for col in ['WTI', 'USDKRW']:
            if col in df_macro.columns:
                diff_col = f'{col}_diff_lag0'
                lag_col = f'{col}_diff_lag1'
                lag2_col = f'{col}_diff_lag2'
                df_macro[diff_col] = df_macro[col].diff()
                df_macro[lag_col] = df_macro[diff_col].shift(1)
                df_macro[lag2_col] = df_macro[diff_col].shift(2)
        
        df_merged = df_cpi.join(df_macro, how='inner')
        return df_merged.sort_index(), weights, df_macro, core_component_items
        
    except Exception as e:
        st.sidebar.error(f"엑셀 구조 파싱 실패: {e}")
        return None, None, None, []

# 유저 업로드 인터페이스 없이, 서버 내부의 엑셀 파일을 자동 호출
df_hist, cpi_weights, df_macro_raw, core_component_items = process_embedded_excel(EXCEL_FILENAME)

# 깃허브에 파일이 누락되었거나 로드에 실패했을 때의 안내 가드레일
if df_hist is None:
    st.error(f"⚠️ 서버에서 데이터 파일(`{EXCEL_FILENAME}`)을 불러오지 못했습니다.")
    st.info("💡 **호스트 안내:** 깃허브 레포지토리에 엑셀 파일이 코드(`app.py`)와 같은 디렉토리에 정확히 업로드(Push)되어 있는지 확인해 주세요.")
    st.stop()
else:
    # 데이터 로드 완료 시 사이드바에 데이터 기준일 안내 메시지 출력
    last_date = df_hist.index[-1].strftime('%Y-%m-%d')
    st.sidebar.success(f"✅ 데이터 동기화 완료 (최신 기준일: {last_date})")



# 주요 변수 선언
last_date = df_hist.index[-1]
last_row = df_hist.iloc[-1]
target_date = last_date + pd.DateOffset(months=1)
target_month = target_date.month 


def get_macro_row_for_target(df_macro, target_dt):
    if df_macro is None or df_macro.empty:
        return None
    target_dt = pd.Timestamp(target_dt)
    if target_dt in df_macro.index:
        row = df_macro.loc[target_dt]
    else:
        row = df_macro.iloc[-1]

    if isinstance(row, pd.Series):
        return row.astype(float)
    return pd.Series({df_macro.columns[0]: float(row)})

macro_target_row = get_macro_row_for_target(df_macro_raw, target_date)

if "weights" not in st.session_state:
    try:
        st.session_state["weights"] = cpi_weights if cpi_weights else {}
    except Exception:
        st.session_state["weights"] = {}

        # 만약 임시로 테스트 중이라면 기본값 방어막 형성 (총합 1000)
        #st.session_state["weights"] = {
        #    '농축수산물': 75.6, '공업제품': 333.8, '전기수도가스': 33.7,
        #    '집세': 99.1, '공공서비스': 120, '개인서비스': 333.3,
        #    'Core': 765.9, 'Ex-core': 234.1, '가공식품': 82.7, '석유제품': 46.6,
        #    '이외공업제품': 209}
        
    except Exception:
        st.session_state["weights"] = {}
        
weights = st.session_state["weights"]

# 과거 등락률 매트릭스 계산
hist_mom_all = df_hist.pct_change() * 100

# ------------------------------------------
# Tab2 분류 구성 품목 풀(Pool)
# ------------------------------------------
volatile_items = ['농축수산물', '석유제품']
# 가공식품과 이외공업제품은 분리 유지 (가공식품은 근원 제외, 이외공업제품은 근원 포함)
core_items = ['이외공업제품', '전기수도가스', '집세', '공공서비스', '개인서비스']
total_index_items = ['농축수산물', '석유제품', '가공식품', '이외공업제품', '전기수도가스', '집세', '공공서비스', '개인서비스']

# Ensure display lists do not contain duplicate names (preserve order)
def _unique_preserve(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

all_display_items = _unique_preserve(['총지수', 'Core'] + volatile_items + ['가공식품', '이외공업제품'] + core_items)

BEST_ORDERS = {
    '이외공업제품': {'order': (0, 1, 2), 'seasonal_order': (0, 0, 0, 12)},
    '전기수도가스': {'order': (0, 2, 1), 'seasonal_order': (1, 0, 1, 12)},
    '집세': {'order': (3, 1, 0), 'seasonal_order': (0, 0, 1, 12)},
    '공공서비스': {'order': (0, 1, 1), 'seasonal_order': (1, 0, 0, 12)},
    '개인서비스': {'order': (0, 2, 1), 'seasonal_order': (1, 0, 1, 12)},
    '가공식품': {'order': (0, 2, 1), 'seasonal_order': (1, 0, 0, 12)},
    '이외공업제품': {'order': (0, 1, 2), 'seasonal_order': (0, 0, 0, 12)}
}


def get_sarima_order(item):
    if item in BEST_ORDERS:
        return BEST_ORDERS[item]['order'], BEST_ORDERS[item]['seasonal_order']
    return (1, 1, 1), (1, 1, 0, 12)


def format_sarima_order(item):
    order, seasonal_order = get_sarima_order(item)
    return f"적용 차수: order={order}, seasonal_order={seasonal_order}"


def normalize_component_weights(weight_map, target_items):
    normalized = {}
    raw_values = {item: float(weight_map.get(item, 0.0)) for item in target_items}
    raw_total = sum(raw_values.values())
    if raw_total <= 0:
        for item in target_items:
            normalized[item] = 0.0
        return normalized
    for item in target_items:
        normalized[item] = raw_values[item] * 1000.0 / raw_total
    return normalized


def resolve_core_component_items(candidate_items, available_columns, fallback_items):
    resolved = [item for item in candidate_items if item in available_columns]
    if resolved:
        return resolved
    return [item for item in fallback_items if item in available_columns]


def compute_weighted_component_series(df, weight_map, target_items):
    available_items = [item for item in target_items if item in df.columns]
    if not available_items:
        return pd.Series(index=df.index, dtype=float)
    component_weights = {item: float(weight_map.get(item, 0.0)) for item in available_items}
    total_weight = sum(component_weights.values())
    if total_weight <= 0:
        return pd.Series(index=df.index, dtype=float)
    weighted = pd.Series(0.0, index=df.index, dtype=float)
    for item in available_items:
        weighted += df[item] * component_weights[item]
    return weighted / total_weight


def get_core_history_series(df):
    if 'Core' in df.columns:
        return pd.to_numeric(df['Core'], errors='coerce')
    return pd.Series(index=df.index, dtype=float)


def build_monthly_avg_growths(df_mom, target_items):
    monthly_avg_growths = {}
    for item in target_items:
        monthly_avg_growths[item] = {}
        for month in range(1, 13):
            same_m_data = df_mom[df_mom.index.month == month]
            recent_three = same_m_data[item].dropna().tail(3)
            avg_mom = recent_three.mean() if not recent_three.empty else 0.0
            monthly_avg_growths[item][month] = float(avg_mom) if not np.isnan(avg_mom) else 0.0
    return monthly_avg_growths


def compute_tab2_pred_indices(last_row, rec_values, session_state, base_fx_value):
   
    mom_agri = float(session_state.get('agri_val', 0.0))
    mom_petro = float(session_state.get('petro_val', 0.0))
    mom_processed = float(session_state.get('processed_val', rec_values.get('가공식품', 0.0)))
    mom_other_manufactured = float(session_state.get('other_manuf_val', rec_values.get('이외공업제품', 0.0)))
    mom_utility = float(session_state.get('utility_val', rec_values['전기수도가스']))
    mom_housing = float(session_state.get('housing_val', rec_values['집세']))
    mom_public = float(session_state.get('public_val', rec_values['공공서비스']))
    mom_personal = float(session_state.get('personal_val', rec_values['개인서비스']))

    
    pred_mom_dict = {
        '농축수산물': mom_agri,
        '석유제품': mom_petro,
        '가공식품': mom_processed,
        '이외공업제품': mom_other_manufactured,
        '전기수도가스': mom_utility,
        '집세': mom_housing,
        '공공서비스': mom_public,
        '개인서비스': mom_personal
    }

    pred_indices = {}
    for item in pred_mom_dict.keys():
        pred_indices[item] = last_row[item] * (1 + pred_mom_dict[item] / 100)
    return pred_indices


def prepare_sarima_exog(df, item):
    petroleum_cols = ['WTI', 'WTI_diff_lag0', 'WTI_diff_lag1', 'WTI_diff_lag2']
    other_manufactured_cols = ['WTI_diff_lag1', 'WTI_diff_lag2', 'USDKRW_diff_lag1']
    processed_cols = ['USDKRW_diff_lag2']

    if item == '석유제품':
        if all(col in df.columns for col in petroleum_cols):
            return df[petroleum_cols].copy()
        return None
    if item == '이외공업제품':
        if all(col in df.columns for col in other_manufactured_cols):
            return df[other_manufactured_cols].copy()
        return None
    if item == '가공식품':
        if all(col in df.columns for col in processed_cols):
            return df[processed_cols].copy()
        return None
    if item == '개인서비스':
        return None
    if item == '전기수도가스':
        return None
    return None


def _get_latest_float(df, col, default=0.0):
    if col in df.columns and not df[col].dropna().empty:
        return float(df[col].dropna().iloc[-1])
    return float(default)


def _build_diff_forecast_exog(df, forecast_rows, lag_spec):
    value_cols = list(lag_spec.keys())
    history = {
        col: {
            'level': _get_latest_float(df, col, 0.0),
            'diffs': [
                _get_latest_float(df, f'{col}_diff_lag0', 0.0),
                _get_latest_float(df, f'{col}_diff_lag1', 0.0),
                _get_latest_float(df, f'{col}_diff_lag2', 0.0),
            ]
        }
        for col in value_cols
    }
    forecast_vals = []
    for row in forecast_rows:
        current_diffs = {}
        for col in value_cols:
            current_level = float(row.get(col, history[col]['level']))
            current_diffs[col] = current_level - history[col]['level']

        row_vals = []
        for col, lags in lag_spec.items():
            available_diffs = [current_diffs[col]] + history[col]['diffs']
            row_vals.extend(available_diffs[lag] for lag in lags)

        for col in value_cols:
            history[col]['level'] = float(row.get(col, history[col]['level']))
            history[col]['diffs'] = [current_diffs[col]] + history[col]['diffs'][:2]
        forecast_vals.append(row_vals)
    return np.array(forecast_vals)


def _build_level_diff_forecast_exog(df, forecast_rows, value_col, diff_lags=(0, 1, 2)):
    history = {
        'level': _get_latest_float(df, value_col, 0.0),
        'diffs': [
            _get_latest_float(df, f'{value_col}_diff_lag0', 0.0),
            _get_latest_float(df, f'{value_col}_diff_lag1', 0.0),
            _get_latest_float(df, f'{value_col}_diff_lag2', 0.0),
        ]
    }
    forecast_vals = []
    for row in forecast_rows:
        current_level = float(row.get(value_col, history['level']))
        current_diff = current_level - history['level']
        available_diffs = [current_diff] + history['diffs']
        forecast_vals.append([current_level] + [available_diffs[lag] for lag in diff_lags])
        history['level'] = current_level
        history['diffs'] = [current_diff] + history['diffs'][:2]
    return np.array(forecast_vals)


def prepare_sarima_forecast_exog(df, item, steps=1, forecast_macro_row=None):
    if forecast_macro_row is None:
        forecast_macro_row = df.iloc[-1]

    if isinstance(forecast_macro_row, pd.Series):
        forecast_rows = [forecast_macro_row]
    elif isinstance(forecast_macro_row, (list, tuple, np.ndarray)):
        forecast_rows = list(forecast_macro_row)
        if len(forecast_rows) < steps:
            forecast_rows = forecast_rows + [forecast_rows[-1]] * (steps - len(forecast_rows))
        forecast_rows = forecast_rows[:steps]
    else:
        forecast_rows = [forecast_macro_row] * steps

    if item == '석유제품':
        return _build_level_diff_forecast_exog(df, forecast_rows, 'WTI', diff_lags=(0, 1, 2))
    if item == '이외공업제품':
        return _build_diff_forecast_exog(df, forecast_rows, {'WTI': [1, 2], 'USDKRW': [1]})
    if item == '가공식품':
        return _build_diff_forecast_exog(df, forecast_rows, {'USDKRW': [2]})
    if item == '개인서비스':
        return None
    if item == '전기수도가스':
        return None
    return None

# ==========================================
# 🔍 SARIMA 모델 진단용 함수
def sarima_model_diagnostics(df, item, forecast_macro_rows=None):
    exog = prepare_sarima_exog(df, item)

    series = df[[item]].copy()
    if exog is not None:
        combined = series.join(exog, how='inner').dropna()
        if combined.empty:
            return {
                'item': item,
                'warning': '훈련 데이터가 충분하지 않습니다.',
                'observations': 0
            }
        endog = combined[item]
        exog_train = combined.drop(columns=[item])
    else:
        endog = series[item].dropna()
        exog_train = None

    order, seasonal_order = get_sarima_order(item)
    model = SARIMAX(endog,
                    exog=exog_train,
                    order=order,
                    seasonal_order=seasonal_order,
                    enforce_stationarity=False,
                    enforce_invertibility=False)
    fit = model.fit(disp=False)

    params = fit.params.to_dict()
    pvalues = fit.pvalues.to_dict()
    stderr = fit.bse.to_dict()

    conf_int_raw = fit.conf_int()
    if isinstance(conf_int_raw, pd.DataFrame):
        ci_lower = conf_int_raw.iloc[:, 0].to_dict()
        ci_upper = conf_int_raw.iloc[:, 1].to_dict()
    else:
        ci_lower = {}
        ci_upper = {}
        try:
            ci_dict = conf_int_raw.to_dict()
            if isinstance(ci_dict, dict) and ci_dict:
                if all(isinstance(v, dict) for v in ci_dict.values()):
                    ci_items = list(ci_dict.values())
                    if len(ci_items) >= 2:
                        ci_lower = ci_items[0]
                        ci_upper = ci_items[1]
                    elif ci_items:
                        ci_lower = ci_items[0]
                        ci_upper = ci_items[0]
                else:
                    ci_lower = ci_dict
                    ci_upper = ci_dict
        except Exception:
            ci_lower = {}
            ci_upper = {}

    summary_df = pd.DataFrame({
        'coef': pd.Series(params),
        'stderr': pd.Series(stderr),
        't': pd.Series(fit.tvalues),
        'pvalue': pd.Series(pvalues),
        'ci_lower': pd.Series(ci_lower),
        'ci_upper': pd.Series(ci_upper),
    })

    forecast_exog = None
    if forecast_macro_rows is not None:
        forecast_exog = prepare_sarima_forecast_exog(df, item, steps=12, forecast_macro_row=forecast_macro_rows)

    exog_train_rows = exog_train.shape[0] if exog_train is not None else 0
    exog_train_columns = list(exog_train.columns) if exog_train is not None else []

    return {
        'item': item,
        'observations': int(fit.nobs),
        'aic': float(fit.aic),
        'bic': float(fit.bic),
        'params': params,
        'pvalues': pvalues,
        'summary_df': summary_df,
        'forecast_exog': forecast_exog,
        'exog_train_rows': exog_train_rows,
        'exog_train_columns': exog_train_columns
    }

@st.cache_data
def build_sarima_coeff_results(df, target_items):
    coeff_results = {}
    for item in target_items:
        diag = sarima_model_diagnostics(df, item)
        if not diag or 'summary_df' not in diag:
            continue

        summary_df = diag['summary_df'].copy()
        summary_df.index.name = 'variable'
        summary_df = summary_df.reset_index()
        summary_df['item'] = item
        summary_df['significant'] = summary_df['pvalue'] < 0.05
        coeff_results[item] = summary_df
    return coeff_results

# ==========================================
# 🔮 [💡 백엔드 1] SARIMA 모델 예측 함수 (기조적 품목 대상)
# ==========================================
@st.cache_data
def predict_items_with_sarima(df, target_items, forecast_macro_row=None):
    sarima_results = {}
    for item in target_items:
        series = df[[item]].copy()
        exog = prepare_sarima_exog(df, item)
        if exog is not None:
            combined = series.join(exog, how='inner').dropna()
            endog = combined[item]
            exog_train = combined.drop(columns=[item])
        else:
            endog = series[item].dropna()
            exog_train = None

        order, seasonal_order = get_sarima_order(item)
        model = SARIMAX(endog,
                        exog=exog_train,
                        order=order,
                        seasonal_order=seasonal_order,
                        enforce_stationarity=False,
                        enforce_invertibility=False)
        model_fit = model.fit(disp=False)
        
        if exog_train is not None:
            exog_forecast = prepare_sarima_forecast_exog(df, item, steps=1, forecast_macro_row=forecast_macro_row)
            pred_index = model_fit.forecast(steps=1, exog=exog_forecast).iloc[0]
        else:
            pred_index = model_fit.forecast(steps=1).iloc[0]

        last_val = endog.iloc[-1]
        pred_mom = ((pred_index - last_val) / last_val) * 100
        sarima_results[item] = float(pred_mom)
    return sarima_results

# ==========================================
# 📊 [💡 백엔드 2] 과거 3개년 동일 월 평균 MoM 계산 함수
# ==========================================
def calculate_3yr_seasonal_avg(df_mom, target_m, target_items):
    seasonal_avg_results = {}
    same_month_data = df_mom[df_mom.index.month == target_m]
    recent_3yrs = same_month_data.tail(3)
    
    for item in target_items:
        avg_mom = recent_3yrs[item].mean()
        seasonal_avg_results[item] = float(avg_mom) if not np.isnan(avg_mom) else 0.0
    return seasonal_avg_results

# 사이드바 연산 구동
with st.sidebar:
    with st.spinner("🤖 추세 알고리즘 연산 중..."):
        # Include '가공식품' in the precomputed defaults so Tab2 shows a default for it
        sarima_target_items = [it for it in (core_items + ['가공식품']) if it in df_hist.columns]
        sarima_preds = predict_items_with_sarima(df_hist, sarima_target_items, forecast_macro_row=macro_target_row)
        seasonal_preds = calculate_3yr_seasonal_avg(hist_mom_all, target_month, sarima_target_items)
        sarima_coeff_results = build_sarima_coeff_results(df_hist, sarima_target_items)
    st.success("✅ 연산 완료")


# ==========================================
# 🔄 세션 상태(Session State) 초기화 및 매핑
# ==========================================
base_fx = float(round(
    macro_target_row.get('USDKRW', last_row.get('USDKRW', 0.0)) if macro_target_row is not None else last_row.get('USDKRW', 0.0),
    1
))

if "model_choice" not in st.session_state:
    st.session_state.model_choice = "🤖 SARIMA 모델"

st.subheader("⚙️ 모델 선택")
current_model = st.radio(
    "어떤 방법론을 기본값으로 기조적 품목 시뮬레이션을 진행하시겠습니까?",
    ["🤖 시계열 모델(SARIMA)", "🗓️ 3개년 동월 평균 MoM"],
    horizontal=True,
    key="model_choice"
)

if current_model == "🤖 시계열 모델(SARIMA)":
    rec_values = sarima_preds
else:
    rec_values = seasonal_preds

# 최초 세션 세팅 (새로운 한글 대분류 반영)
if "agri_val" not in st.session_state: st.session_state.agri_val = 0.0
if "petro_val" not in st.session_state: st.session_state.petro_val = 0.0


if "processed_val" not in st.session_state: st.session_state.processed_val = float(rec_values.get('가공식품', 0.0))
if "other_manuf_val" not in st.session_state: st.session_state.other_manuf_val = float(rec_values.get('이외공업제품', 0.0))
if "utility_val" not in st.session_state: st.session_state.utility_val = float(rec_values['전기수도가스'])
if "housing_val" not in st.session_state: st.session_state.housing_val = float(rec_values['집세'])
if "public_val" not in st.session_state: st.session_state.public_val = float(rec_values['공공서비스'])
if "personal_val" not in st.session_state: st.session_state.personal_val = float(rec_values['개인서비스'])

# 모델 변경감지 로직
if "prev_model" not in st.session_state:
    st.session_state.prev_model = current_model

if st.session_state.prev_model != current_model:
    st.session_state.agri_val = 0.0
    st.session_state.petro_val = 0.0
    st.session_state.processed_val = float(rec_values.get('가공식품', 0.0))
    st.session_state.other_manuf_val = float(rec_values.get('이외공업제품', 0.0))
    st.session_state.utility_val = float(rec_values['전기수도가스'])
    st.session_state.housing_val = float(rec_values['집세'])
    st.session_state.public_val = float(rec_values['공공서비스'])
    st.session_state.personal_val = float(rec_values['개인서비스'])
    st.session_state.prev_model = current_model
    st.session_state["tab2_methodology"] = current_model
    st.rerun()

if "tab2_methodology" not in st.session_state:
    st.session_state["tab2_methodology"] = current_model

# precise control sync callbacks for volatility sliders
def sync_agri_from_slider():
    st.session_state.agri_val = float(st.session_state.agri_val_slider)
    st.session_state.agri_val_exact = float(st.session_state.agri_val_slider)

def sync_agri_from_exact():
    st.session_state.agri_val = float(st.session_state.agri_val_exact)
    st.session_state.agri_val_slider = float(st.session_state.agri_val_exact)

def sync_petro_from_slider():
    st.session_state.petro_val = float(st.session_state.petro_val_slider)
    st.session_state.petro_val_exact = float(st.session_state.petro_val_slider)

def sync_petro_from_exact():
    st.session_state.petro_val = float(st.session_state.petro_val_exact)
    st.session_state.petro_val_slider = float(st.session_state.petro_val_exact)

if "agri_val_slider" not in st.session_state:
    st.session_state.agri_val_slider = float(st.session_state.get('agri_val', 0.0))
if "agri_val_exact" not in st.session_state:
    st.session_state.agri_val_exact = float(st.session_state.get('agri_val', 0.0))
if "petro_val_slider" not in st.session_state:
    st.session_state.petro_val_slider = float(st.session_state.get('petro_val', 0.0))
if "petro_val_exact" not in st.session_state:
    st.session_state.petro_val_exact = float(st.session_state.get('petro_val', 0.0))

# ==========================================
# 🗂️ [UI] 메인 화면 탭 구성
# ==========================================
tab1, tab2, tab3 = st.tabs(["과거 데이터 추이", "근월 CPI 추정", "향후 1개년 경로 시뮬레이션"])

# ------------------------------------------
# 📜 TAB 1: 과거 추이
# ------------------------------------------
with tab1:
    st.header("📋 과거 데이터 분석 (품목성질별)")
    
    # 데이터셋에 총지수 컬럼 매핑 방어선 구축
    df_pure_items = df_hist.copy()
    if '총지수' not in df_pure_items.columns and 'Total' in df_pure_items.columns:
        df_pure_items.rename(columns={'Total': '총지수'}, inplace=True)
        hist_mom_all.rename(columns={'Total': '총지수'}, inplace=True)
        
    df_mom_matrix = df_pure_items[all_display_items].pct_change() * 100
    df_yoy_matrix = df_pure_items[all_display_items].pct_change(12) * 100

    st.subheader("🔍 1. 조회 대상 설정")
    user_view_choices = st.multiselect(
        "조회 유형 선택 (다중 선택 가능)",
        options=["CPI 원지수", "MOM 상승률", "YOY 상승률"],
        default=["MOM 상승률", "YOY 상승률"]
    )
    user_select_items = st.multiselect("대상 품목 선택 (다중 선택 가능)", options=all_display_items, default=["총지수"])

    st.markdown("---")
    st.subheader("📈 2. CPI 그래프 조회")
    
    if len(user_view_choices) > 0 and len(user_select_items) > 0:
        fig_dynamic_history = go.Figure()
        for view_mode in user_view_choices:
            if "원지수" in view_mode:
                target_mat = df_pure_items[all_display_items]
                suffix = "(지수)"
            elif "MOM" in view_mode:
                target_mat = df_mom_matrix
                suffix = "(MoM %)"
            else:
                target_mat = df_yoy_matrix
                suffix = "(YoY %)"
                
            for c_name in user_select_items:
                fig_dynamic_history.add_trace(go.Scatter(
                    x=target_mat.index, y=target_mat[c_name],
                    name=f"{c_name} {suffix}", mode='lines', line=dict(width=2)
                ))
                
        fig_dynamic_history.update_layout(xaxis_title="Date", yaxis_title="지수/상승률", hovermode="x unified", height=450)
        st.plotly_chart(fig_dynamic_history, use_container_width=True)
    else:
        st.warning("⚠️ 상단의 '조회 유형'과 '대상 품목'을 각각 최소 1개 이상 선택해 주세요.")

    st.subheader("📄 3. 품목별 데이터 테이블")
    table_choice = st.radio("데이터 종류", ["원지수", "MoM", "YoY"], horizontal=True)
    
    if "원지수" in table_choice:
        df_table_source = df_pure_items[all_display_items].copy()
    elif "MoM" in table_choice:
        df_table_source = df_mom_matrix.copy()
    else:
        df_table_source = df_yoy_matrix.copy()
        
    df_table_source.index = df_table_source.index.strftime('%Y-%m')
    st.dataframe(df_table_source.sort_index(ascending=False), use_container_width=True)


# ------------------------------------------
# 🔮 TAB 2: 근월 CPI 추정 (변동성 / 기조적 재분류 적용)
# ------------------------------------------
with tab2:
    col_title, col_btn = st.columns([3, 1])
    with col_title:
        st.header(f" 기준월 : {target_date.strftime('%Y-%m')} ({target_month}월 물가)")
        st.markdown(f"**변동성 품목:** 농축수산물, 석유제품 / **추세적 품목:** 이외공업제품, 전기수도가스, 집세, 서비스")
    
    with col_btn:
        st.write("") 
        st.write("")
        if st.button("🔄 품목별 예상치 초기화 (추천값 복원)", use_container_width=True, type="secondary"):
            st.session_state.agri_val = 0.0
            st.session_state.agri_val_slider = 0.0
            st.session_state.agri_val_exact = 0.0
            st.session_state.petro_val = 0.0
            st.session_state.petro_val_slider = 0.0
            st.session_state.petro_val_exact = 0.0
            st.session_state.processed_val = float(rec_values.get('가공식품', 0.0))
            st.session_state.other_manuf_val = float(rec_values.get('이외공업제품', 0.0))
            st.session_state.utility_val = float(rec_values['전기수도가스'])
            st.session_state.housing_val = float(rec_values['집세'])
            st.session_state.public_val = float(rec_values['공공서비스'])
            st.session_state.personal_val = float(rec_values['개인서비스'])
            st.rerun()

    st.markdown("---")
    col_left, col_right = st.columns([1, 1])
    
    with col_left:
        st.subheader("⛽ 1. 변동성 품목 수기조정")
        
        last_agri_mom = float(hist_mom_all['농축수산물'].iloc[-1]) if '농축수산물' in hist_mom_all.columns else 0.0
        st.caption("🔗 [KAMIS 소매가격 확인](https://www.kamis.or.kr/customer/price/agricultureRetail/catalogue.do)")
        agri_col1, agri_col2 = st.columns([3, 1])
        with agri_col1:
            st.slider(
                "농축수산물 예상 등락률 (MoM %)",
                min_value=-3.0, max_value=3.0, step=0.001, format="%.3f",
                key="agri_val_slider",
                on_change=sync_agri_from_slider,
                help=f"직전월 농축수산물 상승률(MoM)은 {last_agri_mom:.2f}% 이었습니다."
            )
        with agri_col2:
            st.number_input(
                "직접 입력",
                min_value=-3.0, max_value=3.0, step=0.001, format="%.3f",
                key="agri_val_exact",
                on_change=sync_agri_from_exact
            )

        last_petro_mom = float(hist_mom_all['석유제품'].iloc[-1]) if '석유제품' in hist_mom_all.columns else 0.0
        st.caption("🔗 [오피넷 국내유가동향 확인](https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do)")
        petro_col1, petro_col2 = st.columns([3, 1])
        with petro_col1:
            st.slider(
                "석유제품 예상 등락률 (MoM %)",
                min_value=-3.0, max_value=3.0, step=0.001, format="%.3f",
                key="petro_val_slider",
                on_change=sync_petro_from_slider,
                help=f"직전월 석유제품 상승률(MoM)은 {last_petro_mom:.2f}% 이었습니다."
            )
        with petro_col2:
            st.number_input(
                "직접 입력",
                min_value=-3.0, max_value=3.0, step=0.001, format="%.3f",
                key="petro_val_exact",
                on_change=sync_petro_from_exact
            )

       
        fx_mom = 0.0
        

    with col_right:
        st.subheader("📈 2. 추세적 품목 예상치")
        st.markdown(f"*(초기값 세팅 기준: **{current_model}**)*")
        
        mom_processed = st.number_input(
            f"가공식품 예상 (MoM %, 추천: {rec_values.get('가공식품', 0.0):.3f}%)",
            step=0.001, format="%.3f", key="processed_val",
            help=format_sarima_order('가공식품')
        )
        mom_other = st.number_input(
            f"이외공업제품 예상 (MoM %, 추천: {rec_values.get('이외공업제품', 0.0):.3f}%)",
            step=0.001, format="%.3f", key="other_manuf_val",
            help=f"{format_sarima_order('이외공업제품')} | 외생변수: WTI diff(lag1-lag2), USDKRW diff(lag1)"
        )
        
        mom_utility = st.number_input(
            f"전기수도가스 예상 (MoM %, 추천: {rec_values['전기수도가스']:.3f}%)",
            step=0.001, format="%.3f", key="utility_val",
            help=f"{format_sarima_order('전기수도가스')} | 외생변수: 없음"
        )
        
        mom_housing = st.number_input(
            f"집세 예상 (MoM %, 추천: {rec_values['집세']:.3f}%)",
            step=0.001, format="%.3f", key="housing_val",
            help=format_sarima_order('집세')
        )
        mom_public = st.number_input(
            f"공공서비스 예상 (MoM %, 추천: {rec_values['공공서비스']:.3f}%)",
            step=0.001, format="%.3f", key="public_val",
            help=format_sarima_order('공공서비스')
        )
        mom_personal = st.number_input(
            f"개인서비스 예상 (MoM %, 추천: {rec_values['개인서비스']:.3f}%)",
            step=0.001, format="%.3f", key="personal_val",
            help=format_sarima_order('개인서비스')
        )

    # ------------------------------------------
    # 🧮 환율 효과 및 인덱스 예측 연산 시동
    # ------------------------------------------
    pred_indices = compute_tab2_pred_indices(last_row, rec_values, st.session_state, base_fx)
    st.caption(f"📌 현재 기준: {current_model}")
        

    # 가중치 합산 기반 총지수 도출
    # 가중치 정보가 cpi_weights 에 존재하므로 호출하여 계산
    w_sum = 0.0
    w_index_product = 0.0
    
    component_weights = normalize_component_weights(cpi_weights, total_index_items)
    for item in total_index_items:
        w = component_weights.get(item, 0.0)
        w_index_product += pred_indices[item] * w
        w_sum += w

    # 정규화된 총지수(Total) 예측치 복원
    total_pred_index = w_index_product / w_sum if w_sum > 0 else last_row['총지수']
    hist_total_last = last_row['총지수']
    total_mom_rate = ((total_pred_index - hist_total_last) / hist_total_last) * 100 if hist_total_last != 0 else 0.0
    
    

    core_component_items_resolved = resolve_core_component_items(
        core_component_items,
        set(df_hist.columns),
        ['이외공업제품', '전기수도가스', '집세', '공공서비스', '개인서비스']
    )
    core_component_weights = {item: float(cpi_weights.get(item, 0.0)) for item in core_component_items_resolved}
    core_weight_total = sum(core_component_weights.values())
    core_pred_index = 0.0
    if core_weight_total > 0:
        for item in core_component_items_resolved:
            core_pred_index += pred_indices.get(item, last_row[item]) * core_component_weights[item]
        core_pred_index = core_pred_index / core_weight_total
    else:
        core_pred_index = last_row['총지수']

    core_hist_series = get_core_history_series(df_hist)
    core_hist_last = core_hist_series.dropna().iloc[-1] if core_hist_series.dropna().size > 0 else last_row['총지수']
    core_mom_rate = ((core_pred_index - core_hist_last) / core_hist_last) * 100 if core_hist_last != 0 else 0.0

    core_hist_mom = core_hist_series.pct_change() * 100
    core_hist_yoy = core_hist_series.pct_change(12) * 100

    ly_date = pd.to_datetime(target_date) - pd.DateOffset(years=1)
    try:
        closest_ly_idx = df_hist.index[df_hist.index.get_indexer([ly_date], method='nearest')[0]]
        ly_total = df_hist.loc[closest_ly_idx, '총지수'] if '총지수' in df_hist.columns else df_hist.loc[closest_ly_idx, 'Total']
        total_yoy_rate = ((total_pred_index - ly_total) / ly_total) * 100 if ly_total != 0 else 0.0
    except Exception:
        total_yoy_rate = total_mom_rate * 12

    try:
        closest_core_ly_idx = core_hist_series.index[core_hist_series.index.get_indexer([ly_date], method='nearest')[0]]
        ly_core_val = core_hist_series.loc[closest_core_ly_idx]
        core_yoy_rate = ((core_pred_index - ly_core_val) / ly_core_val) * 100 if ly_core_val != 0 else 0.0
    except Exception:
        core_yoy_rate = core_mom_rate * 12

    st.markdown("---")
    st.subheader(f"📊 {target_date.strftime('%Y-%m')} 시뮬레이션 결과")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric(label="🎯 예상 소비자물가 상승률 (MoM)", value=f"{total_mom_rate:.3f} %")
    k2.metric(label="📈 예상 소비자물가 상승률 (YoY)", value=f"{total_yoy_rate:.3f} %")
    k3.metric(label="🎯 예상 근원소비자물가 상승률 (MoM)", value=f"{core_mom_rate:.3f} %")
    k4.metric(label="📈 예상 근원소비자물가 상승률 (YoY)", value=f"{core_yoy_rate:.3f} %")




    # ------------------------------------------
    # 📉 예측 스냅샷 시각화 시각화 레이어
    # ------------------------------------------
    series_mode = st.radio("데이터 종류 선택", ["총지수", "근원"], horizontal=True, key="snapshot_series_mode")
    chart_view_mode = st.radio("상승률 기준 선택", ["원지수", "MoM (%)", "YoY (%)"], horizontal=True, key="snapshot_view_mode")
    hist_subset = df_hist[-24:].copy()
    
    if series_mode == "근원":
        hist_short_series = core_hist_series.loc[hist_subset.index]
        df_hist_mom_series = core_hist_mom.loc[hist_subset.index]
        df_hist_yoy_series = core_hist_yoy.loc[hist_subset.index]
        pred_index_value = core_pred_index
        pred_mom_value = core_mom_rate
        pred_yoy_value = core_yoy_rate
    else:
        hist_short_series = hist_subset['총지수']
        df_hist_mom_series = df_hist['총지수'].pct_change() * 100
        df_hist_yoy_series = df_hist['총지수'].pct_change(12) * 100
        pred_index_value = total_pred_index
        pred_mom_value = total_mom_rate
        pred_yoy_value = total_yoy_rate

    fig_dashboard = go.Figure()
    if "원지수" in chart_view_mode:
        y_hist = hist_short_series.values
        y_pred_line = [hist_short_series.iloc[-1], pred_index_value]
        y_title = f"{series_mode} 원지수"
    elif "MoM" in chart_view_mode:
        y_hist = df_hist_mom_series.loc[hist_subset.index].values
        y_pred_line = [df_hist_mom_series.iloc[-1], pred_mom_value]
        y_title = f"{series_mode} 전월비 상승률 (MoM %)"
    else:
        y_hist = df_hist_yoy_series.loc[hist_subset.index].values
        y_pred_line = [df_hist_yoy_series.iloc[-1], pred_yoy_value]
        y_title = f"{series_mode} 전년동월비 상승률 (YoY %)"

    fig_dashboard.add_trace(go.Scatter(x=hist_subset.index, y=y_hist, name='과거 확정치', mode='lines+markers', line=dict(color='#1f77b4', width=3)))
    fig_dashboard.add_trace(go.Scatter(x=[hist_subset.index[-1], pd.to_datetime(target_date)], y=y_pred_line, name='근월 추정치', mode='lines+markers', line=dict(color='#e377c2', width=3, dash='dash')))
    fig_dashboard.update_layout(xaxis_title="연월", yaxis_title=y_title, hovermode="x unified", height=400, margin=dict(t=20, b=20), legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
    st.plotly_chart(fig_dashboard, use_container_width=True)
    

    #with st.expander("📌 SARIMA 계수 결과 확인", expanded=False):
    # if sarima_coeff_results:
    #    for item in sarima_target_items:
    #        if item not in sarima_coeff_results:
    #            continue
    #        st.markdown(f"### {item}")
    #        if item == '이외공업제품':
    #        display_coeff_df = sarima_coeff_results[item][['variable', 'coef', 'stderr', 't', 'pvalue', 'ci_lower', 'ci_upper', 'significant']].copy()
    #        st.dataframe(display_coeff_df, use_container_width=True)
    # else:
    #    st.info("계수 결과를 불러오지 못했습니다.")




    # Tab3에서 사용할 pred_indices를 session_state에 저장
    st.session_state['pred_indices'] = pred_indices

def build_future_macro_rows(latest_macro_row=None):
    latest_macro_row = latest_macro_row if latest_macro_row is not None else {}

    def _get_value(col, default=0.0):
        try:
            return float(latest_macro_row.get(col, default))
        except Exception:
            return float(default)

    default_wti = _get_value('WTI', 0.0)
    default_krwusd = _get_value('USDKRW', 0.0)

    horizon_values = {
        '3m': {
            'WTI': float(st.session_state.get('forecast_wti_3m', default_wti)),
            'USDKRW': float(st.session_state.get('forecast_krwusd_3m', default_krwusd))
        },
        '6m': {
            'WTI': float(st.session_state.get('forecast_wti_6m', default_wti)),
            'USDKRW': float(st.session_state.get('forecast_krwusd_6m', default_krwusd))
        },
        '9m': {
            'WTI': float(st.session_state.get('forecast_wti_9m', default_wti)),
            'USDKRW': float(st.session_state.get('forecast_krwusd_9m', default_krwusd))
        },
        '12m': {
            'WTI': float(st.session_state.get('forecast_wti_12m', default_wti)),
            'USDKRW': float(st.session_state.get('forecast_krwusd_12m', default_krwusd))
        }
    }

    rows = []
    for step in range(1, 13):
        if step <= 3:
            horizon_key = '3m'
        elif step <= 6:
            horizon_key = '6m'
        elif step <= 9:
            horizon_key = '9m'
        else:
            horizon_key = '12m'
        rows.append({
            'WTI': horizon_values[horizon_key]['WTI'],
            'USDKRW': horizon_values[horizon_key]['USDKRW']
        })
    return rows


def predict_all_items_sarima_mom_track(df, w_dict, forecast_macro_row=None):
    sarima_mom_results = {}
    for item in w_dict.keys():
        if item not in df.columns:
            continue
        series = df[[item]].copy()
        exog = prepare_sarima_exog(df, item)
        if exog is not None:
            combined = series.join(exog, how='inner').dropna()
            endog = combined[item]
            exog_train = combined.drop(columns=[item])
        else:
            endog = series[item].dropna()
            exog_train = None
        
        if item in BEST_ORDERS:
            p_order = BEST_ORDERS[item]['order']
            s_order = BEST_ORDERS[item]['seasonal_order']
        else:
            p_order = (1, 1, 1)
            s_order = (1, 1, 0, 12)
            
        try:
            model = SARIMAX(endog, order=p_order, seasonal_order=s_order,
                            exog=exog_train, enforce_stationarity=False, enforce_invertibility=False)
            model_fit = model.fit(disp=False)
            if exog_train is not None:
                exog_forecast = prepare_sarima_forecast_exog(df, item, steps=12, forecast_macro_row=forecast_macro_row)
                raw_pred = model_fit.forecast(steps=12, exog=exog_forecast)
            else:
                raw_pred = model_fit.forecast(steps=12)
            raw_pred_series = [df[item].iloc[-1]] + list(raw_pred.values)
            pct_chg = np.diff(raw_pred_series) / raw_pred_series[:-1] * 100
            sarima_mom_results[item] = list(pct_chg)
        except Exception:
            sarima_mom_results[item] = [0.0] * 12
    return sarima_mom_results

with tab3:
    st.header(f" 향후 1개년 경로 시뮬레이션 ")
    st.markdown(f"본 시나리오는 상단에서 선택하신 **[{current_model}]**의 추정치를 바탕으로 향후 경로를 예측합니다.")
    st.markdown("---")

    total_col = '총지수' if '총지수' in df_hist.columns else 'Total'
    
    future_dates = [target_date + pd.DateOffset(months=i) for i in range(12)]
    extended_dates = [df_hist.index[-1]] + future_dates
    
    # 탭2의 현재 사용자 조정값을 기준으로 최신 예측값 재계산
    pred_indices = compute_tab2_pred_indices(last_row, rec_values, st.session_state, base_fx)

    col_exog_title, col_exog_reset = st.columns([5, 1])
    with col_exog_title:
        st.subheader("📉 외생변수 가정(시계열 모델만 해당)")
        st.caption("시계열 모형을 선택한 경우, 근월 이후 경로는 아래 입력값을 반영한 외생변수로 계산됩니다(초기값 : 근월 평균치)." \
        " 근월 수치는 Tab2에서 예측한 결과에 연동됩니다.")
    # latest_macro_row 가 없을 수 있으므로 안전하게 추출
    latest_macro_row = df_macro_raw.iloc[-1] if df_macro_raw is not None and not df_macro_raw.empty else None
    latest_wti = float(latest_macro_row.get('WTI', 0.0)) if latest_macro_row is not None and hasattr(latest_macro_row, 'get') else 0.0
    latest_krwusd = float(latest_macro_row.get('USDKRW', 0.0)) if latest_macro_row is not None and hasattr(latest_macro_row, 'get') else 0.0

    # 오른쪽 칼럼에 초기화 버튼 배치 — 누르면 8개 예측 입력을 최신값(초기값)으로 되돌립니다.
    with col_exog_reset:
        if st.button("변수 초기화", use_container_width=True):
            st.session_state['forecast_wti_3m'] = float(latest_wti)
            st.session_state['forecast_krwusd_3m'] = float(latest_krwusd)
            st.session_state['forecast_wti_6m'] = float(latest_wti)
            st.session_state['forecast_krwusd_6m'] = float(latest_krwusd)
            st.session_state['forecast_wti_9m'] = float(latest_wti)
            st.session_state['forecast_krwusd_9m'] = float(latest_krwusd)
            st.session_state['forecast_wti_12m'] = float(latest_wti)
            st.session_state['forecast_krwusd_12m'] = float(latest_krwusd)
            st.rerun()

    col3m, col6m, col9m, col12m = st.columns(4)
    with col3m:
        st.markdown("**~3개월**")
        st.number_input("WTI", key="forecast_wti_3m", value=latest_wti, step=0.01, format="%.2f")
        st.number_input("KRW/USD", key="forecast_krwusd_3m", value=latest_krwusd, step=0.01, format="%.2f")
    with col6m:
        st.markdown("**~6개월**")
        st.number_input("WTI", key="forecast_wti_6m", value=latest_wti, step=0.01, format="%.2f")
        st.number_input("KRW/USD", key="forecast_krwusd_6m", value=latest_krwusd, step=0.01, format="%.2f")
    with col9m:
        st.markdown("**~9개월**")
        st.number_input("WTI", key="forecast_wti_9m", value=latest_wti, step=0.01, format="%.2f")
        st.number_input("KRW/USD", key="forecast_krwusd_9m", value=latest_krwusd, step=0.01, format="%.2f")
    with col12m:
        st.markdown("**~12개월**")
        st.number_input("WTI", key="forecast_wti_12m", value=latest_wti, step=0.01, format="%.2f")
        st.number_input("KRW/USD", key="forecast_krwusd_12m", value=latest_krwusd, step=0.01, format="%.2f")

    future_macro_rows = build_future_macro_rows(latest_macro_row)

    # SARIMA 모델 실행
    sarima_trends_mom = predict_all_items_sarima_mom_track(df_hist, weights, forecast_macro_row=future_macro_rows)

    # 시뮬레이션 대상 항목 필터링 (weights의 키 중 실제 df_hist에 존재하는 항목만)
    target_items = [item for item in total_index_items if item in df_hist.columns]

    sim_indices_dict = {k: [last_row[k]] for k in target_items}
    component_weights = normalize_component_weights(cpi_weights, target_items)
    monthly_avg_growths = build_monthly_avg_growths(hist_mom_all, target_items)
    

    for i, f_date in enumerate(future_dates):
        f_month = f_date.month
        for item in target_items:
            if i == 0:
                next_val = pred_indices.get(item, sim_indices_dict[item][-1])
            else:
                if "SARIMA" in current_model:
                    series_list = sarima_trends_mom.get(item, [0.0] * 12)
                    try:
                        trend_mom = series_list[i]
                    except Exception:
                        trend_mom = series_list[-1] if series_list else 0.0
                else:
                    trend_mom = monthly_avg_growths[item].get(f_month, 0.0)
                next_val = sim_indices_dict[item][-1] * (1 + trend_mom / 100)
            sim_indices_dict[item].append(next_val)

    total_weight = sum(component_weights.get(item, 0.0) for item in target_items)
    hybrid_totals = []
    for step in range(1, 13):
        w_sum = sum(sim_indices_dict[item][step] * component_weights.get(item, 0.0) for item in target_items)
        hybrid_totals.append(w_sum / total_weight if total_weight > 0 else last_row[total_col])

    df_path_base = pd.DataFrame(index=future_dates, data={"Total_Index": hybrid_totals})
    base_indices_full = [last_row[total_col]] + list(df_path_base["Total_Index"])
    df_path_base["MoM"] = np.diff(base_indices_full) / base_indices_full[:-1] * 100
    
    base_yoy = []
    for f_date in future_dates:
        ly_date = f_date - pd.DateOffset(years=1)
        if ly_date in df_hist.index:
            ly_total_val = df_hist.loc[ly_date, total_col]
        else:
            closest_idx = df_hist.index[df_hist.index.get_indexer([ly_date], method='nearest')[0]]
            ly_total_val = df_hist.loc[closest_idx, total_col]
        base_yoy.append(((df_path_base.loc[f_date, "Total_Index"] - ly_total_val) / ly_total_val) * 100)
    df_path_base["YoY"] = base_yoy

    core_target_items = resolve_core_component_items(
        core_component_items,
        set(df_hist.columns),
        ['이외공업제품', '전기수도가스', '집세', '공공서비스', '개인서비스']
    )
    core_hist_series = get_core_history_series(df_hist)
    core_sim_indices_dict = {k: [last_row[k]] for k in core_target_items}
    core_component_weights = {item: float(cpi_weights.get(item, 0.0)) for item in core_target_items}
    core_monthly_avg_growths = build_monthly_avg_growths(hist_mom_all, core_target_items)

    for i, f_date in enumerate(future_dates):
        f_month = f_date.month
        for item in core_target_items:
            if i == 0:
                next_val = pred_indices.get(item, core_sim_indices_dict[item][-1])
            else:
                if "SARIMA" in current_model:
                    series_list = sarima_trends_mom.get(item, [0.0] * 12)
                    try:
                        trend_mom = series_list[i]
                    except Exception:
                        trend_mom = series_list[-1] if series_list else 0.0
                else:
                    trend_mom = core_monthly_avg_growths[item].get(f_month, 0.0)
                next_val = core_sim_indices_dict[item][-1] * (1 + trend_mom / 100)
            core_sim_indices_dict[item].append(next_val)

    core_total_weight = sum(core_component_weights.get(item, 0.0) for item in core_target_items)
    core_hybrid_totals = []
    for step in range(1, 13):
        core_w_sum = sum(core_sim_indices_dict[item][step] * core_component_weights.get(item, 0.0) for item in core_target_items)
        core_hybrid_totals.append(core_w_sum / core_total_weight if core_total_weight > 0 else last_row[total_col])

    df_path_core_base = pd.DataFrame(index=future_dates, data={"Core_Total_Index": core_hybrid_totals})
    core_hist_last_for_path = core_hist_series.dropna().iloc[-1] if core_hist_series.dropna().size > 0 else last_row[total_col]
    core_base_indices_full = [core_hist_last_for_path] + list(df_path_core_base["Core_Total_Index"])
    df_path_core_base["Core_MoM"] = np.diff(core_base_indices_full) / core_base_indices_full[:-1] * 100

    core_base_yoy = []
    for f_date in future_dates:
        ly_date = f_date - pd.DateOffset(years=1)
        if ly_date in core_hist_series.index:
            ly_core_val = core_hist_series.loc[ly_date]
        else:
            closest_idx = core_hist_series.index[core_hist_series.index.get_indexer([ly_date], method='nearest')[0]]
            ly_core_val = core_hist_series.loc[closest_idx]
        core_base_yoy.append(((df_path_core_base.loc[f_date, "Core_Total_Index"] - ly_core_val) / ly_core_val) * 100 if ly_core_val != 0 else 0.0)
    df_path_core_base["Core_YoY"] = core_base_yoy

    if "table_reset_counter" not in st.session_state:
        st.session_state["table_reset_counter"] = 0
        
    model_hint = "sarima" if "SARIMA" in current_model else "avg3y"
    session_df_key = f"df_sim_report_{model_hint}_v_{st.session_state['table_reset_counter']}"

    current_tab2_signature = (
        current_model,
        float(st.session_state.get("agri_val", 0.0)),
        float(st.session_state.get("petro_val", 0.0)),
        # fx_val removed; USDKRW used only as SARIMAX exog
        float(st.session_state.get("processed_val", rec_values.get('가공식품', 0.0))),
        float(st.session_state.get("other_manuf_val", rec_values.get('이외공업제품', 0.0))),
        float(st.session_state.get("utility_val", rec_values['전기수도가스'])),
        float(st.session_state.get("housing_val", rec_values['집세'])),
        float(st.session_state.get("public_val", rec_values['공공서비스'])),
        float(st.session_state.get("personal_val", rec_values['개인서비스']))
    )
    prev_tab2_signature = st.session_state.get("tab2_signature")
    if prev_tab2_signature != current_tab2_signature and session_df_key in st.session_state:
        del st.session_state[session_df_key]

    # Tab3 외생변수 입력 변경 감지: exog signature를 만들어 이전값과 비교하여 테이블 캐시를 무효화
    current_exog_signature = (
        float(st.session_state.get('forecast_wti_3m', 0.0)), float(st.session_state.get('forecast_krwusd_3m', 0.0)),
        float(st.session_state.get('forecast_wti_6m', 0.0)), float(st.session_state.get('forecast_krwusd_6m', 0.0)),
        float(st.session_state.get('forecast_wti_9m', 0.0)), float(st.session_state.get('forecast_krwusd_9m', 0.0)),
        float(st.session_state.get('forecast_wti_12m', 0.0)), float(st.session_state.get('forecast_krwusd_12m', 0.0))
    )
    prev_exog_signature = st.session_state.get('tab3_exog_signature')
    if prev_exog_signature != current_exog_signature and session_df_key in st.session_state:
        del st.session_state[session_df_key]
    st.session_state['tab3_exog_signature'] = current_exog_signature

    col_sub_title, col_reset_btn = st.columns([4, 1])
    with col_sub_title:
        st.subheader("📋 향후 1년 예측 경로 (수기 조정 가능)")
        st.caption("💡 **'예상 MoM (%)'** 컬럼의 수치를 수정할 수 있으며, 연동 차트가 실시간 재계산됩니다.")
    
    with col_reset_btn:
        if st.button("🔄 테이블 초기화", use_container_width=True, key="reset_table_btn"):
            st.session_state["table_reset_counter"] += 1
            if session_df_key in st.session_state: del st.session_state[session_df_key]
            st.rerun()

    if session_df_key not in st.session_state:
        df_base_report = pd.DataFrame(index=future_dates)
        df_base_report.index = df_base_report.index.strftime('%Y-%m')
        df_base_report["예상 MoM (%)"] = df_path_base["MoM"].values
        df_base_report["예상 원지수"] = df_path_base["Total_Index"].values
        df_base_report["예상 YoY (%)"] = df_path_base["YoY"].values
        df_base_report["예상 core MoM (%)"] = df_path_core_base["Core_MoM"].values
        df_base_report["예상 core 원지수"] = df_path_core_base["Core_Total_Index"].values
        df_base_report["예상 core YoY (%)"] = df_path_core_base["Core_YoY"].values
        st.session_state[session_df_key] = df_base_report

    st.session_state["tab2_signature"] = current_tab2_signature

    def _get_base_component_mom(item, step_idx, f_month):
        if step_idx == 0:
            prev_val = float(last_row[item])
            next_val = float(pred_indices.get(item, prev_val))
            return ((next_val / prev_val) - 1) * 100 if prev_val != 0 else 0.0
        if "SARIMA" in current_model:
            series_list = sarima_trends_mom.get(item, [0.0] * 12)
            try:
                return float(series_list[step_idx])
            except Exception:
                return float(series_list[-1]) if series_list else 0.0
        return float(monthly_avg_growths[item].get(f_month, 0.0))


    def _lookup_total_yoy(f_date, index_value):
        ly_date = f_date - pd.DateOffset(years=1)
        if ly_date in df_hist.index:
            ly_total_val = df_hist.loc[ly_date, total_col]
        else:
            closest_idx = df_hist.index[df_hist.index.get_indexer([ly_date], method='nearest')[0]]
            ly_total_val = df_hist.loc[closest_idx, total_col]
        return ((index_value - ly_total_val) / ly_total_val) * 100 if ly_total_val != 0 else 0.0


    def _lookup_core_yoy(f_date, index_value):
        ly_date = f_date - pd.DateOffset(years=1)
        if ly_date in core_hist_series.index:
            ly_core_val = core_hist_series.loc[ly_date]
        else:
            closest_idx = core_hist_series.index[core_hist_series.index.get_indexer([ly_date], method='nearest')[0]]
            ly_core_val = core_hist_series.loc[closest_idx]
        return ((index_value - ly_core_val) / ly_core_val) * 100 if ly_core_val != 0 else 0.0


    def _weighted_index(indices_by_item, items, weights_by_item, fallback):
        valid_items = [item for item in items if item in indices_by_item]
        total_w = sum(weights_by_item.get(item, 0.0) for item in valid_items)
        if total_w <= 0:
            return fallback
        return sum(indices_by_item[item] * weights_by_item.get(item, 0.0) for item in valid_items) / total_w


    def _build_adjusted_path_report(template_df, manual_moms):
        cols = list(template_df.columns)
        adjusted_indices = {item: float(last_row[item]) for item in target_items}
        prev_total_index = float(last_row[total_col])
        prev_core_index = float(core_hist_last_for_path)
        rows = []

        for step_idx, f_date in enumerate(future_dates):
            f_month = f_date.month
            base_moms = {
                item: _get_base_component_mom(item, step_idx, f_month)
                for item in target_items
            }

            shock = 0.0
            if step_idx in manual_moms:
                target_mom = float(manual_moms[step_idx])
                target_total_index = prev_total_index * (1 + target_mom / 100)
                base_weighted_sum = sum(
                    adjusted_indices[item] * (1 + base_moms[item] / 100) * component_weights.get(item, 0.0)
                    for item in target_items
                )
                shock_denominator = sum(
                    adjusted_indices[item] * component_weights.get(item, 0.0)
                    for item in target_items
                ) / 100
                if shock_denominator != 0:
                    shock = ((target_total_index * total_weight) - base_weighted_sum) / shock_denominator

            for item in target_items:
                adjusted_indices[item] = adjusted_indices[item] * (1 + (base_moms[item] + shock) / 100)

            total_index = _weighted_index(adjusted_indices, target_items, component_weights, prev_total_index)
            total_mom = ((total_index / prev_total_index) - 1) * 100 if prev_total_index != 0 else 0.0
            total_yoy = _lookup_total_yoy(f_date, total_index)

            core_index = _weighted_index(adjusted_indices, core_target_items, core_component_weights, prev_core_index)
            core_mom = ((core_index / prev_core_index) - 1) * 100 if prev_core_index != 0 else 0.0
            core_yoy = _lookup_core_yoy(f_date, core_index)

            rows.append([total_mom, total_index, total_yoy, core_mom, core_index, core_yoy])
            prev_total_index = total_index
            prev_core_index = core_index

        adjusted_df = pd.DataFrame(rows, index=template_df.index, columns=cols)
        return adjusted_df


    editor_key = f"editor_{model_hint}_v_{st.session_state['table_reset_counter']}"
    if editor_key in st.session_state and st.session_state[editor_key].get("edited_rows"):
        changes = st.session_state[editor_key]["edited_rows"]
        current_df = st.session_state[session_df_key].copy()
        mom_col = current_df.columns[0]
        manual_moms = {}
        for row_idx, updated_cols in changes.items():
            if mom_col in updated_cols:
                manual_moms[int(row_idx)] = float(updated_cols[mom_col])

        manual_signature = tuple(sorted(manual_moms.items()))
        applied_signature_key = f"{editor_key}_manual_signature"
        if manual_moms and st.session_state.get(applied_signature_key) != manual_signature:
            st.session_state[session_df_key] = _build_adjusted_path_report(current_df, manual_moms)
            st.session_state[applied_signature_key] = manual_signature
            st.rerun()

    def highlight_modifiable_col(s):
        style_list = [''] * len(s)
        style_list[s.index.get_loc("예상 MoM (%)")] = 'background-color: #EBF3F9; font-weight: bold;'
        return style_list

    styled_df = st.session_state[session_df_key].style.apply(highlight_modifiable_col, axis=1)
    edited_df = st.data_editor(
        styled_df, key=editor_key, disabled=["예상 원지수", "예상 YoY (%)", "예상 core MoM (%)", "예상 core 원지수", "예상 core YoY (%)"],
        column_config={
            "예상 MoM (%)": st.column_config.NumberColumn("🎨 예상 MoM (%)", format="%.2f%%"),
            "예상 원지수": st.column_config.NumberColumn("예상 원지수", format="%.2f"),
            "예상 YoY (%)": st.column_config.NumberColumn("예상 YoY (%)", format="%.2f%%"),
            "예상 core MoM (%)": st.column_config.NumberColumn("예상 core MoM (%)", format="%.2f%%"),
            "예상 core 원지수": st.column_config.NumberColumn("예상 core 원지수", format="%.2f"),
            "예상 core YoY (%)": st.column_config.NumberColumn("예상 core YoY (%)", format="%.2f%%"),
        }, use_container_width=True
    )

    st.markdown("---")
    st.subheader(f"📉 {current_model} 기반 물가 경로")
    path_series_mode = st.radio("데이터 종류 선택", ["총지수", "근원"], horizontal=True, key="path_series_mode")
    path_view_mode = st.radio("상승률 기준 선택", ["CPI 원지수", "MoM (%)", "YoY (%)"], horizontal=True, key="path_view_mode")

    current_sim_data = st.session_state[session_df_key]
    hist_short = df_hist[-24:]
    core_hist_series = get_core_history_series(df_hist)
    core_hist_short = core_hist_series.loc[hist_short.index]
    hist_mom_short = hist_mom_all[total_col].loc[hist_short.index]
    hist_yoy_short = (df_hist[total_col].pct_change(12) * 100).loc[hist_short.index]
    core_mom_short = core_hist_series.pct_change() * 100
    core_yoy_short = core_hist_series.pct_change(12) * 100

    fig_path = go.Figure()
    if path_series_mode == "근원":
        if "원지수" in path_view_mode:
            fig_path.add_trace(go.Scatter(x=hist_short.index, y=core_hist_short, name='과거 확정치', mode='lines+markers', line=dict(color='#7f7f7f', width=2.5)))
            pred_y_arr = [core_hist_short.iloc[-1]] + list(current_sim_data["예상 core 원지수"])
            y_lbl = "근원 원지수"
        elif "MoM" in path_view_mode:
            fig_path.add_trace(go.Scatter(x=hist_short.index, y=core_mom_short.loc[hist_short.index], name='과거 확정치', mode='lines+markers', line=dict(color='#7f7f7f', width=2.5)))
            pred_y_arr = [core_mom_short.loc[hist_short.index].iloc[-1]] + list(current_sim_data["예상 core MoM (%)"])
            y_lbl = "근원 MoM 상승률 (%)"
        else:
            fig_path.add_trace(go.Scatter(x=hist_short.index, y=core_yoy_short.loc[hist_short.index], name='과거 확정치', mode='lines+markers', line=dict(color='#7f7f7f', width=2.5)))
            pred_y_arr = [core_yoy_short.loc[hist_short.index].iloc[-1]] + list(current_sim_data["예상 core YoY (%)"])
            y_lbl = "근원 YoY 상승률 (%)"
    else:
        if "원지수" in path_view_mode:
            fig_path.add_trace(go.Scatter(x=hist_short.index, y=hist_short[total_col], name='과거 확정치', mode='lines+markers', line=dict(color='#7f7f7f', width=2.5)))
            pred_y_arr = [hist_short[total_col].iloc[-1]] + list(current_sim_data["예상 원지수"])
            y_lbl = "CPI 원지수"
        elif "MoM" in path_view_mode:
            fig_path.add_trace(go.Scatter(x=hist_short.index, y=hist_mom_short, name='과거 확정치', mode='lines+markers', line=dict(color='#7f7f7f', width=2.5)))
            pred_y_arr = [hist_mom_short.iloc[-1]] + list(current_sim_data["예상 MoM (%)"])
            y_lbl = "MoM 상승률 (%)"
        else:
            fig_path.add_trace(go.Scatter(x=hist_short.index, y=hist_yoy_short, name='과거 확정치', mode='lines+markers', line=dict(color='#7f7f7f', width=2.5)))
            pred_y_arr = [hist_yoy_short.iloc[-1]] + list(current_sim_data["예상 YoY (%)"])
            y_lbl = "YoY 상승률 (%)"

    fig_path.add_trace(go.Scatter(
        x=extended_dates, y=pred_y_arr, name=f'추정 경로 ({current_model})', mode='lines+markers',
        line=dict(color='#d62728' if "평균" in current_model else '#1f77b4', width=3, dash='dash')
    ))
    fig_path.update_layout(xaxis_title="Date", yaxis_title=y_lbl, hovermode="x unified", height=520, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0.01))
    st.plotly_chart(fig_path, use_container_width=True)
