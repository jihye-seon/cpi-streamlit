import streamlit as st
import pandas as pd
import numpy as np
import datetime
import plotly.graph_objects as go
import warnings

# SARIMA 모델용 라이브러리
from statsmodels.tsa.statespace.sarimax import SARIMAX

warnings.filterwarnings("ignore")

# --- [1] 페이지 레이아웃 설정 ---
st.set_page_config(page_title="CPI 대시보드", layout="wide")

st.title("📊 한국 CPI 시뮬레이터 (재분류 반영)")
st.markdown("변동성 품목과 기조적 품목을 새로 정의된 기준에 따라 분류하고 시뮬레이션을 진행합니다.")
st.markdown("---")

# ==========================================
# 📂 [DATA LOADING] 엑셀 파일 업로드 인터페이스
# ==========================================
st.sidebar.header("📁 데이터 소스 로드")
uploaded_file = st.sidebar.file_uploader("CPI 마스터 엑셀 파일을 업로드하세요", type=["xlsx"])

@st.cache_data
def process_uploaded_excel(file):
    try:
        # cpi_data 로드 (날짜 가공 및 인덱스 설정)
        df_cpi = pd.read_excel(file, sheet_name="CPI_Data", header=0)
        df_cpi['Date'] = pd.to_datetime(df_cpi['Date'], errors='coerce')
        df_cpi = df_cpi.dropna(subset=['Date']).sort_values('Date')
        df_cpi.set_index('Date', inplace=True)
        
        # cpi_weights 로드 (딕셔너리 구조로 변환)
        df_weights = pd.read_excel(file, sheet_name="CPI_Weights", header=0)
        # 엑셀 양식에 맞게 컬럼명 매핑 (Column_Name / Weight 기준)
        weights = df_weights.set_index(df_weights.columns[0])[df_weights.columns[1]].to_dict()
        

        
        # -------------------------------------------------------------------------
        # [핵심] 기조적 품목 내 '석유 외 공업제품' 계산 (가공식품 + 이외공업제품 가중합)
        # -------------------------------------------------------------------------
        w_processed = weights.get('가공식품', 0)
        w_other_manufactured = weights.get('이외공업제품', 0)
        w_total_ex_oil = w_processed + w_other_manufactured
        
        if w_total_ex_oil > 0:
            df_cpi['석유 외 공업제품'] = (
                (df_cpi['가공식품'] * w_processed) + (df_cpi['이외공업제품'] * w_other_manufactured)
            ) / w_total_ex_oil
        else:
            df_cpi['석유 외 공업제품'] = df_cpi['이외공업제품'] # 방어 코드
            
        weights['석유 외 공업제품'] = w_total_ex_oil
        
        # Oil_krw_Data 시트 로드
        df_macro = pd.read_excel(file, sheet_name="Oil_krw_Data", header=0)
        df_macro['Date'] = pd.to_datetime(df_macro['Date'], errors='coerce')
        df_macro = df_macro.dropna(subset=['Date'])
        df_macro.set_index('Date', inplace=True)
        
        macro_cols = ['WTI', 'Brent', 'USDKRW']
        df_macro = df_macro[macro_cols]
        
        df_merged = df_cpi.join(df_macro, how='inner')
        return df_merged.sort_index(), weights
    except Exception as e:
        st.sidebar.error(f"엑셀 구조 파싱 실패: {e}")
        return None, None

if uploaded_file is not None:
    df_hist, cpi_weights = process_uploaded_excel(uploaded_file)
else:
    df_hist, cpi_weights = None, None

if df_hist is None:
    st.info("💡 대시보드를 시작하려면 왼쪽 사이드바에서 엑셀 파일을 업로드해 주세요.")
    st.stop()

# 주요 변수 선언
last_date = df_hist.index[-1]
last_row = df_hist.iloc[-1]
target_date = last_date + pd.DateOffset(months=1)
target_month = target_date.month 

if "weights" not in st.session_state:
    # 예시: 엑셀에서 가중치 시트를 읽어와 딕셔너리로 만드는 기존 코드가 있다면 다음과 같이 session_state에 주입
    try:
        # 가중치 파일이나 데이터프레임(df_weights)이 있다면 변환
        # 주석을 풀고 기존 가중치 딕셔너리 변환 코드를 여기에 넣으세요.
        st.session_state["weights"] = df_weights.set_index(df_weights.columns[0])[df_weights.columns[1]].to_dict()
        
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
# 새롭게 정의된 Tab2 분류 구성 품목 풀(Pool)
# ------------------------------------------
volatile_items = ['농축수산물', '석유제품']
core_items = ['석유 외 공업제품', '전기수도가스', '집세', '공공서비스', '개인서비스']
all_display_items = ['총지수'] + volatile_items + ['가공식품', '이외공업제품'] + core_items

