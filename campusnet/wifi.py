"""Read the connected Wi-Fi SSID without changing any network settings."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
from dataclasses import dataclass
import os
import uuid


@dataclass
class WifiResult:
    ok: bool = False
    changed: bool = False
    ssid: str = ""
    message: str = ""
    target: str = ""
    supported: bool = True

    def describe(self) -> str:
        return self.message or ("已连接 {}".format(self.ssid) if self.ssid else "未知")


class _Guid(ctypes.Structure):
    _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD),
                ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8)]


def current_ssid() -> str:
    """Return an unambiguous connected SSID, or empty when unavailable.

    GetConnectedSsid reads the actual connected SSID without requesting BSSID
    or location access. Network profile display names are never used as SSIDs.
    """
    if os.name != "nt":
        return ""
    try:
        ssids = set(_winrt_ssids())
        return ssids.pop() if len(ssids) == 1 else ""
    except (OSError, ValueError, AttributeError):
        return ""


def _check_hresult(value):
    if value < 0:
        raise OSError("WinRT HRESULT 0x{:08x}".format(value & 0xffffffff))


def _com_call(interface, slot, types, *args):
    table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    # c_int32 keeps HRESULT handling explicit, including RPC_E_CHANGED_MODE.
    method = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, *types)(table[slot])
    return method(interface, *args)


@contextmanager
def _owned_interface():
    interface = ctypes.c_void_p()
    try:
        yield interface
    finally:
        if interface.value:
            _com_call(interface, 2, [])  # IUnknown::Release


def _winrt_ssids():
    """Read WinRT WLAN connection profiles through the documented COM ABI.

    See Windows SDK windows.networking.connectivity.h. IInspectable methods
    occupy slots 0-5; the comments below identify each invoked interface.
    """
    runtime = ctypes.WinDLL("combase.dll")
    pointer = ctypes.c_void_p
    out_pointer = ctypes.POINTER(pointer)
    runtime.RoInitialize.argtypes = [wintypes.DWORD]
    runtime.RoInitialize.restype = ctypes.c_int32
    runtime.RoUninitialize.argtypes = []
    runtime.RoUninitialize.restype = None
    runtime.WindowsCreateString.argtypes = [wintypes.LPCWSTR, wintypes.UINT, out_pointer]
    runtime.WindowsCreateString.restype = ctypes.c_int32
    runtime.WindowsDeleteString.argtypes = [pointer]
    runtime.WindowsDeleteString.restype = ctypes.c_int32
    runtime.WindowsGetStringRawBuffer.argtypes = [pointer, ctypes.POINTER(wintypes.UINT)]
    runtime.WindowsGetStringRawBuffer.restype = pointer
    runtime.RoGetActivationFactory.argtypes = [pointer, ctypes.POINTER(_Guid), out_pointer]
    runtime.RoGetActivationFactory.restype = ctypes.c_int32
    initialized = runtime.RoInitialize(1)
    if initialized < 0 and initialized != -2147417850:  # Existing STA: RPC_E_CHANGED_MODE.
        _check_hresult(initialized)
    class_name = pointer()
    try:
        name = "Windows.Networking.Connectivity.NetworkInformation"
        _check_hresult(runtime.WindowsCreateString(name, len(name), ctypes.byref(class_name)))
        statics_id = _Guid.from_buffer_copy(uuid.UUID("5074f851-950d-4165-9c15-365619481eea").bytes_le)
        profile_id = _Guid.from_buffer_copy(uuid.UUID("e2045145-4c9f-400c-9150-7ec7d6e2888a").bytes_le)
        found = []
        with _owned_interface() as factory, _owned_interface() as profiles:
            _check_hresult(runtime.RoGetActivationFactory(class_name, ctypes.byref(statics_id), ctypes.byref(factory)))
            # INetworkInformationStatics::GetConnectionProfiles, IVectorView::get_Size.
            _check_hresult(_com_call(factory, 6, [out_pointer], ctypes.byref(profiles)))
            size = wintypes.UINT()
            _check_hresult(_com_call(profiles, 7, [ctypes.POINTER(wintypes.UINT)], ctypes.byref(size)))
            if size.value > 64:
                return []
            for index in range(size.value):
                with _owned_interface() as profile, _owned_interface() as wireless, _owned_interface() as details:
                    _check_hresult(_com_call(profiles, 6, [wintypes.UINT, out_pointer], index, ctypes.byref(profile)))
                    # IConnectionProfile2 identifies WLAN profiles; Ethernet/VPN are excluded.
                    _check_hresult(_com_call(profile, 0, [ctypes.POINTER(_Guid), out_pointer],
                                             ctypes.byref(profile_id), ctypes.byref(wireless)))
                    is_wlan = ctypes.c_ubyte()
                    _check_hresult(_com_call(wireless, 7, [ctypes.POINTER(ctypes.c_ubyte)], ctypes.byref(is_wlan)))
                    if not is_wlan.value:
                        continue
                    connectivity = ctypes.c_int()
                    _check_hresult(_com_call(profile, 7, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(connectivity)))
                    if connectivity.value == 0:  # NetworkConnectivityLevel.None: disconnected.
                        continue
                    _check_hresult(_com_call(wireless, 9, [out_pointer], ctypes.byref(details)))
                    if not details.value:
                        continue
                    ssid = pointer()
                    try:
                        # IWlanConnectionProfileDetails::GetConnectedSsid.
                        _check_hresult(_com_call(details, 6, [out_pointer], ctypes.byref(ssid)))
                        length = wintypes.UINT()
                        raw = runtime.WindowsGetStringRawBuffer(ssid, ctypes.byref(length))
                        value = ctypes.wstring_at(raw, length.value) if raw and length.value else ""
                        if value:
                            found.append(value)
                    finally:
                        runtime.WindowsDeleteString(ssid)
        return found
    finally:
        runtime.WindowsDeleteString(class_name)
        if initialized >= 0:
            runtime.RoUninitialize()
