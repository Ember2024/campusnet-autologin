"""读取门户提供的会话参数；仅接受配置门户同源的跳转。"""

from html import unescape
import re
from urllib.parse import urljoin, urlsplit

from .session import origin


def has_query(url):
    query = urlsplit(url).query
    return any(key + "=" in query for key in ("wlanuserip", "nasip", "wlanacname"))


def discover_portal_url(cfg, session, status_url="", allow_web_probe=True):
    configured = cfg.portal_ip.strip()
    if not configured:
        return status_url
    base = origin(configured)

    def accept(value, source):
        value = urljoin(source, unescape(value))
        return value if origin(value) == base and has_query(value) else ""

    fresh = accept(status_url, base) if status_url else ""
    if fresh:
        return fresh
    candidates = [base + path for path in ("/", "/eportal/", "/eportal/index.jsp")]
    if allow_web_probe:
        candidates += ["http://www.msftconnecttest.com/connecttest.txt",
                       "http://connect.rom.miui.com/generate_204"]
    for url in candidates:
        try:
            resp = session.get(url, timeout=min(cfg.timeout, 3))
        except Exception:
            continue
        fresh = accept(resp.location, resp.url) if resp.location else ""
        if fresh:
            return fresh
        for embedded in re.findall(r'''["']([^"']*(?:wlanuserip|nasip)=[^"']+)["']''', resp.text):
            fresh = accept(embedded, resp.url)
            if fresh:
                return fresh
    return configured if has_query(configured) else base
