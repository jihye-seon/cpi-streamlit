# 국내 호스팅 배포 가이드

## 왜 국내여야 하는가

KAMIS(`www.kamis.or.kr`)는 웹방화벽에서 해외 IP를 차단한다. 진단으로 확인된 내용:

| | 국내 IP | 해외 IP (Streamlit Cloud) |
|---|---|---|
| TLS 협상 | TLSv1.3 / AES-256-GCM | 실패 → 구형 프론트엔드로 라우팅 |
| HTTP 응답 | 200 | **406** + `Web firewall ... blocked` |

TLS 계층에서 이미 갈리므로 **User-Agent 위장이나 API 전환으로는 우회할 수 없다.**
화면(`/customer/...`)과 OpenAPI(`/service/price/xml.do`)가 동일하게 차단된다.

오피넷·EKAPE는 해외에서도 정상 동작하므로, **KAMIS 하나 때문에 국내 실행이 필요하다.**

국내에서 실행하면 세 소스 모두 실시간으로 붙고, 스냅샷 폴백은 예비 수단으로만 남는다.

---

## 1. 서버 준비

국내 리전이면 어디든 된다. 최소 사양은 **1 vCPU / 2GB RAM / 10GB** 정도면 충분하다.

| 사업자 | 비고 |
|---|---|
| 사내 서버 / 온프레미스 | 가장 확실하고 비용 없음. 사내망 전용이면 이 선택이 최선 |
| 네이버클라우드 · NHN · KT Cloud | 국내 사업자, IP도 확실히 국내 |
| AWS 서울(ap-northeast-2) · GCP asia-northeast3 | 서울 리전 IP는 국내로 지오로케이션됨 |
| Oracle Cloud 서울 | Always Free 등급 있음 |

> AWS·GCP는 해외 기업이지만 서울 리전 IP는 대개 통과한다. 다만 일부 차단 정책은
> 국가가 아니라 데이터센터 ASN 자체를 막기도 하므로, 100% 확실하게 가려면
> 국내 사업자나 사내 서버를 쓴다. **배포 후 반드시 연결 진단으로 확인할 것.**

---

## 2-A. Docker로 배포 (권장)

```bash
# 소스 가져오기
git clone <저장소 주소> /opt/cpi && cd /opt/cpi

# 빌드 & 실행
docker compose up -d --build

# 상태 확인
docker compose ps
docker compose logs -f
```

엑셀을 교체할 때는 이미지 재빌드 없이 파일만 바꾸고 `docker compose restart`.

## 2-B. Docker 없이 배포

```bash
sudo apt update && sudo apt install -y python3-venv git
sudo git clone <저장소 주소> /opt/cpi
cd /opt/cpi
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# systemd 등록
sudo cp deploy/cpi-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cpi-dashboard
journalctl -u cpi-dashboard -f
```

`cpi-dashboard.service`의 `User=`, `WorkingDirectory=`는 환경에 맞게 수정한다.

---

## 3. 배포 후 검증 (필수)

브라우저로 `http://<서버주소>:8501` 접속 후:

1. **탭2 → 🔧 연결 진단 → 진단 실행**
   - `KAMIS 화면` 행이 **HTTP 200** 이어야 한다. 406이면 그 서버 IP도 차단된 것이므로
     다른 사업자/리전으로 옮겨야 한다.
2. **탭2 → 🌐 실시간 시세 가져오기**
   - 두 metric의 `출처:` 가 모두 **`실시간`** 이면 성공. `스냅샷`이면 수집이 실패한 것.

---

## 4. 운영 시 주의사항

**타임존** — 수집 로직이 `date.today()` 기준으로 조회월을 정한다. UTC로 두면 월초·월말에
조회월이 하루 어긋날 수 있다. Dockerfile과 systemd 유닛에 `TZ=Asia/Seoul`이 들어 있으니
직접 실행할 때도 반드시 설정한다.

