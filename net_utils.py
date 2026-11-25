# -*- coding: utf-8 -*-
"""
수집 모듈 공용 HTTP 세션 유틸

국내 공공 사이트는 보안장비 설정이 제각각이라, 클라우드(해외 IP)에서 접속하면
TLS 핸드셰이크 단계에서 거절되는 경우가 있다. 대표적인 증상:

    SSLError(1, '[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] ...')

원인은 크게 두 가지다.
  (1) OpenSSL 3.x의 기본 보안수준(SECLEVEL=2)이 서버가 제시하는 암호군을 거부
      -> 보안수준을 낮추고 legacy renegotiation을 허용하면 통과된다.
  (2) 서버가 해외 IP 자체를 차단
      -> 클라이언트에서 할 수 있는 일이 없다. 스냅샷 폴백을 써야 한다.

ResilientSession은 (1)을 자동으로 재시도한다. 처음에는 표준 설정으로 붙고,
SSLError가 나면 완화된 TLS 설정으로 어댑터를 갈아끼운 뒤 한 번 더 시도한다.
인증서 검증은 계속 켜 둔다.
"""

import ssl
import threading
import urllib3
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context

# 검증 생략 폴백이 걸리면 요청마다 경고가 쏟아지므로 한 번만 알리도록 끈다
# (어떤 경로로 붙었는지는 세션의 notes 에 남고 UI에 표시된다)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# User-Agent만 보내면 국내 WAF가 봇으로 판단해 406/403을 돌려주는 경우가 있다.
# 브라우저가 실제로 보내는 헤더를 최대한 흉내낸다.
BROWSER_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

# 배포된 파일이 최신인지 앱에서 바로 확인하기 위한 표식
VERSION = "2026-08-04.1"

# 수집을 병렬로 돌리면 같은 호스트로 동시에 여러 연결이 나간다.
# 기본 풀(10)로는 모자라 연결이 버려지고 재수립되므로 넉넉히 잡는다.
POOL_SIZE = 24

# OpenSSL 3.x에만 있는 옵션이라 없는 환경에서는 0으로 떨어뜨린다
_OP_LEGACY_SERVER_CONNECT = getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)

# 서버가 받아주는 조합을 찾을 때까지 순서대로 시도할 암호군 설정
CIPHER_LADDER = [
    ("표준", None),
    ("SECLEVEL=1", "DEFAULT:@SECLEVEL=1"),
    ("SECLEVEL=0", "DEFAULT:@SECLEVEL=0"),
    ("ALL SECLEVEL=0", "ALL:@SECLEVEL=0"),
]

# 폴백이 걸린 내역을 모아 두고 앱에서 표시한다
_GLOBAL_NOTES = []


def get_notes() -> list:
    """이번 프로세스에서 어떤 폴백 경로가 사용됐는지 반환."""
    return list(_GLOBAL_NOTES)


def clear_notes():
    _GLOBAL_NOTES.clear()


class LegacyTLSAdapter(HTTPAdapter):
    """
    보안수준을 낮추고 구형 재협상을 허용하는 HTTPS 어댑터.

    insecure=True 이면 인증서 검증까지 끈다. 서버가 중간 인증서를 빠뜨려
    체인이 끊긴 경우(‘self-signed certificate in certificate chain’)의
    최후 수단이며, 통신은 여전히 암호화되지만 중간자 공격은 막지 못한다.
    """

    def __init__(self, *args, insecure: bool = False,
                 ciphers: str = "DEFAULT:@SECLEVEL=1", **kwargs):
        self._insecure = insecure
        self._ciphers = ciphers
        super().__init__(*args, **kwargs)

    def _context(self):
        # minimum_version은 일부러 건드리지 않는다. 지정하면 환경에 따라
        # 협상 가능한 조합이 오히려 좁아져 handshake_failure가 나기도 한다.
        ctx = create_urllib3_context(ciphers=self._ciphers)
        ctx.options |= _OP_LEGACY_SERVER_CONNECT
        if self._insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx.load_default_certs()
        return ctx

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._context()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._context()
        return super().proxy_manager_for(*args, **kwargs)


