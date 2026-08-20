"""Restore system library lookup while a frozen app starts external programs."""

from __future__ import annotations

from contextlib import contextmanager
import os
import sys


def _set_windows_dll_directory(path: str | None) -> None:
    import ctypes

    function = ctypes.windll.kernel32.SetDllDirectoryW
    function.argtypes = [ctypes.c_wchar_p]
    function.restype = ctypes.c_bool
    if not function(path):
        raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def external_program_environment():
    """Temporarily undo PyInstaller's inherited external-library overrides."""
    if not getattr(sys, "frozen", False):
        yield
        return

    if sys.platform == "win32":
        _set_windows_dll_directory(None)
        try:
            yield
        finally:
            _set_windows_dll_directory(os.fspath(getattr(sys, "_MEIPASS")))
        return

    if sys.platform == "darwin":
        key = "DYLD_LIBRARY_PATH"
    elif sys.platform.startswith("aix"):
        key = "LIBPATH"
    else:
        key = "LD_LIBRARY_PATH"
    current = os.environ.get(key)
    original = os.environ.get(f"{key}_ORIG")
    if original is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = original
    try:
        yield
    finally:
        if current is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = current
