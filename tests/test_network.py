import ctypes
from io import BytesIO
import os
import unittest
from unittest.mock import Mock, patch
from urllib.response import addinfourl

from campusnet.config import Config
from campusnet.detector import CONTENT_PROBES, HIJACK_PROBES, check_online
from campusnet.providers.ruijie import RuijieProvider
from campusnet.runner import Runner
from campusnet.session import HttpError, Response
from campusnet import wifi


class WifiTests(unittest.TestCase):
    def test_ambiguous_or_unavailable_wifi_is_not_a_match(self):
        for ssids, expected in [([], ""), (["tjus_wifi"], "tjus_wifi"),
                                (["tjus_wifi", "hotspot"], ""),
                                (["TJUS_WIFI"], "TJUS_WIFI"),
                                (["tjus_wifi "], "tjus_wifi ")]:
            with self.subTest(ssids=ssids), patch.object(wifi.os, "name", "nt"), \
                 patch.object(wifi, "_winrt_ssids", return_value=ssids):
                self.assertEqual(wifi.current_ssid(), expected)

    def test_denied_query_fails_closed(self):
        with patch.object(wifi.os, "name", "nt"), \
             patch.object(wifi, "_winrt_ssids", side_effect=OSError("access denied")):
            self.assertEqual(wifi.current_ssid(), "")

    @unittest.skipUnless(os.name == "nt", "Windows COM ABI")
    def test_winrt_excludes_vpn_and_disconnected_profiles_and_releases_objects(self):
        dll = Mock()
        name = ctypes.create_unicode_buffer("tjus_wifi")
        released = []

        def create_string(value, size, output):
            output._obj.value = 11
            return 0

        def factory(string, guid, output):
            output._obj.value = 10
            return 0

        def string_buffer(string, length):
            length._obj.value = 9
            return ctypes.addressof(name)

        def invoke(interface, slot, types, *args):
            address = interface.value
            if slot == 2:
                released.append(address)
                return 0
            output = args[-1]._obj
            if address == 10:
                output.value = 20  # vector of VPN, disconnected WLAN, connected WLAN
            elif address == 20:
                output.value = 3 if slot == 7 else 30 + args[0]
            elif 30 <= address <= 32:
                output.value = address + 10 if slot == 0 else int(address != 31)
            elif 40 <= address <= 42:
                output.value = int(address != 40) if slot == 7 else address + 10
            elif address == 52:
                output.value = 12  # GetConnectedSsid HSTRING
            else:
                self.fail("Unexpected COM interface")
            return 0

        dll.WindowsCreateString.side_effect = create_string
        dll.RoGetActivationFactory.side_effect = factory
        dll.WindowsGetStringRawBuffer.side_effect = string_buffer
        # Existing STA is usable, but must not be uninitialized by this code.
        for initialize_result in (0, -2147417850):
            with self.subTest(initialize_result=initialize_result), \
                 patch.object(ctypes, "WinDLL", return_value=dll), \
                 patch.object(wifi, "_com_call", side_effect=invoke):
                dll.RoInitialize.return_value = initialize_result
                released.clear()
                self.assertEqual(wifi._winrt_ssids(), ["tjus_wifi"])
                self.assertEqual(set(released), {10, 20, 30, 31, 32, 40, 41, 42, 52})
                self.assertEqual(len(released), 9)
                self.assertEqual(dll.WindowsDeleteString.call_count, 2)
                self.assertEqual(dll.RoUninitialize.call_count, int(initialize_result == 0))
                dll.reset_mock()


class ConnectivityTests(unittest.TestCase):
    def test_wifi_switch_inside_provider_cancels_password_submission(self):
        runner = Runner(Config(wifi_ssid="tjus_wifi"))
        connected = True

        def fetch_page(request, **kwargs):
            nonlocal connected
            self.assertEqual(request.method, "GET")
            connected = False  # Wi-Fi changed while the query page was loading.
            return addinfourl(BytesIO(b'queryString="wlanuserip=test&nasip=fresh"'),
                              {}, request.full_url, 200)

        with patch("campusnet.runner.current_ssid",
                   side_effect=lambda: "tjus_wifi" if connected else "hotspot"), \
             patch.object(runner.session._opener, "open", side_effect=fetch_page) as send:
            result = RuijieProvider(runner.session).login("http://192.0.2.1", "test", "test",
                                                         client_ip="192.0.2.2")
        self.assertFalse(result.ok)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[0].method, "GET")

    def test_valid_https_redirect_is_online(self):
        session = Mock()
        session.get.side_effect = [
            Response(204, {}, b"", HIJACK_PROBES[0][0]),
            Response(200, {}, b"User-agent: *", "https://qq.com/robots.txt"),
        ]
        result = check_online(session)
        self.assertTrue(result.online)
        self.assertFalse(session.get.call_args_list[0].kwargs["allow_redirects"])
        self.assertTrue(session.get.call_args_list[1].kwargs["allow_redirects"])

    def test_probe_whitelist_does_not_count_as_internet(self):
        session = Mock()
        session.get.side_effect = [Response(204, {}, b"", HIJACK_PROBES[0][0])] + [
            HttpError("blocked") for _ in CONTENT_PROBES
        ]
        self.assertFalse(check_online(session).online)

    def test_untrusted_or_downgraded_redirect_cannot_count_as_online(self):
        for final_url in ("https://portal.example/robots.txt", "http://www.qq.com/robots.txt",
                          "https://www.qq.com.attacker.example/robots.txt"):
            session = Mock()
            session.get.side_effect = [Response(204, {}, b"", HIJACK_PROBES[0][0])] + [
                Response(200, {}, b"User-agent: *", final_url) for _ in CONTENT_PROBES
            ]
            self.assertFalse(check_online(session).online)

    def test_portal_redirect_is_returned_without_following(self):
        session = Mock()
        session.get.side_effect = [
            Response(302, {"Location": "http://192.0.2.1/login"}, b"", url)
            for url, _, _ in HIJACK_PROBES
        ]
        result = check_online(session)
        self.assertFalse(result.online)
        self.assertEqual(result.portal_url, "http://192.0.2.1/login")
        self.assertEqual(session.get.call_count, len(HIJACK_PROBES))


if __name__ == "__main__":
    unittest.main()
