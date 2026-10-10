import unittest

from campusnet.desktop import _display_text, _soft_wrap_text


class DesktopLayoutTests(unittest.TestCase):
    def test_long_portal_message_gets_wrap_opportunities(self):
        message = "登录失败。最后一次返回：http://192.168.24.65：密码不匹配!"
        wrapped = _soft_wrap_text(message)
        self.assertIn("http:\u200b/\u200b/", wrapped)
        self.assertIn("65：\u200b密码", wrapped)
        self.assertEqual(wrapped.replace("\u200b", ""), message)

    def test_empty_text_remains_empty(self):
        self.assertEqual(_soft_wrap_text(""), "")
        self.assertEqual(_soft_wrap_text(None), "")

    def test_display_text_is_bounded_without_losing_wrap_points(self):
        message = "http://192.168.24.65/" * 30
        displayed = _display_text(message)
        self.assertLessEqual(len(displayed.replace("\u200b", "")), 180)
        self.assertTrue(displayed.endswith("…"))
        self.assertIn(":\u200b/", displayed)


if __name__ == "__main__":
    unittest.main()
