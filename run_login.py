#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校园网自动认证 —— 一次认证尝试（campusnet 引擎驱动）。

设计目标：**幂等、可被反复触发**。
无论被任务计划、网络事件还是手动调用，它都会：
  1. 先判断是否已联网 —— 已联网直接退出（exit 0），绝不重复认证、不打扰现有连接；
  2. 未联网才把 Wi-Fi 抢回校园网并完成 ePortal 认证；
  3. 全过程写日志，日志超过 1 MB 自动滚动。

退出码：
  0  已联网（含本次认证成功）
  1  认证失败
  2  参数/配置错误
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from datetime import datetime


def app_dir() -> str:
    """程序所在目录 —— **脚本运行和打包成 exe 都必须是同一个答案**。

    这里踩过一个必然会踩的坑：不能直接用 ``os.path.dirname(__file__)``。
    PyInstaller 打包后，``__file__`` 指向的是临时解包目录
    （``%TEMP%\\_MEIxxxxxx``），而不是用户放 exe 的地方。用它去拼
    ``config.json`` 的路径，结果就是「配置文件明明在旁边，程序却找不到」，
    而且每次运行路径还都不一样。

    正确做法（按优先级）：

    1. ``sys.frozen`` 存在 = 正在跑打包后的 exe → 用 ``sys.executable`` 所在目录；
    2. 否则就是普通脚本 → 用 ``__file__``。

    onedir 模式下 ``sys.executable`` 指向 ``<目录>/run_login.exe``，
    ``_MEIPASS`` 才是依赖目录，所以要的是前者。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


#: 出现这些名字说明已经到「项目根」了，向上找配置时到此为止。
_ROOT_MARKERS = ("config.json", ".git", "README.md", "campusnet")

#: 向上查找配置时的最大层数（run_login\ → 项目根，一层就够，留点余量）。
_MAX_UP = 4


def find_config_path(explicit: str = "") -> str:
    """定位 ``config.json``（脚本模式与 exe 模式都能找到）。

    打包后 exe 位于 ``<项目根>\\run_login\\run_login.exe``，而
    ``config.json`` 在**上一级的项目根**。所以策略是：

    1. 显式传入的路径（``--config``）优先，找不到就直接用（让报错更明确）；
    2. 从 ``app_dir()`` 开始向上逐级找 ``config.json``；
    3. 找到第一个就返回 —— 这样 exe 在自己目录里放一份、或放在项目根
       都能工作，不会因为目录结构不同就跑不起来。
    """
    if explicit:
        return os.path.abspath(explicit)

    start = app_dir()
    current = start
    for _ in range(_MAX_UP + 1):
        candidate = os.path.join(current, "config.json")
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    # 一个都没找到：返回起始目录下的路径，报错信息才符合直觉
    return os.path.join(start, "config.json")


def find_log_dir() -> str:
    """日志目录：跟随配置文件所在目录，保证「日志就在配置旁边」。"""
    return os.path.join(os.path.dirname(find_config_path()), "logs")


BASE_DIR = app_dir()
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

DEFAULT_CONFIG = find_config_path()
DEFAULT_LOG = os.path.join(os.path.dirname(DEFAULT_CONFIG), "logs", "campusnet.log")
LOG_MAX_BYTES = 1024 * 1024


def _ensure_utf8_console() -> None:
    """让控制台在 GBK 代码页下也能打印中文，而不是抛 UnicodeEncodeError。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                setattr(sys, stream_name,
                        io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace"))
            except Exception:
                pass


def _rotate(path: str) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            backup = path + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(path, backup)
    except OSError:
        pass


def make_logger(log_path: str, quiet_console: bool = False):
    """造一个「同时写控制台与文件」的日志函数。

    ``quiet_console=True`` 只影响控制台输出（用于被任务计划调起的场景，
    避免弹黑窗时闪一堆文字），**文件日志始终完整**。
    """
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    _rotate(log_path)

    def log(message: str, level: str = "info") -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = "[{}] [{}] {}".format(stamp, level.upper(), message)
        if not quiet_console:
            try:
                print(line, flush=True)
            except Exception:
                pass
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    return log


def _make_session_logger(log):
    """只在**真的要认证**时才挂上的详细日志。

    这样 HTTP 明细只会出现在「确实发生了认证动作」的时候 ——
    平时每 5 分钟一次的心跳不会把日志淹掉。
    """
    def session_log(message: str, level: str = "debug") -> None:
        log(message, "debug")
    return session_log


#: 判定「这个地址带没带门户会话令牌」用的字段名。
#: 锐捷 ePortal 的 ``queryString`` 里 ``nasip`` 必须是令牌，
#: 缺了它门户一律回「设备未注册,请在ePortal上添加认证设备」。
_QUERY_KEYS = ("wlanuserip", "wlanacname", "nasip", "mac", "nasid")

#: 会跳转到认证页的探测地址。**必须是普通 http 目标**：
#: 校园网未认证时会被网关劫持并 302 到门户，那串 Location 就是我们要的。
_PORTAL_PROBES = (
    "http://123.123.123.123/",
    "http://connect.rom.miui.com/generate_204",
    "http://www.msftconnecttest.com/connecttest.txt",
)

#: 门户自身上这些路径会 302 到带参数的认证页。
_PORTAL_PATHS = ("/", "/eportal/", "/eportal/index.jsp")


def _looks_like_portal_query(url: str) -> bool:
    """URL 里是否带门户参数（尤其是 nasip 令牌）。"""
    if not url or "?" not in url:
        return False
    query = url.split("?", 1)[1]
    return any(key + "=" in query for key in _QUERY_KEYS)


def discover_portal_url(cfg, log, allow_web_probe: bool = True) -> str:
    """拿到**带完整参数**的门户地址（形如 ``…/eportal/index.jsp?wlanuserip=…&nasip=<令牌>``）。

    为什么要单独做这件事：

    * 锐捷 ePortal 的登录接口要求 ``queryString`` 里有 ``nasip`` 会话令牌；
    * 只有「浏览器/网关跳转过来的那个带参数地址」才有这个令牌；
    * 而 config 里存的通常只是裸 IP（``http://192.168.24.65``），没有参数；
    * 更糟的是：**已联网时**联网探测会直接判定成功，根本不会去访问门户，
      于是永远拿不到带参数的地址。

    所以这里主动去问一次：先问门户自己的路径，再（可选）用公网地址试探
    网关劫持。拿到就用，拿不到就返回空串，让上层退回原逻辑。
    """
    from campusnet.session import Session as _Session

    candidate = (cfg.portal_ip or "").strip()
    if _looks_like_portal_query(candidate):
        return candidate

    session = _Session(timeout=cfg.timeout, use_proxy=cfg.use_proxy)
    origin = candidate.rstrip("/")
    if origin and "//" not in origin:
        origin = "http://" + origin

    # 1) 门户自身的路径：直接问它要跳转地址
    if origin:
        for path in _PORTAL_PATHS:
            try:
                resp = session.get(origin + path, allow_redirects=False)
            except Exception:  # noqa: BLE001
                continue
            location = getattr(resp, "location", "") or ""
            if _looks_like_portal_query(location):
                log("取到带会话令牌的门户地址：{}".format(location[:120]), "debug")
                return location
            # 有些门户不跳转，直接把带参数的 URL 写在页面/脚本里
            body = getattr(resp, "text", "") or ""
            for key in _QUERY_KEYS:
                marker = key + "="
                if marker in body:
                    import re as _re
                    found = _re.search(r'["\']([^"\']*?(?:wlanuserip|nasip)=[^"\']+)["\']', body)
                    if found:
                        value = found.group(1)
                        if _looks_like_portal_query(value):
                            log("从门户页面里取到带会话令牌的地址", "debug")
                            return value
                    break

    if not allow_web_probe:
        return ""

    # 2) 用公网地址试探网关劫持：未认证时会被 302 到门户
    for probe in _PORTAL_PROBES:
        try:
            resp = session.get(probe, allow_redirects=False)
        except Exception:  # noqa: BLE001
            continue
        location = getattr(resp, "location", "") or ""
        if _looks_like_portal_query(location):
            log("网关劫持目标即为门户：{}".format(location[:120]), "debug")
            return location

    return ""


def main() -> int:
    _ensure_utf8_console()

    parser = argparse.ArgumentParser(description="校园网自动认证（一次尝试）")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    parser.add_argument("--log", default=DEFAULT_LOG, help="日志文件路径")
    parser.add_argument("--force", action="store_true",
                        help="即使已联网也强制重新认证一次")
    parser.add_argument("--no-wifi", action="store_true",
                        help="不检查/不切换 Wi-Fi，只做 Portal 认证")
    parser.add_argument("--quiet", action="store_true",
                        help="成功时不往控制台打印（文件日志照常完整记录）")
    parser.add_argument("--no-web-probe", dest="web_probe", action="store_false",
                        default=True,
                        help="不访问公网地址去试探网关劫持（更保守，但未认证时更难拿到门户地址）")
    args = parser.parse_args()

    log = make_logger(args.log, quiet_console=args.quiet)

    if not os.path.exists(args.config):
        log("配置文件不存在：{}".format(args.config), "error")
        return 2

    try:
        from campusnet.config import Config
        from campusnet.runner import Runner
    except ImportError as exc:
        log("无法导入 campusnet 引擎：{}".format(exc), "error")
        return 2

    try:
        cfg = Config.load(args.config)
    except Exception as exc:  # noqa: BLE001
        log("读取配置失败：{}".format(exc), "error")
        return 2

    if args.no_wifi:
        # 空 SSID = campusnet 完全不做 Wi-Fi 操作
        cfg.wifi_ssid = ""

    if not cfg.username:
        log("配置里没有账号（username）", "error")
        return 2

    # 密码：配置文件明文 → 环境变量（Config.resolve_password 自带这个顺序）
    if not cfg.resolve_password(prompt=False):
        log("没有取到密码：请在 config.json 填 password，或设置 CAMPUSNET_PASSWORD", "error")
        return 2

    # 先做一次轻量探测，再决定要不要建 Runner 与详细日志。
    #
    # 这样做有个很实际的好处：**已联网时日志保持干净**。
    # 触发式任务（心跳/网络事件）多数情况下都是「已联网、什么都不用做」，
    # 如果一上来就挂上带 HTTP 调试输出的 logger，每 5 分钟就会往日志里
    # 灌一堆 GET/POST 明细，真正出问题时反而翻不到重点。
    started = time.time()
    try:
        from campusnet.detector import check_online
        from campusnet.session import Session as NetSession

        quiet_session = NetSession(timeout=cfg.timeout, use_proxy=cfg.use_proxy)
        status = check_online(quiet_session, (cfg.portal_ip if "//" in (cfg.portal_ip or "")
                                              else ("http://" + cfg.portal_ip + "/") if cfg.portal_ip else ""))
    except Exception as exc:  # noqa: BLE001
        log("联网探测异常：{}: {}".format(type(exc).__name__, exc), "error")
        return 1

    if status.online and not args.force:
        # 这条是「任务确实跑过且一切正常」的唯一凭据，即便是 --quiet 也要留下，
        # 否则日志里会只剩半截探测细节，看起来像是任务被中断了。
        log("已联网，无需认证（探测点 {}）".format(status.probe or "通过"), "ok")
        return 0

    runner = Runner(cfg, logger=_make_session_logger(log))

    try:
        if status.online and args.force:
            log("当前已联网，但指定了 --force，仍重新认证", "warn")
        else:
            log("未联网（{}），开始认证".format(status.describe()), "warn")

        # 认证前先拿到「带会话令牌的门户地址」。
        # 这一步是必须的：门户的 nasip 字段要令牌，而令牌只存在于
        # 网关跳转过来的那个带参数地址里；config 里的裸 IP 拿不到它。
        portal_url = discover_portal_url(cfg, log, allow_web_probe=args.web_probe)

        result = runner.ensure_online(force=args.force, portal_url=portal_url)
    except Exception as exc:  # noqa: BLE001 —— 触发式脚本绝不能让异常冒到任务计划
        log("认证过程异常：{}: {}".format(type(exc).__name__, exc), "error")
        return 1

    elapsed = time.time() - started
    if result.ok:
        log("{}（认证方式：{}，耗时 {:.1f}s）".format(
            result.message or "认证成功", result.provider or "-", elapsed), "ok")
        return 0

    log("认证失败：{}（耗时 {:.1f}s）".format(result.message or "原因未知", elapsed), "error")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
