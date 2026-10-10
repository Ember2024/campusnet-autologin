"""账号设置：保留原始配置，只原子更新账号和密码。"""

import json
from pathlib import Path

from .runtime import write_json


def _read_document(path):
    try:
        with Path(path).open("r", encoding="utf-8-sig") as stream:
            document = json.load(stream)
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as exc:
        raise ValueError("配置文件不是合法 JSON，请修复后再保存") from exc
    if not isinstance(document, dict):
        raise ValueError("配置顶层必须是 JSON 对象")
    return document


def read_settings(path):
    """返回文件中的账号、密码；首次配置返回空值，不应用环境变量。"""
    document = _read_document(path) or {}
    username, password = document.get("username", ""), document.get("password", "")
    if not isinstance(username, str) or not isinstance(password, str):
        raise ValueError("配置中的账号和密码必须是字符串")
    return username, password


def save_credentials(path, username, password):
    """保存账号密码，保留所有其它字段；损坏的 JSON 绝不覆盖。"""
    if not isinstance(username, str) or not username.strip():
        raise ValueError("请输入账号")
    if not isinstance(password, str) or not password.strip():
        raise ValueError("请输入密码")
    document = _read_document(path)
    if document is None:
        document = {
            "provider": "ruijie",
            "portal_ip": "http://192.168.24.65",
            "wifi_ssid": "tjus_wifi",
            "timeout": 8,
            "use_proxy": False,
            "options": {
                "service": "",
                "query_string": "",
                "online_check_seconds": 15,
                "verify_delay": 2,
            },
        }
    document["username"] = username.strip()
    document["password"] = password
    write_json(path, document)