**접근 제어** — Streamlit에는 인증 기능이 없다. 외부에 공개한다면 nginx 리버스 프록시로
Basic 인증이나 IP 제한을 걸고, `.streamlit/config.toml`의 `showErrorDetails`를 `false`로
바꾼다(트레이스백에 KAMIS 인증키가 포함된 URL이 노출될 수 있다).

```nginx
location / {
    proxy_pass http://127.0.0.1:8501;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;   # WebSocket 필수
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    auth_basic "CPI";
    auth_basic_user_file /etc/nginx/.htpasswd;
}
```

`Upgrade`/`Connection` 헤더가 없으면 Streamlit이 접속 후 멈춘다.

**수집 소요 시간** — 첫 수집은 EKAPE 19회 + KAMIS 5회 + 오피넷 3회로 30~40초가 걸린다.
결과는 1시간 캐시된다.

**스냅샷 자동화 불필요** — `update_snapshot.bat`(작업 스케줄러)는 Streamlit Cloud용
차선책이었다. 국내 호스팅으로 옮기면 실시간 수집이 되므로 **등록하지 않아도 된다.**
`live_snapshot.json`은 수집 실패 시의 폴백으로만 남겨 둔다.

---

## 5. 배포에 필요한 파일

```
cpi_estimation_9.py            앱 본체
net_utils.py                   공용 HTTP/TLS 세션 + 연결 진단
opinet_daily_price.py          석유제품 수집
kamis_retail_price.py          농·수산물 수집 + 가중치 매칭 + 가중합산
ekape_livestock_price.py       축산물 수집
CPI data_202605_v1.xlsx        CPI 데이터 · 가중치(kamis_name) · 원지수(detail)
농축수산물 품목 및 등급 코드표.xlsx    (참고용, 앱은 사용하지 않음)
requirements.txt               의존성 (lxml 포함 — 없으면 파싱 실패)
.streamlit/config.toml         서버 설정
Dockerfile / docker-compose.yml
deploy/cpi-dashboard.service
live_snapshot.json             수집 실패 시 폴백 (선택)
make_snapshot.py               스냅샷 생성기 (선택)
rent_cpi_kb.py                 집세 — KB 전세지수 기반 예측 (선택)
kb_jeonse.csv                  KB 전국 전세지수·전세수급지수 (39KB, 선택)
extract_kb_jeonse.py           kb.db -> kb_jeonse.csv 추출기 (배포 불필요)
```

### 집세 KB 옵션

탭2 "📈 2. 추세적 품목 예상치"의 **🏠 KB 전세지수 반영** 버튼을 누를 때만 동작한다.
기본값은 SARIMA이며, `rent_cpi_kb.py` 나 `kb_jeonse.csv` 가 없으면 버튼이 뜨지 않고
앱은 그대로 SARIMA로 돌아간다. 외부 네트워크 호출은 없다.

`kb_jeonse.csv` 는 KB 주간 자료 중 전국 전세지수·전세수급지수 두 계열만 담은
39KB 파일이다. 원본 `kb.db`(29MB)는 배포에 넣지 않는다.

새 `kb.db` 를 받으면 로컬에서 한 번 실행해 CSV를 갱신하고 커밋한다.

~~~bash
python extract_kb_jeonse.py --kb-db "경로/kb.db"
~~~

**갱신 주기** — KB 지연을 1~52주로 늘려 재측정한 결과 집세 MAE는 0.01822(최신)
→ 0.01832(26주) → 0.01913(52주)였다. 26주까지는 사실상 영향이 없으므로 급하지 않다.
26주를 넘으면 앱이 경고를 띄우고, 39주를 넘으면 KB 반영이 역효과가 되므로 옵션이
자동으로 꺼진다(SARIMA 유지).

**연 1회 유지보수** — `rent_cpi_kb.py` 의 `CPI_RELEASE_CALENDAR` 에 해당 연도의 CPI
실제 공표일을 넣는다. 비어 있으면 '익월 7일' 가정으로 떨어져 예측 시점의 정보집합이
며칠 어긋날 수 있다. 현재 2026년분만 들어 있다.
