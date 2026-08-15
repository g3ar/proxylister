import ctypes
import unittest
from unittest.mock import Mock, patch

from proxylister import clipboard


class ClipboardTests(unittest.TestCase):
    def test_non_windows_has_no_native_clipboard_path(self):
        with patch.object(clipboard.sys, "platform", "linux"):
            self.assertFalse(clipboard.copy_to_system_clipboard("http://proxy"))

    def test_windows_copies_unicode_and_transfers_memory_ownership(self):
        user32 = Mock()
        kernel32 = Mock()
        memory = ctypes.create_string_buffer(512)
        kernel32.GlobalAlloc.return_value = 42
        kernel32.GlobalLock.return_value = ctypes.addressof(memory)
        user32.OpenClipboard.return_value = True
        user32.EmptyClipboard.return_value = True
        user32.SetClipboardData.return_value = 42
        connection = "socks5://proxy-Київ:1080"

        with patch.object(clipboard.sys, "platform", "win32"), patch.object(
            clipboard.ctypes,
            "WinDLL",
            side_effect=(user32, kernel32),
            create=True,
        ):
            self.assertTrue(clipboard.copy_to_system_clipboard(connection))

        wchar_size = ctypes.sizeof(ctypes.c_wchar)
        encoding = "utf-16-le" if wchar_size == 2 else "utf-32-le"
        copied = memory.raw[: (len(connection) + 1) * wchar_size]
        self.assertEqual(copied.decode(encoding).rstrip("\0"), connection)
        user32.SetClipboardData.assert_called_once_with(clipboard.CF_UNICODETEXT, 42)
        user32.CloseClipboard.assert_called_once_with()
        kernel32.GlobalFree.assert_not_called()
