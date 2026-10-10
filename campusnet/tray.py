"""A windowless Win32 tray and a signal for opening the existing settings UI."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import os
import struct
import threading
import zlib
from types import SimpleNamespace

SHOW_EVENT_NAME = "Local\\CampusNet-ShowSettings"
_WM_TRAY = 0x8001
_WM_UPDATE = 0x8002
_WM_CLOSE = 0x0010
_WM_DESTROY = 0x0002
_WM_TIMER = 0x0113
_OPEN, _EXIT = 1001, 1002
_LRESULT = ctypes.c_ssize_t
_WPARAM = ctypes.c_size_t
_LPARAM = ctypes.c_ssize_t
_WNDPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    _LRESULT, wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM)


class _WindowClass(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("procedure", _WNDPROC),
                ("class_extra", ctypes.c_int), ("window_extra", ctypes.c_int),
                ("instance", wintypes.HINSTANCE), ("icon", wintypes.HICON),
                ("cursor", wintypes.HANDLE), ("background", wintypes.HBRUSH),
                ("menu", wintypes.LPCWSTR), ("name", wintypes.LPCWSTR)]


class _Guid(ctypes.Structure):
    _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD),
                ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]


class _Notification(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("window", wintypes.HWND),
                ("identifier", wintypes.UINT), ("flags", wintypes.UINT),
                ("message", wintypes.UINT), ("icon", wintypes.HICON),
                ("tip", wintypes.WCHAR * 128), ("state", wintypes.DWORD),
                ("state_mask", wintypes.DWORD), ("info", wintypes.WCHAR * 256),
                ("version", wintypes.UINT), ("info_title", wintypes.WCHAR * 64),
                ("info_flags", wintypes.DWORD), ("guid", _Guid),
                ("balloon_icon", wintypes.HICON)]


def _bind(dll, name, result, arguments):
    function = getattr(dll, name)
    function.restype, function.argtypes = result, arguments
    return function


def _event_api():
    if os.name != "nt":
        raise OSError("Windows is required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _bind(kernel, "CreateEventW", wintypes.HANDLE,
          [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR])
    _bind(kernel, "OpenEventW", wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR])
    _bind(kernel, "SetEvent", wintypes.BOOL, [wintypes.HANDLE])
    _bind(kernel, "WaitForSingleObject", wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD])
    _bind(kernel, "CloseHandle", wintypes.BOOL, [wintypes.HANDLE])
    return kernel


class ShowSignal:
    """An auto-reset event. Only the existing application's owner creates it."""

    def __init__(self, name=SHOW_EVENT_NAME):
        self.name = name
        self._handle = None
        self._api = None

    def create(self):
        if self._handle is None:
            self._api = _event_api()
            self._handle = self._api.CreateEventW(None, False, False, self.name)
            if not self._handle:
                raise ctypes.WinError(ctypes.get_last_error())
        return self

    def poll(self):
        if self._handle is None:
            return False
        result = self._api.WaitForSingleObject(self._handle, 0)
        if result == 0:  # WAIT_OBJECT_0; auto-reset consumes this notification.
            return True
        if result == 258:  # WAIT_TIMEOUT
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self._handle is not None:
            self._api.CloseHandle(self._handle)
            self._handle = None


