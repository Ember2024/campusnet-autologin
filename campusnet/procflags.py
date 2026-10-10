"""Windows 辅助命令隐藏控制台，并显式提供有效的标准输入句柄。"""

import os
import subprocess


def no_window_kwargs() -> dict:
    """Windows 返回隐藏控制台的 creationflags；其它平台返回空 dict。"""
    if os.name == "nt":
        # pythonw / PyInstaller windowed 没有可继承的 stdin 句柄。
        # 显式提供 DEVNULL，避免子进程以 WinError 6 启动失败。
        return {"creationflags": subprocess.CREATE_NO_WINDOW,
                "stdin": subprocess.DEVNULL}
    return {}
