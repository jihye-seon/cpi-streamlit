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

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# OpenSSL 3.x에만 있는 옵션이라 없는 환경에서는 0으로 떨어뜨린다
_OP_LEGACY_SERVER_CONNECT = getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)


class LegacyTLSAdapter(HTTPAdapter):
    """보안수준을 낮추고 구형 재협상을 허용하는 HTTPS 어댑터."""

    def _context(self):
        ctx = create_urllib3_context(ciphers="DEFAULT@SECLEVEL=1")
        ctx.options |= _OP_LEGACY_SERVER_CONNECT
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._context()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._context()
        return super().proxy_manager_for(*args, **kwargs)


class ResilientSession(requests.Session):
    """SSL 핸드셰이크 실패 시 완화된 TLS 설정으로 한 번 더 시도하는 세션."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._relaxed = False

        retry = Retry(total=2, backoff_factor=0.5,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET", "POST"])
        for prefix in ("http://", "https://"):
            self.mount(prefix, HTTPAdapter(max_retries=retry))

    def _relax_tls(self):
        self.mount("https://", LegacyTLSAdapter())
        self._relaxed = True

    def request(self, method, url, **kwargs):
        try:
            return super().request(method, url, **kwargs)
        except requests.exceptions.SSLError:
            if self._relaxed:
                raise
            self._relax_tls()
            return super().request(method, url, **kwargs)


def make_session(referer: str = None, warm_url: str = None,
                 timeout: int = 25) -> ResilientSession:
    """
    공용 세션 생성.
      referer  - Referer 헤더로 넣을 주소
      warm_url - 세션 쿠키 확보를 위해 미리 한 번 GET 할 주소
    """
    s = ResilientSession()
    s.headers.update({"User-Agent": DEFAULT_UA})
    if referer:
        s.headers["Referer"] = referer

    if warm_url:
        try:
            s.get(warm_url, timeout=timeout)
        except requests.RequestException:
            pass      # 워밍업 실패는 치명적이지 않다. 본 요청에서 다시 판단한다.

    return s
