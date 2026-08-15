"""HTTP session lifecycles for direct services and unbounded proxy lists.

Direct upstream calls reuse one session per worker thread. Proxy calls must use
``proxy_session``: Requests caches a connection manager for every distinct
proxy URL, so reusing one session across an ever-changing public proxy list
would retain sockets until the process exhausts its file-descriptor limit.

Proxy sessions share only one immutable default TLS trust context. Loading the
CA bundle for every short-lived session is particularly expensive on Windows;
connection pools and sockets remain isolated and are still closed after every
logical check.
"""

from contextlib import contextmanager
from functools import lru_cache
import threading

import requests
from requests.adapters import DEFAULT_CA_BUNDLE_PATH, HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

_thread_local = threading.local()


@lru_cache(maxsize=1)
def _default_tls_context():
    context = create_urllib3_context()
    context.load_verify_locations(DEFAULT_CA_BUNDLE_PATH)
    return context


class _ProxyTLSAdapter(HTTPAdapter):
    """Reuse default trust data without retaining any proxy connection pool."""

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host, pool = super().build_connection_pool_key_attributes(request, verify, cert)
        if verify is True:
            pool["ssl_context"] = _default_tls_context()
            pool.pop("ca_certs", None)
            pool.pop("ca_cert_dir", None)
        return host, pool

    def cert_verify(self, conn, url, verify, cert):
        super().cert_verify(conn, url, verify, cert)
        if url.lower().startswith("https") and verify is True:
            conn.ca_certs = None
            conn.ca_cert_dir = None


def session() -> requests.Session:
    current = getattr(_thread_local, "session", None)
    if current is None:
        current = requests.Session()
        _thread_local.session = current
    return current


@contextmanager
def proxy_session():
    """Yield a session whose proxy pools are closed after one logical check."""
    current = requests.Session()
    current.mount("https://", _ProxyTLSAdapter())
    try:
        yield current
    finally:
        current.close()
