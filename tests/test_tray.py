import ctypes
import os
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from campusnet import tray


class TrayContractTests(unittest.TestCase):
    def test_tooltip_preserves_unicode_and_fits_windows_buffer(self):
        text = tray._tooltip("校园网\x00\n" + "🙂" * 100)
        self.assertLessEqual(len(text.encode("utf-16-le")), 254)
        self.assertNotIn("\n", text)
        self.assertNotIn("\x00", text)

    def test_icon_png_has_valid_signature_and_rgba_dimensions(self):
        png = tray.icon_png(24)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack_from(">IIBBBBB", png, 16), (24, 24, 8, 6, 0, 0, 0))
        self.assertTrue(png.endswith(b"IEND\xaeB`\x82"))
        length = struct.unpack_from(">I", png, 33)[0]
        rgba_rows = __import__("zlib").decompress(png[41:41 + length])
        self.assertIn((37, 99, 235, 255),
                      [tuple(rgba_rows[index:index + 4]) for index in range(0, len(rgba_rows), 4)])
        self.assertIn((255, 255, 255, 255),
                      [tuple(rgba_rows[index:index + 4]) for index in range(0, len(rgba_rows), 4)])
        with self.assertRaises(ValueError):
            tray.icon_png(129)

    def test_double_click_and_explorer_restart_dispatch_without_ui(self):
        on_open, on_exit = Mock(), Mock()
        icon = tray.TrayIcon(on_open, on_exit)
        icon._api = SimpleNamespace(user=Mock(), shell=Mock())
        icon._taskbar_message = 0xc123
        icon._window = 7
        icon._added = True
        with patch.object(icon, "_add_icon") as add, patch.object(icon, "_menu") as menu:
            icon._dispatch(7, tray._WM_TRAY, 0, (1 << 16) | 0x0203)
            on_open.assert_called_once()
            icon._dispatch(7, tray._WM_TRAY, 0, (1 << 16) | 0x007b)
            menu.assert_called_once()
            icon._dispatch(7, 0xc123, 0, 0)
            add.assert_called_once()
            self.assertFalse(icon._added)
        on_exit.assert_not_called()

    def test_callback_exception_is_logged_and_never_crosses_native_boundary(self):
        log = Mock()
        icon = tray.TrayIcon(Mock(side_effect=RuntimeError("callback failure")), Mock(), log)
        self.assertEqual(icon._procedure(1, tray._WM_TRAY, 0, 0x0203), 0)
        log.assert_called_once()

    def test_start_reports_native_initialization_failure_and_stop_is_safe(self):
        icon = tray.TrayIcon(Mock(), Mock())
        with patch.object(tray, "_tray_api", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(OSError, "unavailable"):
                icon.start()
        icon.stop()
        self.assertFalse(icon._thread.is_alive())

    def test_notification_never_requests_a_balloon(self):
        icon = tray.TrayIcon(Mock(), Mock())
        api = SimpleNamespace(user=Mock(), shell=Mock())
        icon._api = api
        icon._window, icon._icon = 7, 8
        notifications = []
        def notify(operation, data):
            notifications.append((operation, data._obj.flags, data._obj.info))
            return True
        api.shell.Shell_NotifyIconW.side_effect = notify
        icon._add_icon()
        self.assertEqual([entry[0] for entry in notifications], [0, 4])
        self.assertTrue(all(not flags & 0x10 and not info for _, flags, info in notifications))


@unittest.skipUnless(os.name == "nt", "Win32 event and icon APIs")
class WindowsContractTests(unittest.TestCase):
    def test_show_signal_is_auto_reset_and_request_does_not_create_it(self):
        name = "Local\\CampusNet-Test-" + uuid.uuid4().hex
        self.assertFalse(tray.request_show(name))
        signal = tray.ShowSignal(name).create()
        try:
            self.assertFalse(signal.poll())
            self.assertTrue(tray.request_show(name))
            self.assertTrue(signal.poll())
            self.assertFalse(signal.poll())
        finally:
            signal.close()
            signal.close()
        self.assertFalse(tray.request_show(name))

    def test_win32_accepts_icon_resource_without_creating_a_window(self):
        resource = tray._icon_resource()
        self.assertEqual(struct.unpack_from("<IiiHH", resource), (40, 32, 64, 1, 32))
        self.assertEqual(len(resource), 40 + 32 * 32 * 4 + 32 * 4)
        api = tray._tray_api()
        buffer = (ctypes.c_ubyte * len(resource)).from_buffer_copy(resource)
        icon = api.user.CreateIconFromResourceEx(buffer, len(resource), True, 0x00030000, 32, 32, 0)
        self.assertTrue(icon)
        api.user.DestroyIcon(icon)
        self.assertEqual(ctypes.sizeof(tray._Notification), 976 if ctypes.sizeof(ctypes.c_void_p) == 8 else 956)


if __name__ == "__main__":
    unittest.main()
