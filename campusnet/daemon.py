"""单实例守护与退避；网络认证由 Runner 负责。"""

from datetime import datetime, timezone
import os
import sys
import threading
import time

from .runner import RunResult
from .singleton import ProcessLock

RETRY_DELAYS = (5, 15, 30, 60)


class Daemon:
    def __init__(self, runner=None, publish=None, *, reload_runner=None, logger=None,
                 target_ssid="tjus_wifi"):
        self.runner = runner
        self.log = logger or (runner.log if runner is not None else lambda *args: None)
        self._logger = logger
        self.reload_runner = reload_runner
        self._reload_requested = threading.Event()
        if reload_runner is not None:
            self._reload_requested.set()
        self.publish = publish or (lambda state: None)
        self.state = {"pid": os.getpid(), "executable": sys.executable,
                      "target_ssid": runner.cfg.wifi_ssid if runner is not None else target_ssid,
                      "ssid": "",
                      "retries": 0, "next_retry_seconds": 0, "message": ""}

    def request_reload(self):
        """供设置窗口调用；只发送信号，认证及 Runner 替换始终在守护线程。"""
        self._reload_requested.set()

    def _reload(self):
        self._reload_requested.clear()
        if self.reload_runner is None:
            return
        try:
            runner = self.reload_runner()
            if runner is not None:
                self.state["target_ssid"] = runner.cfg.wifi_ssid
                if self._logger is None:
                    self.log = runner.log
            self.runner = runner
        except Exception as exc:
            self.runner = None
            self.log("配置重载失败：{}".format(type(exc).__name__), "warn")
        self.state["ssid"] = ""

    def _wait(self, stop, seconds):
        if self.reload_runner is None:
            # 保留原有 stop_event 等待协议，旧调用无需额外的唤醒事件。
            stop.wait(seconds)
            return
        deadline = time.monotonic() + seconds
        while not stop.is_set() and not self._reload_requested.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            # 配置保存立即唤醒；同时每 250ms 响应应用退出。
            self._reload_requested.wait(min(remaining, 0.25))

    def update(self, state, **fields):
        self.state.update(fields, state=state, heartbeat=datetime.now(timezone.utc).isoformat())
        try:
            self.publish(dict(self.state))
        except OSError as exc:
            self.log("无法写入状态：{}".format(type(exc).__name__), "warn")

    def run(self, stop_event=None):
        lock = ProcessLock("campusnet-watch")
        if not lock.acquire():
            self.log("守护已在运行，本次启动退出")
            return 0
        try:
            self.update("starting")
            self.watch(stop_event)
            return 0
        finally:
            self.update("stopped", next_retry_seconds=0)
            lock.release()

    def watch(self, stop_event=None):
        stop = stop_event or threading.Event()
        gap = float(self.runner.cfg.options.get("online_check_seconds", 15)) if self.runner is not None else 15
        retries, next_attempt = 0, 0.0
        previous, connected = None, False
        self.log("守护启动：每 {:g} 秒检查，失败按 5/15/30/60 秒重试".format(gap))
        while not stop.is_set():
            if self._reload_requested.is_set():
                self._reload()
                retries, next_attempt, previous, connected = 0, 0.0, None, False
                gap = float(self.runner.cfg.options.get("online_check_seconds", 15)) if self.runner is not None else 15
            if self.runner is None:
                message = "请打开设置填写有效的账号和密码"
                self.update("needs_config", ssid="", retries=0, next_retry_seconds=0, message=message)
                if previous != message:
                    self.log(message)
                    previous = message
                self._wait(stop, 15)
                continue
            try:
                wifi = self.runner.ensure_wifi()
                self.state["ssid"] = wifi.ssid
                if not wifi.ok:
                    retries, next_attempt, connected = 0, 0.0, False
                    message, delay = wifi.message, gap
                    self.update("waiting", retries=0, next_retry_seconds=0, message=message)
                else:
                    if not connected:
                        retries, next_attempt = 0, 0.0
                    connected = True
                    remaining = next_attempt - time.monotonic()
                    if remaining > 0:
                        self.update(self.state["state"], next_retry_seconds=round(remaining, 1))
                        self._wait(stop, min(gap, remaining))
                        continue
                    self.update("checking", next_retry_seconds=0)
                    try:
                        result = self.runner.ensure_online()
                    except Exception as exc:
                        result = RunResult(message="本轮异常：{}".format(type(exc).__name__))
                    message = result.message
                    if result.ok or result.skipped:
                        retries, delay = 0, gap
                        state = "online" if result.ok else "waiting"
                    else:
                        delay = RETRY_DELAYS[min(retries, len(RETRY_DELAYS) - 1)]
                        retries += 1
                        state = "retrying"
                        self.log("{}；{} 秒后重试".format(message, delay), "warn")
                    next_attempt = time.monotonic() + delay
                    self.update(state, retries=retries, next_retry_seconds=delay, message=message)
                    delay = min(gap, delay)
                if message != previous:
                    self.log(message)
                    previous = message
            except Exception as exc:
                delay = RETRY_DELAYS[min(retries, len(RETRY_DELAYS) - 1)]
                retries += 1
                next_attempt = time.monotonic() + delay
                message = "检查异常：{}".format(type(exc).__name__)
                self.log(message, "error")
                self.update("retrying", retries=retries, next_retry_seconds=delay, message=message)
                delay = min(gap, delay)
            self._wait(stop, delay)
