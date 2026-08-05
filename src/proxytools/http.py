"""Thread-local HTTP sessions shared by network-facing modules."""

import threading

import requests

_thread_local = threading.local()


def session() -> requests.Session:
    current = getattr(_thread_local, "session", None)
    if current is None:
        current = requests.Session()
        _thread_local.session = current
    return current
