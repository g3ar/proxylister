"""HTTP session lifecycles for direct services and unbounded proxy lists.

Direct upstream calls reuse one session per worker thread. Proxy calls must use
``proxy_session``: Requests caches a connection manager for every distinct
proxy URL, so reusing one session across an ever-changing public proxy list
would retain sockets until the process exhausts its file-descriptor limit.
"""

from contextlib import contextmanager
import threading

import requests

_thread_local = threading.local()


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
    try:
        yield current
    finally:
        current.close()
