"""锐捷 Ruijie ePortal 认证（``/eportal/InterFace.do``）。

接口：

``POST /eportal/InterFace.do?method=login``

表单字段：``userId`` / ``password`` / ``service`` / ``queryString`` 等。
其中 ``queryString`` 是门户页面 URL 里带的那一串（形如
``wlanuserip=...&wlanacname=...&nasip=...``），第一次请求时从页面里抠出来。

响应是 JSON：``{"result":"success","message":"..."}``。
"""

from __future__ import annotations

import json
from typing import List, Optional

from ..session import local_ip
from .base import DetectContext, LoginResult, Provider


class RuijieProvider(Provider):
    name = "ruijie"
    display_name = "锐捷 Ruijie"
    docs = "https://www.ruijie.com.cn/"
    min_confidence = 0.4

    @classmethod
    def detect(cls, ctx: DetectContext) -> float:
        score = 0.0
        low = ctx.lower_text
        url = (ctx.url or "").lower()

        if "interface.do" in low or "interface.do" in url:
            score += 0.75
        if "querystring" in low:
            score += 0.3
        if "锐捷" in ctx.text or "ruijie" in low:
            score += 0.6
        if "/eportal/" in url:
            score += 0.2
        if "wlanacname" in low or "wlanuserip" in low:
            score += 0.15
        return min(score, 1.0)

    #: ``nasip`` 字段里应当是**会话令牌**（一串 hex），不是门户 URL。
    #:
    #: 本机实测（锐捷 RG-SAM+ ePortal，2026-10）：
    #:   · ``queryString`` 原样取自地址栏（含 ``nasip=<token>``）→ ``result=success``
    #:   · ``nasip`` 清空 / 删掉 / 写成门户 URL            → ``设备未注册,请在ePortal上添加认证设备``
    #: 也就是说 ``nasip`` 是**必填且必须正确**的，拼错必然失败。
    #: 真实的 queryString 长这样（来自浏览器跳转后的地址栏）::
    #:
    #:   wlanuserip=426256f5...&wlanacname=32e4b2fa...&ssid=&nasip=323fbaa6...
    #:   &mac=5ebf7a08...&t=wireless-v2&url=5667f364...&nasid=32e4b2fa...
    #:   &vid=2d7fd075...&port=0acc80f2...&nasportid=d9ed0d20...
    _QUERY_MARKERS = ("wlanuserip", "wlanacname", "nasip")

    def login(self, portal: str, username: str, password: str, client_ip: str = "", mac: str = "") -> LoginResult:
        ip = client_ip or local_ip()
        service = str(self.opt("service", ""))
        errors: List[str] = []
        self._used_fallback = False

        for origin in self.origins(portal):
            query_string = (self.opt("query_string", "")
                            or self._fetch_query_string(origin, ip, portal_url=portal))
            payload = {
                "userId": username,
                "password": password,
                "service": service,
                "queryString": query_string,
                "operatorPwd": "",
                "operatorUserId": "",
                "validcode": "",
                "passwordEncrypt": "false",
            }
            url = "{}/eportal/InterFace.do?method=login".format(origin)
            try:
                resp = self.session.post(url, data=payload, headers={
                    "Referer": "{}/eportal/".format(origin),
                    "Origin": origin,
                })
            except Exception as exc:  # noqa: BLE001
                errors.append("{}：{}".format(origin, exc))
                continue

            result = self._as_result(resp, url)
            if result.ok or result.already_online:
                return result
            errors.append("{}：{}".format(origin, result.message))

        hint = ""
        if self._used_fallback:
            hint = ("\n（提示：没能取到门户要求的完整 queryString，已退回自拼版本。"
                    "锐捷 ePortal 的 nasip 字段必须是会话令牌，自拼是拿不到的 —— "
                    "请断开网络后在浏览器里打开认证页，把地址栏那串完整 URL 填入 "
                    "config.json 的 options.query_string，或把 portal_ip 改成带参数的那个地址。）")
        return LoginResult(
            ok=False,
            provider=self.name,
            message="登录失败。最后一次返回：{}{}".format(
                errors[-1] if errors else "无响应", hint),
            raw="\n".join(errors[-3:]),
        )

    # ------------------------------------------------------------ 内部
    def _fetch_query_string(self, origin: str, ip: str, portal_url: str = "") -> str:
        """拿到门户要求的 ``queryString``。

        优先级（**顺序很重要**）：

        1. ``portal_url`` 里带的查询串 —— 这就是浏览器跳转后的完整地址，
           参数最全（含 ``nasip`` 会话令牌）；
        2. 门户页面里抠出来的 ``queryString`` / 内嵌 URL；
        3. **只有当上面都拿不到时**才退化自拼，并在日志里说明它可能不被接受。

        历史教训：上游旧版会**跳过第 1 步**，直接退化成
        ``wlanuserip=<ip>&wlanacname=&nasip=<门户URL>`` ——
        把门户 URL 塞进了本该放会话令牌的 ``nasip``，于是门户一律回
        ``设备未注册``，看起来像"账号密码错"，其实是请求拼错了。
        """
        # 1) 门户地址自带的查询串（权威来源）
        from_url = self._query_from_url(portal_url)
        if from_url:
            return from_url

        # 2) 页面里抠
        for path in ("{}/eportal/?c=ACSetting".format(origin), "{}/eportal/".format(origin),
                     "{}/eportal/login.jsp".format(origin)):
            try:
                resp = self.session.get(path, headers={"Referer": origin + "/"})
            except Exception:  # noqa: BLE001
                continue
            text = resp.text or ""
            found = self._query_from_text(text)
            if found:
                return found

        # 3) 兜底自拼。注意 nasip 只能留空 —— 填门户 URL 必定被判「设备未注册」。
        #    这一步拿到的 queryString 多半会被门户拒绝，所以置一个标记，
        #    由 login() 在失败信息里把补救办法告诉用户。
        self._used_fallback = True
        return "wlanuserip={ip}&wlanacname=&nasip=&t=wireless-v2".format(ip=ip)

    @classmethod
    def _query_from_url(cls, url: str) -> str:
        """从完整 URL 里取出查询串（只在看起来像门户参数时才认）。"""
        if not url or "?" not in url:
            return ""
        query = url.split("?", 1)[1].split("#", 1)[0]
        if any(marker + "=" in query for marker in cls._QUERY_MARKERS):
            return query
        return ""

    @classmethod
    def _query_from_text(cls, text: str) -> str:
        """从页面 HTML / JS 里找出查询串。"""
        if not text:
            return ""
        found = cls.find(r'queryString["\']?\s*[:=]\s*["\']([^"\']+)', text)
        if found and any(marker + "=" in found for marker in cls._QUERY_MARKERS):
            return found
        # 内嵌的完整 URL（有些门户把跳转地址写在脚本里）
        embedded = cls.find(r'["\']([^"\']*?(?:wlanuserip|wlanacname)=[^"\']+)["\']', text)
        if embedded:
            return cls._query_from_url(embedded) or embedded.lstrip("?")
        return ""

    @staticmethod
    def _decode_message(value: object) -> str:
        """修掉门户返回消息的编码问题。

        锐捷 ePortal 有相当一部分实现把 UTF-8 字节当 latin-1 塞进 JSON 字符串，
        所以 ``"账号2024001已经在线"`` 读出来会变成一串看不懂的乱码。
        这不仅是显示难看的问题 ——
        ``_as_result`` 里靠 ``"已在线" in message`` 判定「已经在线」，
        乱码会让这个判定**永远不成立**，把「本来就在线」误报成「刚认证成功」。
        """
        message = str(value)
        if not message:
            return ""
        try:
            repaired = message.encode("latin-1").decode("utf-8")
        except (UnicodeError, LookupError):
            return message
        # 只有在「修完确实出现了中文、而原文没有」时才采用，避免误伤正常文本
        def has_cjk(text: str) -> bool:
            return any("\u4e00" <= ch <= "\u9fff" for ch in text)

        if has_cjk(repaired) and not has_cjk(message):
            return repaired
        return message

    def _as_result(self, resp, endpoint: str) -> LoginResult:
        text = resp.text or ""
        data: Optional[dict] = None
        payload = resp.jsonp_payload or text.strip()
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            data = None

        if data:
            result = str(data.get("result", "")).lower()
            message = self._decode_message(data.get("message") or data.get("msg") or "")
            if result in ("success", "ok", "1", "true"):
                # 「已经在线」也返回 result=success，但语义不同 —— 必须分开，
                # 否则日志会把「无需认证」写成「刚刚认证成功」，误导排查。
                if "已经在线" in message or "已在线" in message or "already" in message.lower():
                    return LoginResult(True, self.name, message or "已在线", endpoint=endpoint,
                                       raw=text[:600], already_online=True)
                return LoginResult(True, self.name, message or "认证成功", endpoint=endpoint, raw=text[:600])
            return LoginResult(False, self.name, message or "result={}".format(result),
                               endpoint=endpoint, raw=text[:600])

        if "success" in text.lower():
            return LoginResult(True, self.name, "认证成功", endpoint=endpoint, raw=text[:600])
        return LoginResult(False, self.name, "HTTP {} 未识别响应".format(resp.status),
                           endpoint=endpoint, raw=text[:600])