def request_show(name=SHOW_EVENT_NAME):
    """Notify an existing instance, returning False if its event is absent."""
    api = _event_api()
    handle = api.OpenEventW(0x0002, False, name)  # EVENT_MODIFY_STATE
    if not handle:
        error = ctypes.get_last_error()
        if error == 2:  # ERROR_FILE_NOT_FOUND; do not create a replacement event.
            return False
        raise ctypes.WinError(error)
    try:
        if not api.SetEvent(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        return True
    finally:
        api.CloseHandle(handle)


def _tooltip(text):
    text = str(text).replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    return text.encode("utf-16-le", errors="replace")[:254].decode("utf-16-le", errors="ignore")


def _icon_pixels(size=32):
    """Return RGBA rows for the tray's blue-and-white Wi-Fi icon."""
    pixels = bytearray()
    samples = 4
    for y in reversed(range(size)):  # A DIB is stored from bottom to top.
        for x in range(size):
            red = green = blue = alpha = 0
            for sy in range(samples):
                for sx in range(samples):
                    px = (x + (sx + 0.5) / samples) * 32 / size
                    py = (y + (sy + 0.5) / samples) * 32 / size
                    dx, dy = max(7 - px, px - 25, 0), max(7 - py, py - 25, 0)
                    if dx * dx + dy * dy > 49:
                        continue
                    radius = math.hypot(px - 16, 25 - py)
                    angle = abs(math.atan2(px - 16, 25 - py))
                    white = radius <= 1.9 or (
                        angle <= 0.88 and any(abs(radius - ring) <= 1.05 for ring in (5.7, 10.9, 16.1)))
                    r, g, b = (255, 255, 255) if white else (37, 99, 235)
                    red += r
                    green += g
                    blue += b
                    alpha += 255
            divisor = samples * samples
            pixels.extend((blue // divisor, green // divisor, red // divisor, alpha // divisor))
    return bytes(pixels)


def icon_png(size=32):
    """Return a standard RGBA PNG for Tk ``PhotoImage(data=...)``."""
    if not 1 <= int(size) <= 128:
        raise ValueError("icon size must be between 1 and 128")
    size = int(size)
    # DIB is bottom-up; PNG scanlines are top-down. Re-rendering with rows
    # reversed keeps this function independent from Win32's icon structures.
    bgra = _icon_pixels(size)
    row = size * 4
    raw = bytearray()
    for index in range((size - 1) * row, -1, -row):
        raw.append(0)
        for pixel in range(index, index + row, 4):
            raw.extend((bgra[pixel + 2], bgra[pixel + 1], bgra[pixel], bgra[pixel + 3]))
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def _icon_resource(size=32):
    """A small antialiased blue rounded square with a white Wi-Fi symbol."""
    pixels = _icon_pixels(size)
    mask = bytes(((size + 31) // 32) * 4 * size)
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
    return header + pixels + mask


def _tray_api():
    if os.name != "nt":
        raise OSError("Windows is required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    handle, uint, boolean = wintypes.HANDLE, wintypes.UINT, wintypes.BOOL
    _bind(kernel, "GetModuleHandleW", wintypes.HMODULE, [wintypes.LPCWSTR])
    _bind(user, "RegisterClassW", wintypes.WORD, [ctypes.POINTER(_WindowClass)])
    _bind(user, "UnregisterClassW", boolean, [wintypes.LPCWSTR, wintypes.HINSTANCE])
    _bind(user, "CreateWindowExW", wintypes.HWND,
          [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
           wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p])
    _bind(user, "DestroyWindow", boolean, [wintypes.HWND])
    _bind(user, "DefWindowProcW", _LRESULT, [wintypes.HWND, uint, _WPARAM, _LPARAM])
    _bind(user, "GetMessageW", wintypes.BOOL, [ctypes.POINTER(wintypes.MSG), wintypes.HWND, uint, uint])
    _bind(user, "TranslateMessage", boolean, [ctypes.POINTER(wintypes.MSG)])
    _bind(user, "DispatchMessageW", _LRESULT, [ctypes.POINTER(wintypes.MSG)])
    _bind(user, "PostMessageW", boolean, [wintypes.HWND, uint, _WPARAM, _LPARAM])
    _bind(user, "PostQuitMessage", None, [ctypes.c_int])
    _bind(user, "RegisterWindowMessageW", uint, [wintypes.LPCWSTR])
    _bind(user, "CreateIconFromResourceEx", wintypes.HICON,
          [ctypes.POINTER(ctypes.c_ubyte), wintypes.DWORD, boolean, wintypes.DWORD, ctypes.c_int, ctypes.c_int, uint])
    _bind(user, "DestroyIcon", boolean, [wintypes.HICON])
    _bind(user, "CreatePopupMenu", wintypes.HMENU, [])
    _bind(user, "AppendMenuW", boolean, [wintypes.HMENU, uint, ctypes.c_size_t, wintypes.LPCWSTR])
    _bind(user, "DestroyMenu", boolean, [wintypes.HMENU])
    _bind(user, "GetCursorPos", boolean, [ctypes.POINTER(wintypes.POINT)])
    _bind(user, "SetForegroundWindow", boolean, [wintypes.HWND])
    _bind(user, "TrackPopupMenu", uint,
          [wintypes.HMENU, uint, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p])
    _bind(user, "SetTimer", ctypes.c_size_t, [wintypes.HWND, ctypes.c_size_t, uint, ctypes.c_void_p])
    _bind(user, "KillTimer", boolean, [wintypes.HWND, ctypes.c_size_t])
    _bind(shell, "Shell_NotifyIconW", boolean, [wintypes.DWORD, ctypes.POINTER(_Notification)])
    return SimpleNamespace(kernel=kernel, user=user, shell=shell)


class TrayIcon:
    """Callbacks run on the tray thread; a GUI should forward them to its queue."""

    def __init__(self, on_open, on_exit, logger=None):
        self.on_open, self.on_exit = on_open, on_exit
        self.logger = logger
        self._text = "校园网自动登录"
        self._thread = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._error = None
        self._api = None
        self._window = None
        self._icon = None
        self._added = False
        self._taskbar_message = 0

    def _log(self, message):
        if self.logger is not None:
            try:
                self.logger(message, "warn")
            except Exception:
                pass

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._ready.clear()
            self._stopping.clear()
            self._error = None
            self._thread = threading.Thread(target=self._run, name="CampusNet-Tray", daemon=True)
            self._thread.start()
        if not self._ready.wait(10):
            self._stopping.set()
            raise TimeoutError("托盘初始化超时")
        if self._error is not None:
            raise self._error
        return self

    def update(self, text):
        self._text = _tooltip(text)
        if self._window is not None:
            self._api.user.PostMessageW(self._window, _WM_UPDATE, 0, 0)

    def stop(self):
        self._stopping.set()
        if self._window is not None:
            if not self._api.user.PostMessageW(self._window, _WM_CLOSE, 0, 0):
                self._log("无法通知托盘退出")
        if self._thread is not None and threading.current_thread() is not self._thread:
            self._thread.join(5)
            if self._thread.is_alive():
                raise TimeoutError("托盘线程未及时退出")

    def _notification(self, window=None):
        data = _Notification()
        data.size = ctypes.sizeof(data)
        data.window = window or self._window
        data.identifier = 1
        data.message = _WM_TRAY
        data.icon = self._icon
        data.tip = self._text
        return data

    def _add_icon(self):
        data = self._notification()
        data.flags = 0x01 | 0x02 | 0x04 | 0x80  # MESSAGE, ICON, TIP, SHOWTIP; never INFO.
        self._added = bool(self._api.shell.Shell_NotifyIconW(0, ctypes.byref(data)))
        if self._added:
            data.version = 4
            self._api.shell.Shell_NotifyIconW(4, ctypes.byref(data))
            self._api.user.KillTimer(self._window, 1)
        else:
            # Explorer may still be starting at user logon.
            self._api.user.SetTimer(self._window, 1, 2000, None)

    def _remove_icon(self, window):
        if self._added:
            data = self._notification(window)
            self._api.shell.Shell_NotifyIconW(2, ctypes.byref(data))
            self._added = False

    def _menu(self):
        user = self._api.user
        menu = user.CreatePopupMenu()
        if not menu:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not user.AppendMenuW(menu, 0, _OPEN, "账号设置") or not user.AppendMenuW(menu, 0, _EXIT, "退出程序"):
                raise ctypes.WinError(ctypes.get_last_error())
            position = wintypes.POINT()
            if not user.GetCursorPos(ctypes.byref(position)):
                raise ctypes.WinError(ctypes.get_last_error())
            user.SetForegroundWindow(self._window)
            selected = user.TrackPopupMenu(menu, 0x0100 | 0x0080 | 0x0002,
                                          position.x, position.y, 0, self._window, None)
            user.PostMessageW(self._window, 0, 0, 0)
            if selected == _OPEN:
                self.on_open()
            elif selected == _EXIT:
                self.on_exit()
        finally:
            user.DestroyMenu(menu)

    def _dispatch(self, window, message, wparam, lparam):
        if message == self._taskbar_message and self._taskbar_message:
            self._added = False
            self._add_icon()
            return 0
        if message == _WM_TRAY:
            event = lparam & 0xffff
            if event in (0x0202, 0x0203, 0x0400, 0x0401):  # click, select, double-click, or keyboard
                self.on_open()
            elif event in (0x0205, 0x007b):  # right-click / version-4 context menu
                self._menu()
            return 0
        if message == _WM_UPDATE:
            if self._added:
                data = self._notification()
                data.flags = 0x04 | 0x80
                if not self._api.shell.Shell_NotifyIconW(1, ctypes.byref(data)):
                    self._added = False
            if not self._added:
                self._add_icon()
            return 0
        if message == _WM_TIMER and wparam == 1:
            if not self._added:
                self._add_icon()
            return 0
        if message == _WM_CLOSE:
            self._api.user.DestroyWindow(window)
            return 0
        if message == _WM_DESTROY:
            self._remove_icon(window)
            self._window = None
            self._api.user.PostQuitMessage(0)
            return 0
        return self._api.user.DefWindowProcW(window, message, wparam, lparam)

    def _procedure(self, window, message, wparam, lparam):
        try:
            return self._dispatch(window, message, wparam, lparam)
        except Exception as exc:
            self._log("托盘操作失败：{}".format(exc))
            return 0

    def _run(self):
        registered = False
        class_name = "CampusNetTray_{}_{}".format(os.getpid(), id(self))
        instance = None
        try:
            self._api = _tray_api()
            user = self._api.user
            instance = self._api.kernel.GetModuleHandleW(None)
            if not instance:
                raise ctypes.WinError(ctypes.get_last_error())
            self._taskbar_message = user.RegisterWindowMessageW("TaskbarCreated")
            if not self._taskbar_message:
                raise ctypes.WinError(ctypes.get_last_error())
            self._callback = _WNDPROC(self._procedure)  # Keep alive until the window is destroyed.
            window_class = _WindowClass(procedure=self._callback, instance=instance, name=class_name)
            if not user.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered = True
            # A hidden top-level window receives Explorer's broadcast messages.
            self._window = user.CreateWindowExW(0, class_name, "", 0, 0, 0, 0, 0, None, None, instance, None)
            if not self._window:
                raise ctypes.WinError(ctypes.get_last_error())
            resource = _icon_resource()
            buffer = (ctypes.c_ubyte * len(resource)).from_buffer_copy(resource)
            self._icon = user.CreateIconFromResourceEx(buffer, len(resource), True, 0x00030000, 32, 32, 0)
            if not self._icon:
                raise ctypes.WinError(ctypes.get_last_error())
            self._add_icon()
            self._ready.set()
            message = wintypes.MSG()
            while not self._stopping.is_set():
                result = user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                user.TranslateMessage(ctypes.byref(message))
                user.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self._error = exc
            self._log("托盘初始化或运行失败：{}".format(exc))
        finally:
            if self._window is not None:
                self._remove_icon(self._window)
                self._api.user.DestroyWindow(self._window)
                self._window = None
            if self._icon is not None:
                self._api.user.DestroyIcon(self._icon)
                self._icon = None
            if registered:
                self._api.user.UnregisterClassW(class_name, instance)
            self._ready.set()
