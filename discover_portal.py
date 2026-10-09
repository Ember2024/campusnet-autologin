#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""认证门户探测 —— 找出「到底该往哪个地址认证」。

用于 ``config.json`` 里 ``portal_ip`` 写错、或者换校区/换认证系统之后。

它做三件事：

1. 用 ``generate_204`` 判断**当前是否已认证**（并且抓出被劫持时跳到的门户地址）；
2. 扫描一批常见门户候选地址，按锐捷/深澜/Dr.COM 的特征打分；
3. 对每个候选**真的发一次登录请求**，看它认不认你这台设备。

第 3 步是关键：锐捷 ePortal 在「设备没接入」时会直接回
``设备未注册,请在ePortal上添加认证设备`` / ``Web认证接入设备不存在``，
这种地址**换任何账号密码都不会成功**，必须先排除掉。

用法：
    python discover_portal.py
    python discover_portal.py --gateway 192.0.2.1
"""

from __future__ import annotations

import argparse
import io
import ipaddress
import json
import os
import re
import socket
import sys
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")

#: 锐捷 ePortal 的「这台设备根本没接入」类报错。
#: 命中这些词的地址可以直接判死：不是账号密码的问题。
DEVICE_REJECT_HINTS = (
    "设备未注册",
    "Web认证接入设备不存在",
    "未注册",
    "请联系管理员",
    "添加认证设备",
)

#: 各认证系统的指纹。
FINGERPRINTS = {
    "ruijie": ("ruijie", "eportal", "interface.do", "rg-sam", "wlanuserip", "wlanacname"),
    "srun": ("srun_portal", "srun", "深澜", "ac_id", "jsVersion"),
    "drcom": ("drcom", "dr1003", "upass", "0mkkey", "wlanacname"),
    "h3c": ("h3c", "portal/login", "wlanuserip", "acname"),
    "huawei": ("huawei", "portal", "wlanparameter"),
}

PORTAL_PATHS = ("/", "/eportal/", "/eportal/login.jsp", "/eportal/index.jsp", "/portal/", "/srun_portal_pc")


def _utf8_console() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace"))
            except Exception:
                pass


def decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


#: 本机代理（Clash/Meta 之类）常用的 fake-ip 段。这些地址不是校园网设备，
#: 扫进来只会浪费时间和误导结论，一律排除。
PROXY_PREFIXES = ("198.18.", "198.19.")

#: ipconfig / route 输出里表示「这一行是网关」的关键词。
_GATEWAY_KEYS = ("默认网关", "Default Gateway")


def _is_proxy_ip(ip: str) -> bool:
    return any(ip.startswith(p) for p in PROXY_PREFIXES)


def default_gateways() -> list:
    """取所有默认网关，**按 metric 排序，并优先非代理网段**。

    实测机器上常见两条默认路由：

    * ``198.18.0.2``（metric 0）—— 代理软件（Clash/Meta 等）的虚拟网卡；
    * ``10.x.x.x``（metric 较高）—— 校园网真实网关。

    metric 最小的恰恰是代理，所以**不能只取第一条**，否则会去扫
    198.18.0.0/24 这种根本不存在的网段（这是本脚本第一版踩过的坑）。
    """
    import subprocess

    found = []          # [(是否代理, metric, 网关)]

    # route print 的表格式输出最可靠：Gateway 与 Interface 列齐全
    try:
        text = decode(subprocess.run(["route", "print", "-4"],
                                     capture_output=True, timeout=8).stdout)
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                gateway, metric = parts[2], parts[4]
                if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", gateway or ""):
                    continue
                try:
                    metric_value = int(metric)
                except ValueError:
                    metric_value = 9999
                found.append((_is_proxy_ip(gateway), metric_value, gateway))
    except Exception:
        pass

    # 兜底：ipconfig 的「默认网关」行
    if not found:
        try:
            text = decode(subprocess.run(["ipconfig"], capture_output=True, timeout=8).stdout)
            for line in text.splitlines():
                if any(k in line for k in _GATEWAY_KEYS):
                    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
                    if m and m.group(1) != "0.0.0.0":
                        found.append((_is_proxy_ip(m.group(1)), 9999, m.group(1)))
        except Exception:
            pass

    found.sort(key=lambda item: (item[0], item[1]))
    out = []
    for _, _, gw in found:
        if gw not in out:
            out.append(gw)
    return out


def default_gateway() -> str:
    gws = default_gateways()
    return gws[0] if gws else ""


def local_ipv4() -> list:
    """本机所有 IPv4（排除回环与 fake-ip 代理段）。"""
    ips = []

    # 1) 用 UDP connect 拿主出口 IP（不会真的发包）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("223.5.5.5", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    # 2) ipconfig 里的其它地址（多网卡/代理虚拟网卡）
    try:
        import subprocess

        text = decode(subprocess.run(["ipconfig"], capture_output=True, timeout=6).stdout)
        for m in re.finditer(r"IPv4.*?:\s*(\d+\.\d+\.\d+\.\d+)", text):
            ips.append(m.group(1))
    except Exception:
        pass

    out = []
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_loopback or ip.startswith("198.18.") or ip.startswith("169.254."):
            continue
        if ip not in out:
            out.append(ip)
    return out


def probe_online(session) -> tuple:
    """返回 (是否已认证, 被劫持时跳转到的地址)。"""
    portal = ""
    for url in ("http://connect.rom.miui.com/generate_204",
                "http://edge.microsoft.com/captiveportal/generate_204"):
        try:
            r = session.get(url, timeout=6, allow_redirects=False)
        except Exception:
            continue
        if r.status_code == 204:
            return True, ""
        loc = r.headers.get("Location") or ""
        if loc.startswith("http"):
            portal = loc
        elif r.status_code == 200 and r.text:
            low = r.text.lower()
            if any(k in low for k in ("eportal", "interface.do", "srun", "portal", "认证", "登录")):
                portal = url
    return False, portal


#: 一个 /24 里最可能放门户的末位数字，按可能性排序。
LAST_OCTET_HINTS = (1, 254, 65, 250, 100, 2, 251, 252, 253)


def _subnet24(ip: str) -> str:
    return ".".join(ip.split(".")[:3])


def candidates(gateway: str, extra: list) -> list:
    """候选门户地址。

    优先级：手动指定 > 劫持目标 > 本机网段常见地址 > 网关 > 网关网段常见地址。
    代理 fake-ip 段（198.18/198.19）一律排除。
    """
    out = []

    def add(item):
        item = (item or "").strip().rstrip("/")
        if not item:
            return
        host = item.split("//", 1)[-1].split("/")[0].split(":")[0]
        if _is_proxy_ip(host):
            return
        if item not in out:
            out.append(item)

    for item in extra:
        add(item)

    if gateway and not _is_proxy_ip(gateway):
        add("http://{}".format(gateway))

    # 本机真实 IP 所在 /24（比网关网段更可能是门户所在）
    nets = []
    for ip in local_ipv4():
        if _is_proxy_ip(ip):
            continue
        prefix = _subnet24(ip)
        nets.append(prefix)
        for last in LAST_OCTET_HINTS:
            add("http://{}.{}".format(prefix, last))

    if gateway and not _is_proxy_ip(gateway):
        prefix = _subnet24(gateway)
        if prefix not in nets:
            nets.append(prefix)
            for last in LAST_OCTET_HINTS:
                add("http://{}.{}".format(prefix, last))

    return out


def score_page(text: str, headers: dict) -> tuple:
    low = (text or "").lower()
    server = str(headers.get("Server", "")).lower()
    best, best_score = "", 0.0
    for name, keys in FINGERPRINTS.items():
        hits = sum(1 for k in keys if k.lower() in low or k.lower() in server)
        if hits:
            sc = min(1.0, hits / 3.0)
            if sc > best_score:
                best, best_score = name, sc
    return best, best_score


def try_portal(session, origin: str, username: str, password: str) -> dict:
    """对一个候选地址做「能不能用」的判定。"""
    info = {"origin": origin, "reachable": False, "system": "", "score": 0.0,
            "device_ok": None, "note": "", "title": "", "server": ""}

    page_text = ""
    for path in PORTAL_PATHS:
        url = origin + path
        try:
            r = session.get(url, timeout=6, allow_redirects=True)
        except Exception as exc:
            info["note"] = "{}: {}".format(type(exc).__name__, exc)
            continue
        info["reachable"] = True
        info["server"] = r.headers.get("Server", "")
        body = decode(r.content)
        page_text += body
        m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        if m and not info["title"]:
            info["title"] = m.group(1).strip()[:80]
        break

    if not info["reachable"]:
        return info

    info["system"], info["score"] = score_page(page_text, {"Server": info["server"]})

    # 已经是「设备未注册」→ 这个地址对本机不可用，直接判死
    if any(h in page_text for h in DEVICE_REJECT_HINTS):
        info["device_ok"] = False
        info["note"] = "门户拒绝：设备未注册 / Web认证接入设备不存在（换账号密码也没用）"
        return info

    # 真发一次登录，看它怎么回
    if not username or not password:
        info["note"] = "（未提供账号密码，跳过登录探测）"
        return info

    for endpoint, payload in (
        ("/eportal/InterFace.do?method=login",
         {"userId": username, "password": password, "service": "", "queryString": "",
          "operatorPwd": "", "operatorUserId": "", "validcode": "", "passwordEncrypt": "false"}),
        ("/eportal/user.do?method=login_ajax",
         {"username": username, "usernameHidden": username, "pwd": password,
          "strTypeAu": "", "uuidQrCode": "", "authorMode": "", "net_access_type": "",
          "isNoOperatorPwd": "", "isNoDomainName": "", "authorCode": "", "is_auto_land": "false"}),
    ):
        try:
            r = session.post(origin + endpoint, data=payload, timeout=10,
                             headers={"Referer": origin + "/eportal/", "Origin": origin})
        except Exception:
            continue
        body = decode(r.content)
        if any(h in body for h in DEVICE_REJECT_HINTS):
            info["device_ok"] = False
            m = re.search(r"(设备未注册[^\"'<；;]*|Web认证接入设备不存在[^\"'<；;]*)", body)
            info["note"] = "登录接口拒绝：{}".format(m.group(1) if m else "设备未注册/设备不存在")
            return info
        if '"result":"success"' in body.replace(" ", "") or "result: 'success'" in body:
            info["device_ok"] = True
            info["note"] = "登录接口返回 success（本机设备已被接纳）"
            return info
        # 有别的业务性反馈（密码错、验证码…）说明设备是通的
        for hint in ("密码", "账号", "验证码", "已在线", "欠费", "userIndex", "result"):
            if hint in body:
                info["device_ok"] = True
                info["note"] = "登录接口有业务响应（设备已接纳）：{}".format(body.strip()[:120])
                return info
        info["note"] = "登录接口响应：{}".format(body.strip()[:120])

    if info["device_ok"] is None:
        info["device_ok"] = False
    return info


def main() -> int:
    _utf8_console()

    parser = argparse.ArgumentParser(description="探测校园网认证门户")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--gateway", default="", help="手动指定网关地址")
    parser.add_argument("--portal", action="append", default=[],
                        help="手动追加候选门户地址（可重复）")
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--timeout", type=int, default=6)
    args = parser.parse_args()

    try:
        import requests
    except ImportError:
        print("[x] 需要 requests：pip install requests")
        return 2

    username, password = args.username, args.password
    if os.path.exists(args.config):
        try:
            with open(args.config, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            username = username or cfg.get("username", "")
            password = password or cfg.get("password", "")
            saved = cfg.get("portal_ip", "")
            if saved:
                args.portal.append(saved)
        except Exception as exc:  # noqa: BLE001
            print("[!] 读取配置失败（忽略）：{}".format(exc))

    session = requests.Session()
    session.trust_env = False          # 必须绕开本机代理，否则探测全被污染
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    print("=" * 74)
    print("校园网认证门户探测")
    print("=" * 74)
    print("本机 IPv4 : {}".format(", ".join(local_ipv4()) or "(未取到)"))
    gateways = [args.gateway] if args.gateway else default_gateways()
    if gateways:
        print("默认网关  : {}（按非代理优先排序，代理网段已排除）".format(", ".join(gateways)))
    else:
        print("默认网关  : (未取到)")
    gateway = gateways[0] if gateways else ""

    online, hijack = probe_online(session)
    print("当前状态  : {}".format("已认证（可正常出网）" if online else "未认证 / 被门户劫持"))
    if hijack:
        print("劫持目标  : {}   ← 这才是真正的认证地址候选".format(hijack))

    cands = candidates(gateway, args.portal + ([hijack] if hijack else []))
    print("候选地址  : {} 个".format(len(cands)))
    for c in cands:
        print("    - {}".format(c))

    if not cands:
        print("\n[x] 没有候选地址。请用 --gateway 或 --portal 手动指定。")
        return 1

    print("\n开始逐个探测（每个地址最多 6 秒）…\n")
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(try_portal, session, c, username, password): c for c in cands}
        for fut in futures:
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                print("  {} ERR {}".format(futures[fut], exc))

    results.sort(key=lambda r: (r["device_ok"] is not True, -r["score"]))

    print("=" * 74)
    print("{:<26} {:<9} {:<8} {}".format("地址", "认证系统", "设备可用", "说明"))
    print("-" * 74)
    for r in results:
        if not r["reachable"]:
            continue
        ok = {True: "是", False: "否", None: "?"}[r["device_ok"]]
        print("{:<26} {:<9} {:<8} {}".format(
            r["origin"][:25], (r["system"] or "-")[:8], ok, r["note"][:60]))

    usable = [r for r in results if r["device_ok"] is True]
    print("=" * 74)
    if usable:
        best = usable[0]
        print("[+] 可用门户：{}（{}）".format(best["origin"], best["system"] or "未知系统"))
        print("    写入配置：把 config.json 的 portal_ip 改成 \"{}\"".format(best["origin"]))
        return 0

    print("[x] 没有找到「设备已接纳」的门户地址。")
    print("    这说明当前这台机器在认证系统里没有登记为接入设备 ——")
    print("    换账号密码不会有任何区别，需要联系校园网管理员，")
    print("    或者你正在用的其实不是 192.168.24.65 这套认证。")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