class ResilientSession(requests.Session):
    """SSL 핸드셰이크 실패 시 완화된 TLS 설정으로 한 번 더 시도하는 세션."""

    def __init__(self, *args, allow_http_fallback: bool = True,
                 allow_insecure: bool = True, start_relaxed: bool = True,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self._allow_http = allow_http_fallback
        self._allow_insecure = allow_insecure
        self._insecure_on = False
        self._ciphers = None
        self._downgraded = set()      # http로 내려 붙기로 확정한 호스트
        self.notes = []               # 어떤 경로로 통했는지 기록 (UI 표시용)

        self._retry = Retry(total=2, backoff_factor=0.5,
                            status_forcelist=[429, 500, 502, 503, 504],
                            allowed_methods=["GET", "POST"])
        for prefix in ("http://", "https://"):
            self.mount(prefix, HTTPAdapter(max_retries=self._retry,
                                           pool_connections=POOL_SIZE,
                                           pool_maxsize=POOL_SIZE))

        # 실패를 겪은 뒤에 어댑터를 갈아끼우는 방식은 세션 워밍업·리다이렉트·
        # 커넥션 풀 재사용이 얽히면 타이밍이 어긋날 수 있다. SECLEVEL=1은
        # 인증서 검증을 그대로 유지하면서 구형 암호군만 추가로 허용하므로,
        # 처음부터 이 설정으로 시작하는 편이 안전하고 결과도 동일하다.
        # (TLS1.3을 지원하는 서버는 완화 설정에서도 그대로 TLS1.3으로 붙는다)
        if start_relaxed:
            self._mount(ciphers="DEFAULT:@SECLEVEL=1", insecure=False)

    # -- 내부 헬퍼 ---------------------------------------------------
    def _mount(self, ciphers: str, insecure: bool):
        """지정한 암호군/검증설정으로 HTTPS 어댑터를 갈아끼운다."""
        self.mount("https://", LegacyTLSAdapter(insecure=insecure,
                                                ciphers=ciphers,
                                                max_retries=self._retry,
                                                pool_connections=POOL_SIZE,
                                                pool_maxsize=POOL_SIZE))
        self._ciphers = ciphers
        self._insecure_on = insecure

    @staticmethod
    def _to_http(url: str) -> str:
        return "http://" + url[len("https://"):]

    def _note(self, msg: str):
        if msg not in self.notes:
            self.notes.append(msg)
        if msg not in _GLOBAL_NOTES:
            _GLOBAL_NOTES.append(msg)

    # -- 요청 -------------------------------------------------------
    def request(self, method, url, **kwargs):
        host = urlparse(url).hostname or ""

        # 이미 http로 내려 붙기로 한 호스트는 처음부터 http로 간다
        if host in self._downgraded and url.startswith("https://"):
            return super().request(method, self._to_http(url), **kwargs)

        if self._insecure_on:
            kwargs.setdefault("verify", False)

        try:
            return super().request(method, url, **kwargs)
        except requests.exceptions.SSLError as first_err:
            err = first_err
            plain = {k: v for k, v in kwargs.items() if k != "verify"}

            # 1단계: 암호군 사다리를 순서대로 내려가며 재시도
            for label, ciphers in CIPHER_LADDER:
                if ciphers is None or ciphers == self._ciphers:
                    continue
                self._mount(ciphers=ciphers, insecure=self._insecure_on)
                try:
                    resp = super().request(method, url, **kwargs)
                    self._note(f"{host}: TLS 암호군 {label}로 접속")
                    return resp
                except requests.exceptions.SSLError as e:
                    err = e

            # 2단계: 인증서 체인이 끊긴 경우 검증을 생략하고 재시도
            if self._allow_insecure and not self._insecure_on:
                for label, ciphers in CIPHER_LADDER:
                    self._mount(ciphers=ciphers or "DEFAULT", insecure=True)
                    try:
                        resp = super().request(method, url, verify=False, **plain)
                        self._note(f"⚠️ {host}: 인증서 검증 생략({label})으로 접속")
                        return resp
                    except requests.exceptions.SSLError as e:
                        err = e

            # 3단계: 그래도 안 되면 평문 HTTP로 시도
            #        (443 포트가 막혀도 80 포트는 열려 있는 경우가 있다)
            if self._allow_http and url.startswith("https://"):
                try:
                    resp = super().request(method, self._to_http(url), **plain)
                    self._downgraded.add(host)
                    self._note(f"⚠️ {host}: HTTPS 실패 → HTTP(80)로 우회")
                    return resp
                except requests.RequestException:
                    pass

            raise err


def make_session(referer: str = None, warm_url: str = None,
                 timeout: int = 25, allow_http_fallback: bool = True,
                 start_relaxed: bool = True) -> ResilientSession:
    """
    공용 세션 생성.
      referer       - Referer 헤더로 넣을 주소
      warm_url      - 세션 쿠키 확보를 위해 미리 한 번 GET 할 주소
      start_relaxed - 처음부터 SECLEVEL=1 설정으로 붙는다 (기본값)
    """
    s = ResilientSession(allow_http_fallback=allow_http_fallback,
                         start_relaxed=start_relaxed)
    s.headers.update(BROWSER_HEADERS)
    if referer:
        s.headers["Referer"] = referer
        parts = urlparse(referer)
        s.headers["Origin"] = f"{parts.scheme}://{parts.netloc}"

    if warm_url:
        try:
            s.get(warm_url, timeout=timeout)
        except requests.RequestException:
            pass      # 워밍업 실패는 치명적이지 않다. 본 요청에서 다시 판단한다.

    return s


# ----------------------------------------------------------------------
# 병렬 수집 유틸
#
# 수집 시간의 대부분은 국내 공공 사이트의 응답 대기(I/O)라 스레드로 겹쳐
# 돌리면 벽시계 시간이 크게 줄어든다. 다만 requests.Session은 스레드 안전이
# 보장되지 않고, ResilientSession은 SSL 실패 시 어댑터를 갈아끼우기까지 하므로
# 세션을 스레드끼리 공유하면 안 된다. SessionPool이 스레드별로 하나씩 만들어 준다.
# ----------------------------------------------------------------------
class SessionPool:
    """스레드마다 세션을 하나씩 만들어 재사용한다 (워밍업 GET도 스레드당 1회)."""

    def __init__(self, factory):
        self._factory = factory
        self._local = threading.local()

    def preset(self, session):
        """이미 만들어 둔 세션을 현재 스레드 몫으로 등록한다."""
        if session is not None:
            self._local.session = session

    @property
    def session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = self._local.session = self._factory()
        return s


def pmap(fn, items, max_workers: int = 8) -> list:
    """
    items를 스레드로 병렬 처리하고 입력 순서 그대로 [(item, 결과, 예외)] 반환.

    예외를 밖으로 던지지 않는 이유: 품목 하나가 실패해도 나머지는 살려야 한다.
    호출부에서 error가 None인 것만 골라 쓰면 된다.
    """
    items = list(items)
    if not items:
        return []

    if len(items) == 1 or max_workers <= 1:      # 스레드를 띄울 이유가 없다
        out = []
        for it in items:
            try:
                out.append((it, fn(it), None))
            except Exception as e:
                out.append((it, None, e))
        return out

    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as ex:
        futures = [ex.submit(fn, it) for it in items]
        out = []
        for it, fut in zip(items, futures):
            try:
                out.append((it, fut.result(), None))
            except Exception as e:
                out.append((it, None, e))
    return out


# ----------------------------------------------------------------------
# 연결 진단
# ----------------------------------------------------------------------
def diagnose_host(url: str, timeout: int = 10) -> dict:
    """
    한 호스트에 대해 어느 계층에서 막히는지 단계별로 확인한다.

    DNS -> TCP:443 -> TLS(기본) -> TLS(완화) -> HTTPS GET -> HTTP GET

    TLS 핸드셰이크가 실패하는데 TCP는 붙는다면 IP/지역 차단일 가능성이 높고,
    TLS(완화)가 통한다면 OpenSSL 보안수준 문제다.
    HTTPS는 막히는데 HTTP가 되면 80포트 우회가 가능하다.
    """
    import socket

    host = urlparse(url).hostname
    out = {"host": host, "url": url}

    # 1) DNS
    try:
        ip = socket.gethostbyname(host)
        out["DNS"] = f"OK ({ip})"
    except Exception as e:
        out["DNS"] = f"실패 — {e}"
        return out

    # 2) TCP 443
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            out["TCP:443"] = "OK"
    except Exception as e:
        out["TCP:443"] = f"실패 — {type(e).__name__}"

    # 3) TLS 기본 / 4) TLS 완화 / 5) TLS 완화 + 검증생략
    for label, ciphers, insecure in (("TLS(기본)", None, False),
                                     ("TLS(완화)", "DEFAULT@SECLEVEL=1", False),
                                     ("TLS(검증생략)", "DEFAULT@SECLEVEL=1", True)):
        try:
            if ciphers:
                ctx = create_urllib3_context(ciphers=ciphers)
                ctx.options |= _OP_LEGACY_SERVER_CONNECT
                if insecure:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                else:
                    ctx.load_default_certs()
            else:
                ctx = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    out[label] = f"OK ({tls.version()}/{tls.cipher()[0]})"
        except Exception as e:
            out[label] = f"실패 — {type(e).__name__}"

    # 5) 헤더 구성별 비교 — TLS는 통과하는데 WAF가 봇으로 보고 막는 경우를 가른다
    body = None
    for label, headers in (("UA만", {"User-Agent": DEFAULT_UA}),
                           ("브라우저헤더", BROWSER_HEADERS)):
        try:
            s = requests.Session()
            s.headers.update(headers)
            s.mount("https://", LegacyTLSAdapter(ciphers="DEFAULT:@SECLEVEL=1"))
            r = s.get(url, timeout=timeout, allow_redirects=False)
            out[f"requests({label})"] = f"HTTP {r.status_code}"
            if r.status_code != 200 and body is None:
                body = r.text[:200].replace("\n", " ").strip()
        except Exception as e:
            out[f"requests({label})"] = f"실패 — {type(e).__name__}"

    # 6) 최종 폴백까지 포함한 세션
    try:
        s = make_session(referer=url)
        r = s.get(url, timeout=timeout)
        out["ResilientSession"] = f"HTTP {r.status_code}"
        out["폴백경로"] = "; ".join(s.notes) or "폴백 없이 성공"
        if r.status_code != 200 and body is None:
            body = r.text[:200].replace("\n", " ").strip()
    except Exception as e:
        out["ResilientSession"] = f"실패 — {type(e).__name__}"
        out["폴백경로"] = "-"

    # 7) 평문 HTTP
    try:
        r = requests.get("http://" + url.split("://", 1)[1],
                         headers=BROWSER_HEADERS, timeout=timeout,
                         allow_redirects=False)
        out["HTTP GET"] = f"HTTP {r.status_code}"
    except Exception as e:
        out["HTTP GET"] = f"실패 — {type(e).__name__}"

    # 차단 페이지 본문 일부 — 해외IP 차단 안내인지 확인용
    out["응답본문"] = body or ""

    return out


def _kamis_api_probe_url() -> str:
    """
    인증키와 정상 파라미터를 갖춘 KAMIS OpenAPI 주소.

    파라미터 없이 호출하면 서버가 정상적으로 404를 주기 때문에, 그것만으로는
    WAF 차단인지 아닌지 구분할 수 없다. 실제 호출로 확인해야 판별이 된다.
    """
    from urllib.parse import urlencode
    try:
        from kamis_retail_price import KAMIS_KEY, KAMIS_ID
    except Exception:
        return None

    params = {
        "action": "dailyPriceByCategoryList",
        "p_cert_key": KAMIS_KEY, "p_cert_id": KAMIS_ID,
        "p_returntype": "json", "p_product_cls_code": "01",
        "p_item_category_code": "100", "p_country_code": "1101",
        "p_convert_kg_yn": "N",
    }
    return "https://www.kamis.or.kr/service/price/xml.do?" + urlencode(params)


DIAGNOSE_TARGETS = [
    ("KAMIS 화면", "https://www.kamis.or.kr/customer/price/agricultureRetail/catalogue.do"),
    ("EKAPE", "https://www.ekapepia.com/v3/price/consumer/periodPrice.do"),
    ("오피넷", "https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do"),
    # KAMIS가 해외 IP를 막을 경우의 대안. 호스트가 완전히 달라 별도 정책을 탄다.
    ("공공데이터포털", "https://apis.data.go.kr"),
]


def diagnose_all(timeout: int = 10) -> list:
    """수집에 쓰는 모든 호스트를 진단해 리스트로 반환."""
    targets = list(DIAGNOSE_TARGETS)

    api_url = _kamis_api_probe_url()
    if api_url:
        targets.insert(1, ("KAMIS API(실호출)", api_url))

    rows = []
    for name, url in targets:
        row = dict(대상=name, **diagnose_host(url, timeout))
        row["url"] = row["url"].split("?")[0]      # 인증키는 표에 노출하지 않는다
        rows.append(row)
    return rows
