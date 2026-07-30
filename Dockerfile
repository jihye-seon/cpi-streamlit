# ======================================================================
#  CPI 시뮬레이터 - 국내 호스팅용 이미지
#
#  KAMIS 웹방화벽이 해외 IP를 차단하므로 국내 리전에서 실행해야
#  실시간 수집이 동작한다. (오피넷·EKAPE는 어디서든 동작)
#
#  빌드 : docker build -t cpi-dashboard .
#  실행 : docker run -d --name cpi -p 8501:8501 --restart unless-stopped cpi-dashboard
# ======================================================================
FROM python:3.13-slim

# 수집 로직이 date.today() 기준으로 조회월을 정하므로 KST 고정이 필수.
# UTC로 두면 월초·월말에 조회월이 하루 어긋날 수 있다.
ENV TZ=Asia/Seoul \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 의존성을 먼저 넣어 레이어 캐시를 활용한다
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 + 수집 모듈 + 데이터
COPY .streamlit/ ./.streamlit/
COPY net_utils.py opinet_daily_price.py kamis_retail_price.py \
     ekape_livestock_price.py make_snapshot.py cpi_estimation_9.py ./
COPY *.xlsx ./
# 스냅샷은 없어도 동작한다 (수집 실패 시의 폴백일 뿐)
COPY live_snapshot.json* ./

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8501/_stcore/health').read()"

CMD ["streamlit", "run", "cpi_estimation_9.py"]