# ==========================================
# 🔮 [💡 백엔드 1] SARIMA 모델 예측 함수 (기조적 품목 대상)
# ==========================================
@st.cache_data
def predict_items_with_sarima(df, target_items):
    sarima_results = {}
    for item in target_items:
        series = df[item].dropna()
        model = SARIMAX(series, 
                        order=(1, 1, 1), 
                        seasonal_order=(1, 1, 0, 12),
                        enforce_stationarity=False,
                        enforce_invertibility=False)
        model_fit = model.fit(disp=False)
        
        pred_index = model_fit.forecast(steps=1).iloc[0]
        last_val = series.iloc[-1]
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
        sarima_preds = predict_items_with_sarima(df_hist, core_items)
        seasonal_preds = calculate_3yr_seasonal_avg(hist_mom_all, target_month, core_items)
    st.success("✅ 연산 완료")

# ==========================================
# 🔄 세션 상태(Session State) 초기화 및 매핑
# ==========================================
base_fx = float(round(last_row['USDKRW'], 1))

if "model_choice" not in st.session_state:
    st.session_state.model_choice = "🤖 SARIMA 모델"

st.subheader("⚙️ 추세 추정 방법론 선택")
current_model = st.radio(
    "어떤 방법론을 베이스(기본값)로 기조적 품목 시뮬레이션을 진행하시겠습니까?",
    ["🤖 SARIMA 모델", "🗓️ 과거 3개년 동월 평균 MoM"],
    horizontal=True,
    key="model_choice"
)

if current_model == "🤖 SARIMA 모델":
    rec_values = sarima_preds
else:
    rec_values = seasonal_preds

# 최초 세션 세팅 (새로운 한글 대분류 반영)
if "agri_val" not in st.session_state: st.session_state.agri_val = 0.0
if "petro_val" not in st.session_state: st.session_state.petro_val = 0.0
if "fx_val" not in st.session_state: st.session_state.fx_val = base_fx

if "core_oil_ex_val" not in st.session_state: st.session_state.core_oil_ex_val = float(rec_values['석유 외 공업제품'])
if "utility_val" not in st.session_state: st.session_state.utility_val = float(rec_values['전기수도가스'])
if "housing_val" not in st.session_state: st.session_state.housing_val = float(rec_values['집세'])
if "public_val" not in st.session_state: st.session_state.public_val = float(rec_values['공공서비스'])
if "personal_val" not in st.session_state: st.session_state.personal_val = float(rec_values['개인서비스'])

# 모델 변경감지 로직
if "prev_model" not in st.session_state:
    st.session_state.prev_model = current_model

if st.session_state.prev_model != current_model:
    st.session_state.core_oil_ex_val = float(rec_values['석유 외 공업제품'])
    st.session_state.utility_val = float(rec_values['전기수도가스'])
    st.session_state.housing_val = float(rec_values['집세'])
    st.session_state.public_val = float(rec_values['공공서비스'])
    st.session_state.personal_val = float(rec_values['개인서비스'])
    st.session_state.prev_model = current_model
    st.rerun()

# ==========================================
# 🗂️ [UI] 메인 화면 탭 구성
# ==========================================
tab1, tab2, tab3 = st.tabs(["📜 과거 데이터 추이", "🔮 익월 CPI 추정", "📈 향후 1개년 경로 시뮬레이터"])

