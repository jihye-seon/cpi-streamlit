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
import urllib3
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context

# 검증 생략 폴백이 걸리면 요청마다 경고가 쏟아지므로 한 번만 알리도록 끈다
# (어떤 경로로 붙었는지는 세션의 notes 에 남고 UI에 표시된다)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# OpenSSL 3.x에만 있는 옵션이라 없는 환경에서는 0으로 떨어뜨린다
_OP_LEGACY_SERVER_CONNECT = getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)

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

    def __init__(self, *args, insecure: bool = False, **kwargs):
        self._insecure = insecure
        super().__init__(*args, **kwargs)

    def _context(self):
        ctx = create_urllib3_context(ciphers="DEFAULT@SECLEVEL=1")
        ctx.options |= _OP_LEGACY_SERVER_CONNECT
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
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
                 allow_insecure: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self._stage = 0               # 0=표준 1=완화 2=검증생략
        self._allow_http = allow_http_fallback
        self._allow_insecure = allow_insecure
        self._downgraded = set()      # http로 내려 붙기로 확정한 호스트
        self.notes = []               # 어떤 경로로 통했는지 기록 (UI 표시용)

        retry = Retry(total=2, backoff_factor=0.5,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET", "POST"])
        for prefix in ("http://", "https://"):
            self.mount(prefix, HTTPAdapter(max_retries=retry))

    # -- 내부 헬퍼 ---------------------------------------------------
    def _mount_stage(self, stage: int):
        """1=암호군 완화, 2=완화 + 인증서 검증 생략"""
        self.mount("https://", LegacyTLSAdapter(insecure=(stage >= 2)))
        self._stage = stage

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

        # 이미 검증 생략 단계면 verify 인자를 맞춰 준다
        if self._stage >= 2:
            kwargs.setdefault("verify", False)

        try:
            return super().request(method, url, **kwargs)
        except requests.exceptions.SSLError as first_err:
            err = first_err

            # 1단계: OpenSSL 보안수준을 낮춰 재시도
            #        (KAMIS는 SHA1 CBC 암호군만 받아들여 SECLEVEL=2에서 거절됨)
            if self._stage < 1:
                self._mount_stage(1)
                try:
                    resp = super().request(method, url, **kwargs)
                    self._note(f"{host}: TLS 암호군 완화(SECLEVEL=1)로 접속")
                    return resp
                except requests.exceptions.SSLError as e:
                    err = e

            # 2단계: 인증서 체인이 끊긴 경우 검증을 생략하고 재시도
            if self._allow_insecure and self._stage < 2:
                self._mount_stage(2)
                try:
                    resp = super().request(method, url, verify=False,
                                           **{k: v for k, v in kwargs.items()
                                              if k != "verify"})
                    self._note(f"⚠️ {host}: 서버 인증서 체인 불완전 → 검증 생략하고 접속")
                    return resp
                except requests.exceptions.SSLError as e:
                    err = e

            # 3단계: 그래도 안 되면 평문 HTTP로 시도
            #        (443 포트가 막혀도 80 포트는 열려 있는 경우가 있다)
            if self._allow_http and url.startswith("https://"):
                try:
                    resp = super().request(method, self._to_http(url), **kwargs)
                    self._downgraded.add(host)
                    self._note(f"⚠️ {host}: HTTPS 실패 → HTTP(80)로 우회")
                    return resp
                except requests.RequestException:
                    pass

            raise err


def make_session(referer: str = None, warm_url: str = None,
                 timeout: int = 25, allow_http_fallback: bool = True
                 ) -> ResilientSession:
    """
    공용 세션 생성.
      referer  - Referer 헤더로 넣을 주소
      warm_url - 세션 쿠키 확보를 위해 미리 한 번 GET 할 주소
    """
    s = ResilientSession(allow_http_fallback=allow_http_fallback)
    s.headers.update({"User-Agent": DEFAULT_UA})
    if referer:
        s.headers["Referer"] = referer

    if warm_url:
        try:
            s.get(warm_url, timeout=timeout)
        except requests.RequestException:
            pass      # 워밍업 실패는 치명적이지 않다. 본 요청에서 다시 판단한다.

    return s


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

    # 5) HTTPS GET / 6) HTTP GET
    headers = {"User-Agent": DEFAULT_UA}
    for label, target in (("HTTPS GET", url),
                          ("HTTP GET", "http://" + url.split("://", 1)[1])):
        try:
            r = requests.get(target, headers=headers, timeout=timeout,
                             allow_redirects=False)
            out[label] = f"HTTP {r.status_code}"
        except Exception as e:
            out[label] = f"실패 — {type(e).__name__}"

    return out


DIAGNOSE_TARGETS = [
    ("KAMIS", "https://www.kamis.or.kr/customer/price/agricultureRetail/catalogue.do"),
    ("KAMIS API", "https://www.kamis.or.kr/service/price/xml.do"),
    ("EKAPE", "https://www.ekapepia.com/v3/price/consumer/periodPrice.do"),
    ("오피넷", "https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do"),
]


def diagnose_all(timeout: int = 10) -> list:
    """수집에 쓰는 모든 호스트를 진단해 리스트로 반환."""
    return [dict(대상=name, **diagnose_host(url, timeout))
            for name, url in DIAGNOSE_TARGETS]
