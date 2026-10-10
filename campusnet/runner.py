"""共用认证流程及守护调度：确认校园 Wi-Fi → 探测 → 登录 → 校验。"""

from dataclasses import dataclass, field
from typing import Callable, List, Optional
import time

from .config import Config
from .detector import Detection, NetStatus, check_online, detect, provider_order
from .portal import discover_portal_url
from .providers import LoginResult, get_provider
from .session import HttpError, Session, local_ip, local_mac, origin
from .singleton import ProcessLock
from .wifi import WifiResult, current_ssid

@dataclass
class RunResult:
    ok: bool = False
    skipped: bool = False
    provider: str = ""
    message: str = ""
    status_before: Optional[NetStatus] = None
    detection: Optional[Detection] = None
    attempts: List[LoginResult] = field(default_factory=list)


class Runner:
    def __init__(self, cfg: Config, logger: Optional[Callable] = None):
        self.cfg = cfg
        self.log = logger or (lambda *args: None)
        self._session = None
        self._probe_session = None

    @property
    def session(self):
        if self._session is None:
            self._session = Session(timeout=self.cfg.timeout, use_proxy=self.cfg.use_proxy,
                                    retries=0, logger=lambda msg: self.log(msg, "debug"),
                                    request_guard=self._require_campus_wifi)
        return self._session

    def _require_campus_wifi(self):
        # Providers may fetch several pages before submitting credentials.
        # Recheck immediately before each actual authentication HTTP request.
        if not self.ensure_wifi().ok:
            raise HttpError("当前已离开目标校园 Wi-Fi，取消认证请求")

    @property
    def client_ip(self):
        return local_ip()

    def status(self):
        if self._probe_session is None:
            self._probe_session = Session(timeout=3, use_proxy=self.cfg.use_proxy, retries=0)
        return check_online(self._probe_session, self.cfg.portal_ip)

    def detect(self, status=None):
        # 已配置学校协议和门户时，无需每轮扫描网关或其它候选。
        if self.cfg.provider not in ("", "auto") and self.cfg.portal_ip:
            return Detection(portal=origin(self.cfg.portal_ip),
                             scores=[(self.cfg.provider, 1.0)])
        return detect(self.session, self.cfg, status)

    def ensure_wifi(self):
        target = self.cfg.wifi_ssid
        if not target:
            return WifiResult(message="未配置校园 Wi-Fi，跳过认证")
        try:
            ssid = current_ssid()
        except Exception:
            return WifiResult(target=target, message="无法确认当前 Wi-Fi，跳过认证")
        if ssid != target:
            return WifiResult(ssid=ssid, target=target,
                              message="当前未连接目标校园 Wi-Fi，等待连接")
        return WifiResult(ok=True, ssid=ssid, target=target, message="已连接校园 Wi-Fi")

    def ensure_online(self, force=False, limit=3):
        # 所有入口共用同一把锁，异常/进程结束都会释放。
        lock = ProcessLock("campusnet-login")
        if not lock.acquire():
            return RunResult(skipped=True, message="另一认证进程正在工作")
        try:
            wifi = self.ensure_wifi()
            if not wifi.ok:
                return RunResult(skipped=True, message=wifi.message)
            status = self.status()
            if status.online and not force:
                return RunResult(ok=True, skipped=True, message="网络正常，无需认证",
                                 status_before=status)
            password = self.cfg.resolve_password(prompt=False)
            if not self.cfg.username or not password:
                return RunResult(message="配置中缺少账号或密码", status_before=status)
            detection = self.detect(status)
            portal = discover_portal_url(self.cfg, self.session, status.portal_url)
            portal = portal or detection.portal
            if not portal:
                return RunResult(message="未找到认证门户", status_before=status)
            if self.cfg.portal_ip and origin(portal) != origin(self.cfg.portal_ip):
                return RunResult(message="发现非配置门户，跳过认证", status_before=status)
            attempts = []
            for name in provider_order(self.cfg, detection, limit):
                # 探测可能耗时，提交凭据前再次确认仍在校园 Wi-Fi。
                wifi = self.ensure_wifi()
                if not wifi.ok:
                    return RunResult(skipped=True, message=wifi.message, attempts=attempts)
                try:
                    provider = get_provider(name)(self.session, self.cfg.options)
                    self.log("检测到校园网无法上网，尝试 {} 认证".format(name))
                    result = provider.login(portal, self.cfg.username, password,
                                            self.client_ip, local_mac())
                except Exception as exc:
                    result = LoginResult(False, name, "认证异常：{}".format(exc))
                attempts.append(result)
                if result.ok or result.already_online:
                    time.sleep(max(0, float(self.cfg.options.get("verify_delay", 2))))
                    # “账号已在线”也必须通过真实联网检查，避免停止重试。
                    if self.ensure_wifi().ok and self.status().online:
                        return RunResult(ok=True, provider=name, message="校园网已恢复联网",
                                         status_before=status, detection=detection, attempts=attempts)
                    return RunResult(provider=name, message="门户返回在线，但网络仍不可用",
                                     status_before=status, detection=detection, attempts=attempts)
            return RunResult(message=attempts[-1].message if attempts else "无可用认证方式",
                             status_before=status, detection=detection, attempts=attempts)
        finally:
            lock.release()