# ------------------------------------------
# 📜 TAB 1: 과거 추이
# ------------------------------------------
with tab1:
    st.header("📋 품목성질별 과거 데이터 분석")
    
    # 데이터셋에 총지수 컬럼 매핑 방어선 구축
    df_pure_items = df_hist.copy()
    if '총지수' not in df_pure_items.columns and 'Total' in df_pure_items.columns:
        df_pure_items.rename(columns={'Total': '총지수'}, inplace=True)
        hist_mom_all.rename(columns={'Total': '총지수'}, inplace=True)
        
    df_mom_matrix = df_pure_items[all_display_items].pct_change() * 100
    df_yoy_matrix = df_pure_items[all_display_items].pct_change(12) * 100

    st.subheader("🔍 1. 분석 모드 및 품목 필터링")
    user_view_choices = st.multiselect(
        "조회 유형 선택 (다중 선택 가능)",
        options=["CPI 원지수", "MOM 상승률", "YOY 상승률"],
        default=["MOM 상승률", "YOY 상승률"]
    )
    user_select_items = st.multiselect("대상 품목 선택 (다중 선택 가능)", options=all_display_items, default=["총지수"])

    st.markdown("---")
    st.subheader("📈 2. 품목별 CPI 시계열 그래프")
    
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
                
        fig_dynamic_history.update_layout(xaxis_title="조사 연월", yaxis_title="지수/상승률", hovermode="x unified", height=450)
        st.plotly_chart(fig_dynamic_history, use_container_width=True)
    else:
        st.warning("⚠️ 상단의 '조회 유형'과 '대상 품목'을 각각 최소 1개 이상 선택해 주세요.")

    st.subheader("📄 3. 품목성질별 데이터 시트")
    table_choice = st.radio("데이터 종류", ["원지수 기준", "MoM 등락률 기준", "YoY 등락률 기준"], horizontal=True)
    
    if "원지수" in table_choice:
        df_table_source = df_pure_items[all_display_items].copy()
    elif "MoM" in table_choice:
        df_table_source = df_mom_matrix.copy()
    else:
        df_table_source = df_yoy_matrix.copy()
        
    df_table_source.index = df_table_source.index.strftime('%Y-%m')
    st.dataframe(df_table_source.sort_index(ascending=False), use_container_width=True)


