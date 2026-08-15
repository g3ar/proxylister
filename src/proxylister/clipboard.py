"""Copy monitor values through the native Windows clipboard when available.

Windows Console does not consistently implement the OSC 52 terminal sequence,
especially on Windows 10.  The monitor therefore uses the Win32 Unicode
clipboard there while terminals on other platforms retain Textual's OSC 52
path.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def copy_to_system_clipboard(text: str) -> bool:
    """Copy *text* natively on Windows; report whether a native path exists."""
    if sys.platform != "win32":
        return False
    _copy_windows_unicode(text)
    return True


def _copy_windows_unicode(text: str) -> None:
    """Put Unicode text on the Win32 clipboard without external dependencies."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
    kernel32.GlobalFree.restype = wintypes.HANDLE

    contents = ctypes.create_unicode_buffer(text)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, ctypes.sizeof(contents))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())

    transferred = False
    clipboard_open = False
    try:
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ctypes.memmove(pointer, ctypes.addressof(contents), ctypes.sizeof(contents))
        finally:
            kernel32.GlobalUnlock(handle)

        if not user32.OpenClipboard(None):
            raise ctypes.WinError(ctypes.get_last_error())
        clipboard_open = True
        if not user32.EmptyClipboard():
            raise ctypes.WinError(ctypes.get_last_error())
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise ctypes.WinError(ctypes.get_last_error())
        transferred = True
    finally:
        if clipboard_open:
            user32.CloseClipboard()
        if not transferred:
            kernel32.GlobalFree(handle)
