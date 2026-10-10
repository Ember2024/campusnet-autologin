"""脚本和 EXE 共用的路径、滚动日志、配置检查及静默异常处理。"""

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit


def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def find_config_path(explicit=""):
    if explicit:
        return str(Path(explicit).resolve())
    start = app_dir()
    for folder in (start, start.parent):
        path = folder / "config.json"
        if path.is_file():
            return str(path)
    return str(start / "config.json")


def state_dir(config):
    return Path(config).resolve().parent / "logs"


def make_logger(path, quiet=False):
    # 保留 Windows 的逻辑路径，避免把应用容器重定向路径固化进日志句柄。
    path = Path(path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(str(path))
    logger.setLevel(logging.INFO)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024,
                                  backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s",
                                           datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    if not quiet and sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(handler.formatter)
        logger.addHandler(console)
    logger.propagate = False

    def log(message, level="info"):
        # 请求 URL 中的门户会话参数不进入日志。
        message = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[redacted]", str(message))
        logger.log({"debug": logging.DEBUG, "warn": logging.WARNING,
                    "error": logging.ERROR}.get(level, logging.INFO), message)
    return log


def load_config(path):
    from .config import Config
    from .providers import PROVIDERS
    if not Path(path).is_file():
        raise ValueError("配置文件不存在：" + str(path))
    cfg = Config.load(path)
    if not isinstance(cfg.username, str) or not cfg.username.strip() or not isinstance(cfg.password, str):
        raise ValueError("账号和密码必须是字符串，账号不能为空")
    if not cfg.resolve_password(prompt=False):
        raise ValueError("配置中缺少账号或密码")
    if not isinstance(cfg.wifi_ssid, str) or not cfg.wifi_ssid.strip():
        raise ValueError("请配置 wifi_ssid，自动登录需要明确的校园 Wi-Fi 名称")
    if type(cfg.timeout) not in (int, float) or not 0 < cfg.timeout <= 60:
        raise ValueError("timeout 必须在 0 到 60 秒之间")
    if cfg.provider not in PROVIDERS and cfg.provider != "auto":
        raise ValueError("provider 不在支持列表中")
    if not isinstance(cfg.portal_ip, str):
        raise ValueError("portal_ip 必须是 URL 字符串")
    portal = urlsplit(cfg.portal_ip)
    if portal.scheme not in ("http", "https") or not portal.hostname or portal.username or portal.password:
        raise ValueError("portal_ip 必须是明确的 HTTP/HTTPS 门户地址")
    if not isinstance(cfg.use_proxy, bool):
        raise ValueError("use_proxy 必须是 true 或 false")
    gap = float(cfg.options.get("online_check_seconds", 15))
    if not 1 <= gap <= 3600:
        raise ValueError("online_check_seconds 必须在 1 到 3600 秒之间")
    if not 0 <= float(cfg.options.get("verify_delay", 2)) <= 30:
        raise ValueError("verify_delay 必须在 0 到 30 秒之间")
    return cfg


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
        # Windows 读取方可能短暂持有不允许删除的句柄。
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def self_test(cfg, report):
    from .singleton import ProcessLock
    from .wifi import current_ssid
    lock = ProcessLock("campusnet-selftest-" + str(os.getpid()))
    locked = lock.acquire()
    lock.release()
    ssid = current_ssid()
    data = {"ok": locked, "wifi_readable": bool(ssid),
            "frozen": bool(getattr(sys, "frozen", False)),
            "ssid": ssid, "target_ssid": cfg.wifi_ssid,
            "lock_ok": locked, "pid": os.getpid()}
    if report:
        write_json(report, data)
    return 0 if data["ok"] else 1


def safe_main(main):
    # Python 异常不能落到 PyInstaller 的错误对话框中。
    try:
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        return main()
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        try:
            make_logger(state_dir(find_config_path()) / "startup.log", quiet=True)(
                "启动失败：{}: {}".format(type(exc).__name__, exc), "error")
        except Exception:
            pass
        return 2
