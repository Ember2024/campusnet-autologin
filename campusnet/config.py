"""配置与凭据读写。"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

APP_NAME = "campusnet"

ENV_USERNAME = "CAMPUSNET_USERNAME"
ENV_PASSWORD = "CAMPUSNET_PASSWORD"
ENV_PROVIDER = "CAMPUSNET_PROVIDER"
ENV_OPTIONS = "CAMPUSNET_OPTIONS"  # JSON 串


def config_dir() -> str:
    """配置文件所在目录。"""
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_NAME)


def default_config_path() -> str:
    return os.path.join(config_dir(), "config.json")


@dataclass
class Config:
    """外置配置；加载与读取凭据不会发起交互或修改配置文件。"""

    username: str = ""
    password: str = ""
    provider: str = "auto"
    portal_ip: str = ""
    wifi_ssid: str = ""
    verify_url: str = "http://connect.rom.miui.com/generate_204"
    timeout: int = 8
    use_proxy: bool = False
    password_source: str = ""
    options: Dict[str, Any] = field(default_factory=dict)
    path: str = ""

    # ------------------------------------------------------------ 序列化
    FIELDS = (
        "username",
        "password",
        "provider",
        "portal_ip",
        "wifi_ssid",
        "verify_url",
        "timeout",
        "use_proxy",
        "options",
    )

    @classmethod
    def from_dict(cls, data: Dict[str, Any], path: str = "") -> "Config":
        if not isinstance(data, dict):
            raise ValueError("配置顶层必须是 JSON 对象")
        cfg = cls(path=path)
        for key in cls.FIELDS:
            if key in data and data[key] is not None:
                setattr(cfg, key, data[key])
        if not isinstance(cfg.options, dict):
            raise ValueError("options 必须是 JSON 对象")
        cfg.options = dict(cfg.options)
        return cfg

    # ------------------------------------------------------------ 读写
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        path = path or default_config_path()
        data: Dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as fh:
                try:
                    data = json.load(fh)
                except ValueError as exc:
                    raise ValueError("配置文件不是合法 JSON：{}（{}）".format(path, exc)) from exc
        cfg = cls.from_dict(data, path=path)
        cfg.apply_env()
        return cfg

    # ------------------------------------------------------------ 环境变量
    def apply_env(self) -> "Config":
        if os.environ.get(ENV_USERNAME):
            self.username = os.environ[ENV_USERNAME]
        if os.environ.get(ENV_PROVIDER):
            self.provider = os.environ[ENV_PROVIDER]
        if os.environ.get(ENV_OPTIONS):
            try:
                extra = json.loads(os.environ[ENV_OPTIONS])
                if isinstance(extra, dict):
                    self.options.update(extra)
            except ValueError:
                pass
        return self

    # ------------------------------------------------------------ 取密码
    def resolve_password(self, prompt: bool = False, allow_keyring: bool = True) -> str:
        """按环境变量 → 钥匙串 → 配置文件取密码；永不提示输入。"""
        env = os.environ.get(ENV_PASSWORD)
        if env:
            self.password_source = "env"
            return env

        if allow_keyring:
            secret = _keyring_get(self.username)
            if secret:
                self.password_source = "keyring"
                return secret

        if self.password:
            if not self.password_source:
                self.password_source = "config"
            return self.password

        return ""


# -------------------------------------------------------------------- 工具
def load_config(path: Optional[str] = None) -> Config:
    """便捷函数：``Config.load()`` 的别名。"""
    return Config.load(path)


def _keyring_get(username: str) -> str:
    if not username:
        return ""
    try:
        import keyring  # type: ignore
    except ImportError:
        return ""
    try:
        return keyring.get_password(APP_NAME, username) or ""
    except Exception:  # noqa: BLE001 - 钥匙串后端经常抽风，静默降级
        return ""