# ------------------------------------------
# 🔮 TAB 2: 익월 CPI 추정 (변동성 / 기조적 재분류 적용)
# ------------------------------------------
with tab2:
    col_title, col_btn = st.columns([3, 1])
    with col_title:
        st.header(f"🔮 예측 대상: {target_date.strftime('%Y-%m')} ({target_month}월 물가)")
        st.markdown(f"**변동성 품목:** 농축수산물, 석유제품 / **기조적 품목:** 석유 외 공업제품 등 5개 항목")
    
    with col_btn:
        st.write("") 
        st.write("")
        if st.button("🔄 품목별 예상치 초기화 (추천값 복원)", use_container_width=True, type="secondary"):
            st.session_state.agri_val = 0.0
            st.session_state.petro_val = 0.0
            st.session_state.fx_val = base_fx
            st.session_state.core_oil_ex_val = float(rec_values['석유 외 공업제품'])
            st.session_state.utility_val = float(rec_values['전기수도가스'])
            st.session_state.housing_val = float(rec_values['집세'])
            st.session_state.public_val = float(rec_values['공공서비스'])
            st.session_state.personal_val = float(rec_values['개인서비스'])
            st.rerun()

    st.markdown("---")
    col_left, col_right = st.columns([1, 1])
    
    with col_left:
        st.subheader("⛽ 1. 변동성 품목 미세조정")
        
        last_agri_mom = float(hist_mom_all['농축수산물'].iloc[-1]) if '농축수산물' in hist_mom_all.columns else 0.0
        st.caption("🔗 [KAMIS 소매가격 확인](https://www.kamis.or.kr/customer/price/agricultureRetail/catalogue.do)")
        mom_agri = st.slider(
            "농축수산물 예상 등락률 (MoM %)", 
            min_value=-5.0, max_value=5.0, step=0.1, key="agri_val",
            help=f"직전월 농축수산물 상승률(MoM)은 {last_agri_mom:.2f}% 이었습니다."
        )
        
        last_petro_mom = float(hist_mom_all['석유제품'].iloc[-1]) if '석유제품' in hist_mom_all.columns else 0.0
        st.caption("🔗 [오피넷 국내유가동향 확인](https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do)")
        mom_petro = st.slider(
            "석유제품 예상 등락률 (MoM %)", 
            min_value=-5.0, max_value=5.0, step=0.1, key="petro_val",
            help=f"직전월 석유제품 상승률(MoM)은 {last_petro_mom:.2f}% 이었습니다."
        )
        
        exchange_rate = st.number_input("예상 원/달러 환율 (원)", step=5.0, key="fx_val")
        fx_mom = ((exchange_rate - base_fx) / base_fx) * 100
        st.caption(f"💡 환율 변동률: 전월비 **{fx_mom:+.2f}%**")

    with col_right:
        st.subheader("📈 2. 기조적 추세 품목 예상치")
        st.markdown(f"*(초기값 세팅 기준: **{current_model}**)*")
        
        mom_oil_ex = st.number_input(f"석유 외 공업제품 예상 (MoM %, 추천: {rec_values['석유 외 공업제품']:.2f}%)", step=0.05, key="core_oil_ex_val")
        mom_utility = st.number_input(f"전기수도가스 예상 (MoM %, 추천: {rec_values['전기수도가스']:.2f}%)", step=0.1, key="utility_val")
        mom_housing = st.number_input(f"집세 예상 (MoM %, 추천: {rec_values['집세']:.2f}%)", step=0.01, key="housing_val")
        mom_public = st.number_input(f"공공서비스 예상 (MoM %, 추천: {rec_values['공공서비스']:.2f}%)", step=0.1, key="public_val")
        mom_personal = st.number_input(f"개인서비스 예상 (MoM %, 추천: {rec_values['개인서비스']:.2f}%)", step=0.05, key="personal_val")

    # ------------------------------------------
    # 🧮 환율 효과 및 인덱스 예측 연산 시동
    # ------------------------------------------
    fx_to_core_manufactured_beta = 0.03
    adjusted_oil_ex_mom = mom_oil_ex + (fx_mom * fx_to_core_manufactured_beta)
    
    # 예측 반영용 MoM 딕셔너리 매핑
    pred_mom_dict = {
        '농축수산물': mom_agri,
        '석유제품': mom_petro,
        '석유 외 공업제품': adjusted_oil_ex_mom,
        '전기수도가스': mom_utility,
        '집세': mom_housing,
        '공공서비스': mom_public,
        '개인서비스': mom_personal
    }
    
    # 각 최종 항목의 예상 지수 산출
    pred_indices = {}
    for item in pred_mom_dict.keys():
        pred_indices[item] = last_row[item] * (1 + pred_mom_dict[item] / 100)
        
    # 가중치 합산 기반 총지수 도출
    # 가중치 정보가 cpi_weights 에 존재하므로 호출하여 계산
    w_sum = 0.0
    w_index_product = 0.0
    
    final_calc_items = ['농축수산물', '석유제품', '석유 외 공업제품', '전기수도가스', '집세', '공공서비스', '개인서비스']
    for item in final_calc_items:
        w = cpi_weights.get(item, 0)
        w_index_product += pred_indices[item] * w
        w_sum += w

    # 정규화된 총지수(Total) 예측치 복원
    pred_total = w_index_product / w_sum if w_sum > 0 else last_row['총지수']
    
    # 전월비(MoM) 및 전년동월비(YoY) 계산
    hist_total_last = last_row['총지수']
    total_mom = ((pred_total - hist_total_last) / hist_total_last) * 100
    
    ly_date = pd.to_datetime(target_date) - pd.DateOffset(years=1)
    try:
        closest_ly_idx = df_hist.index[df_hist.index.get_indexer([ly_date], method='nearest')[0]]
        ly_total = df_hist.loc[closest_ly_idx, '총지수'] if '총지수' in df_hist.columns else df_hist.loc[closest_ly_idx, 'Total']
        total_yoy = ((pred_total - ly_total) / ly_total) * 100
    except:
        total_yoy = total_mom * 12

    st.markdown("---")
    st.subheader(f"📊 {target_date.strftime('%Y-%m')} 총지수 시뮬레이션 결과")
    k1, k2 = st.columns(2)
    k1.metric(label="🎯 예상 소비자물가 상승률 (MoM)", value=f"{total_mom:.2f} %")
    k2.metric(label="📈 예상 소비자물가 상승률 (YoY)", value=f"{total_yoy:.2f} %")

    # ------------------------------------------
    # 📉 예측 스냅샷 시각화 시각화 레이어
    # ------------------------------------------
    chart_view_mode = st.radio("차트 표시 기준 선택", ["CPI 원지수", "MoM 상승률 (%)", "YoY 상승률 (%)"], horizontal=True, key="snapshot_view_mode")
    hist_subset = df_hist[-24:].copy()
    
    df_hist_mom = df_hist['총지수'].pct_change() * 100
    df_hist_yoy = df_hist['총지수'].pct_change(12) * 100

    fig_dashboard = go.Figure()
    if "원지수" in chart_view_mode:
        y_hist = hist_subset['총지수'].values
        y_pred_line = [hist_subset['총지수'].iloc[-1], pred_total]
        y_title = "CPI 총지수"
    elif "MoM" in chart_view_mode:
        y_hist = df_hist_mom.loc[hist_subset.index].values
        y_pred_line = [df_hist_mom.iloc[-1], total_mom]
        y_title = "전월비 상승률 (MoM %)"
    else:
        y_hist = df_hist_yoy.loc[hist_subset.index].values
        y_pred_line = [df_hist_yoy.iloc[-1], total_yoy]
        y_title = "전년동월비 상승률 (YoY %)"

    fig_dashboard.add_trace(go.Scatter(x=hist_subset.index, y=y_hist, name='과거 확정치', mode='lines+markers', line=dict(color='#1f77b4', width=3)))
    fig_dashboard.add_trace(go.Scatter(x=[hist_subset.index[-1], pd.to_datetime(target_date)], y=y_pred_line, name='익월 추정치', mode='lines+markers', line=dict(color='#e377c2', width=3, dash='dash')))
    fig_dashboard.update_layout(xaxis_title="연월", yaxis_title=y_title, hovermode="x unified", height=400, margin=dict(t=20, b=20), legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
    st.plotly_chart(fig_dashboard, use_container_width=True)


with tab3:
    st.header(f"🔮 향후 1개년 경로 시뮬레이터 ({current_model})")
    st.markdown(f"본 시나리오는 상단에서 선택하신 **[{current_model}]**의 추정치를 바탕으로 향후 경로를 예측합니다.")
    st.markdown("---")

    
    future_dates = [target_date + pd.DateOffset(months=i) for i in range(12)]
    extended_dates = [df_hist.index[-1]] + future_dates

    # 캐시 함수 내부에 전역 weights가 직접 들어가지 않도록 인자(w_dict)로 명시적 전달
    @st.cache_data
    def predict_all_items_sarima_mom_track(df, w_dict):
        sarima_mom_results = {}
        for item in w_dict.keys():
            if item not in df.columns:
                continue
            series = df[item].dropna()
            model = SARIMAX(series, order=(1, 1, 1), seasonal_order=(1, 1, 0, 12),
                            enforce_stationarity=False, enforce_invertibility=False)
            model_fit = model.fit(disp=False)
            raw_pred = model_fit.forecast(steps=12)
            raw_pred_series = [df[item].iloc[-1]] + list(raw_pred.values)
            pct_chg = np.diff(raw_pred_series) / raw_pred_series[:-1] * 100
            sarima_mom_results[item] = list(pct_chg)
        return sarima_mom_results

    # 전역 변수 weights를 안전하게 인자로 주입
    sarima_trends_mom = predict_all_items_sarima_mom_track(df_hist, weights)

    # 시뮬레이션 대상 항목 필터링 (weights의 키 중 실제 df_hist에 존재하는 항목만)
    target_items = [item for item in weights.keys() if item in df_hist.columns]

    sim_indices_dict = {k: [last_row[k]] for k in target_items}
    for i, f_date in enumerate(future_dates):
        f_month = f_date.month
        for item in target_items:
            if i == 0:
                sim_indices_dict[item].append(pred_indices[item])
            else:
                if "SARIMA" in current_model:
                    trend_mom = sarima_trends_mom[item][i]
                else:
                    same_m_data = hist_mom_all[hist_mom_all.index.month == f_month]
                    trend_mom = same_m_data[item].tail(3).mean()
                    if np.isnan(trend_mom): trend_mom = 0.0
                next_val = sim_indices_dict[item][-1] * (1 + trend_mom / 100)
                sim_indices_dict[item].append(next_val)

    # 가중치 총합 계산 (일반적으로 1000)
    total_weight = sum(weights[item] for item in target_items)

    hybrid_totals = []
    for step in range(1, 13):
        w_sum = sum(sim_indices_dict[item][step] * weights[item] for item in target_items)
        # 가중치 합이 1000이 아닐 경우를 대비해 total_weight로 나누어 원지수 스케일링(100 내외) 충족
        hybrid_totals.append(w_sum / total_weight)

    df_path_base = pd.DataFrame(index=future_dates, data={"Total_Index": hybrid_totals})
    base_indices_full = [last_row['Total']] + list(df_path_base["Total_Index"])
    df_path_base["MoM"] = np.diff(base_indices_full) / base_indices_full[:-1] * 100
    
    base_yoy = []
    for f_date in future_dates:
        ly_date = f_date - pd.DateOffset(years=1)
        if ly_date in df_hist.index:
            ly_total_val = df_hist.loc[ly_date, 'Total']
        else:
            closest_idx = df_hist.index[df_hist.index.get_indexer([ly_date], method='nearest')[0]]
            ly_total_val = df_hist.loc[closest_idx, 'Total']
        base_yoy.append(((df_path_base.loc[f_date, "Total_Index"] - ly_total_val) / ly_total_val) * 100)
    df_path_base["YoY"] = base_yoy

    if "table_reset_counter" not in st.session_state:
        st.session_state["table_reset_counter"] = 0
        
    model_hint = "sarima" if "SARIMA" in current_model else "avg3y"
    session_df_key = f"df_sim_report_{model_hint}_v_{st.session_state['table_reset_counter']}"

    col_sub_title, col_reset_btn = st.columns([4, 1])
    with col_sub_title:
        st.subheader("📋 시뮬레이션 반영 향후 1년 예측치 (수기 조정 가능)")
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
        st.session_state[session_df_key] = df_base_report

    editor_key = f"editor_{model_hint}_v_{st.session_state['table_reset_counter']}"
    if editor_key in st.session_state and st.session_state[editor_key].get("edited_rows"):
        changes = st.session_state[editor_key]["edited_rows"]
        df_working = st.session_state[session_df_key].copy()
        for row_idx, updated_cols in changes.items():
            if "예상 MoM (%)" in updated_cols: 
                df_working.iloc[row_idx, df_working.columns.get_loc("예상 MoM (%)")] = updated_cols["예상 MoM (%)"]

        current_index_val = last_row['Total']
        for idx in range(len(df_working)):
            mom_val = df_working.iloc[idx, df_working.columns.get_loc("예상 MoM (%)")]
            current_index_val = current_index_val * (1 + mom_val / 100)
            df_working.iloc[idx, df_working.columns.get_loc("예상 원지수")] = current_index_val
            f_date_obj = future_dates[idx]
            ly_date = f_date_obj - pd.DateOffset(years=1)
            if ly_date in df_hist.index:
                ly_total_val = df_hist.loc[ly_date, 'Total']
            else:
                closest_idx = df_hist.index[df_hist.index.get_indexer([ly_date], method='nearest')[0]]
                ly_total_val = df_hist.loc[closest_idx, 'Total']
            df_working.iloc[idx, df_working.columns.get_loc("예상 YoY (%)")] = ((current_index_val - ly_total_val) / ly_total_val) * 100

        st.session_state[session_df_key] = df_working
        st.rerun()

    def highlight_modifiable_col(s):
        style_list = [''] * len(s)
        style_list[s.index.get_loc("예상 MoM (%)")] = 'background-color: #EBF3F9; font-weight: bold;'
        return style_list

    styled_df = st.session_state[session_df_key].style.apply(highlight_modifiable_col, axis=1)
    edited_df = st.data_editor(
        styled_df, key=editor_key, disabled=["예상 원지수", "예상 YoY (%)"],
        column_config={
            "예상 MoM (%)": st.column_config.NumberColumn("🎨 예상 MoM (%)", format="%.2f%%"),
            "예상 원지수": st.column_config.NumberColumn("예상 원지수", format="%.2f"),
            "예상 YoY (%)": st.column_config.NumberColumn("예상 YoY (%)", format="%.2f%%"),
        }, use_container_width=True
    )

    st.markdown("---")
    st.subheader(f"📉 {current_model} 기반 물가 경로")
    path_view_mode = st.radio("분석할 물가 지표 선택", ["CPI 원지수", "MoM 상승률 (%)", "YoY 상승률 (%)"], horizontal=True, key="path_view_mode")

    current_sim_data = st.session_state[session_df_key]
    hist_short = df_hist[-24:] 
    hist_mom_short = hist_mom_all['Total'].loc[hist_short.index]
    hist_yoy_short = (df_hist['Total'].pct_change(12) * 100).loc[hist_short.index]

    fig_path = go.Figure()
    if "원지수" in path_view_mode:
        fig_path.add_trace(go.Scatter(x=hist_short.index, y=hist_short['Total'], name='과거 확정치', mode='lines+markers', line=dict(color='#7f7f7f', width=2.5)))
        pred_y_arr = [hist_short['Total'].iloc[-1]] + list(current_sim_data["예상 원지수"])
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
    fig_path.update_layout(xaxis_title="연월", yaxis_title=y_lbl, hovermode="x unified", height=520, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0.01))
    st.plotly_chart(fig_path, use_container_width=True)