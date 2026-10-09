#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校园网自动认证 —— 常驻守护（定期巡检 + 断网自动重连）。

与 ``run_login.py`` 的分工：

* ``run_login.py`` —— 被任务计划按事件/定时**触发**的一次性尝试，跑完就退出；
* ``daemon.py``     —— 常驻后台，自己按间隔巡检，断线立刻补认证。

守护进程用 PID 文件做单实例保护，重复启动会直接退出而不是抢网。
退出码：0 正常退出（含被要求停止）；1 异常；2 参数/配置错误。
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from datetime import datetime


def app_dir() -> str:
    """程序所在目录 —— 脚本运行与打包成 exe 都要得到同一个答案。

    打包后 ``__file__`` 指向临时解包目录（``%TEMP%\\_MEIxxxxxx``），
    不能用来定位 ``config.json`` / ``logs``。见 ``run_login.py`` 的同名函数。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


#: 向上查找配置时的最大层数。
_MAX_UP = 4


def find_config_path(explicit: str = "") -> str:
    """定位 ``config.json``。

    打包后 exe 在 ``<项目根>\\daemon\\daemon.exe``，而配置在上一级的项目根，
    所以要向上找一层。与 ``run_login.py`` 保持完全一致的策略。
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
    return os.path.join(start, "config.json")


BASE_DIR = app_dir()
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

#: 日志/pid/心跳统一放在**配置文件旁边**的 logs 目录 ——
#: 用户在 exe 模式下打开项目根就能看到日志，不用去 run_login\ 里翻。
_STATE_DIR = os.path.join(os.path.dirname(find_config_path()), "logs")

DEFAULT_CONFIG = find_config_path()
DEFAULT_LOG = os.path.join(_STATE_DIR, "daemon.log")
DEFAULT_PID = os.path.join(_STATE_DIR, "daemon.pid")
DEFAULT_HEARTBEAT = os.path.join(_STATE_DIR, "heartbeat.txt")
LOG_MAX_BYTES = 2 * 1024 * 1024


def _ensure_utf8_console() -> None:
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


def make_logger(log_path: str):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    _rotate(log_path)

    def log(message: str, level: str = "info") -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = "[{}] [{}] {}".format(stamp, level.upper(), message)
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


# ------------------------------------------------------------------ 单实例
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        # 非 Windows 或调用失败：退回 os.kill 探测
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def acquire_pid_file(path: str) -> bool:
    """拿到 PID 文件锁返回 True；已有存活实例返回 False。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                old = int((fh.read() or "0").strip() or "0")
        except (OSError, ValueError):
            old = 0
        if old and old != os.getpid() and pid_alive(old):
            return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    return True


def release_pid_file(path: str) -> None:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                if (fh.read() or "").strip() == str(os.getpid()):
                    os.remove(path)
    except OSError:
        pass


def main() -> int:
    _ensure_utf8_console()

    parser = argparse.ArgumentParser(description="校园网自动认证常驻守护")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--log", default=DEFAULT_LOG)
    parser.add_argument("--pid", default=DEFAULT_PID)
    parser.add_argument("--heartbeat", default=DEFAULT_HEARTBEAT)
    parser.add_argument("--interval", type=int, default=3,
                        help="联网正常时的巡检间隔（分钟，最小 1）")
    parser.add_argument("--warmup", type=float, default=None,
                        help="启动后的高频抢网秒数（默认取配置 wifi_warmup）")
    parser.add_argument("--heartbeat-interval", type=float, default=20.0,
                        help="心跳文件写入间隔（秒）")
    parser.add_argument("--once-check", action="store_true",
                        help="只做一轮检查就退出（自检用）")
    args = parser.parse_args()

    log = make_logger(args.log)

    if not acquire_pid_file(args.pid):
        log("已有一个守护进程在运行，本次退出（PID 文件：{}）".format(args.pid), "warn")
        return 0

    try:
        from campusnet.config import Config
        from campusnet.runner import Runner
    except ImportError as exc:
        log("无法导入 campusnet 引擎：{}".format(exc), "error")
        release_pid_file(args.pid)
        return 2

    try:
        cfg = Config.load(args.config)
    except Exception as exc:  # noqa: BLE001
        log("读取配置失败：{}".format(exc), "error")
        release_pid_file(args.pid)
        return 2

    if not cfg.username or not cfg.resolve_password(prompt=False):
        log("账号或密码缺失：请检查 {}".format(args.config), "error")
        release_pid_file(args.pid)
        return 2

    log("=" * 62)
    log("守护进程启动（PID {}），巡检间隔 {} 分钟".format(os.getpid(), max(1, args.interval)), "ok")
    log("账号：{}｜门户：{}｜校园 Wi-Fi：{}".format(
        cfg.username, cfg.portal_ip or "(自动探测)", cfg.wifi_ssid or "(未配置)"), "info")

    runner = Runner(cfg, logger=log)

    def touch_heartbeat() -> None:
        try:
            os.makedirs(os.path.dirname(args.heartbeat), exist_ok=True)
            with open(args.heartbeat, "w", encoding="utf-8") as fh:
                fh.write("{}\tpid={}\n".format(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
        except OSError:
            pass

    if args.once_check:
        ok = runner._watch_round_safe(None)  # noqa: SLF001 —— 自检就是要跑真实那一轮
        touch_heartbeat()
        release_pid_file(args.pid)
        log("自检一轮结束：{}".format("已联网" if ok else "仍未联网"), "ok" if ok else "error")
        return 0 if ok else 1

    # 心跳线程：即使认证卡在等待 Wi-Fi，外部也能看出守护还活着
    import threading

    stop_flag = threading.Event()

    def heartbeat_loop() -> None:
        while not stop_flag.is_set():
            touch_heartbeat()
            stop_flag.wait(args.heartbeat_interval)

    thread = threading.Thread(target=heartbeat_loop, name="heartbeat", daemon=True)
    thread.start()

    try:
        runner.watch(interval_minutes=max(1, args.interval),
                     warmup_seconds=args.warmup)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，守护进程退出", "info")
    except Exception as exc:  # noqa: BLE001
        log("守护进程异常退出：{}: {}".format(type(exc).__name__, exc), "error")
        return 1
    finally:
        stop_flag.set()
        touch_heartbeat()
        release_pid_file(args.pid)
        log("守护进程已退出", "info")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
