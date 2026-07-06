"""Client-usage email alert (alpha).

Sends an email to the operator (ALERT_EMAIL_TO) via ZeptoMail whenever a real
person uses the app — debounced so a burst of clicks yields ONE "client is
active" email per ALERT_DEBOUNCE_SECONDS window, not one per request.

What counts as "real usage": a browser request to the API. We filter out health
checks, docs, static assets, uptime pings, and server-side-render / bot traffic
(Node/undici/crawler user-agents) so only genuine browser activity fires. The
app is anonymous, so we can't prove it's *the* client — per the alpha assumption
("only the client has the link"), any real human hit is treated as the client.

Inert until configured: if ZEPTOMAIL_TOKEN / ALERT_EMAIL_FROM are unset, the
middleware simply does nothing (logs once). No request is ever blocked or slowed
— the email is sent on a background thread.

Env vars:
  ZEPTOMAIL_TOKEN         ZeptoMail "Send Mail" token (Zoho-enczapikey ...)
  ALERT_EMAIL_FROM        verified sender on a ZeptoMail-verified domain
  ALERT_EMAIL_TO          recipient (default prem@grx10.com)
  ZEPTOMAIL_API_URL       default https://api.zeptomail.in/v1.1/email (India DC)
  ALERT_DEBOUNCE_SECONDS  default 1800 (30 min)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

import httpx
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("grx10.usage_alert")

_IGNORE_PREFIXES = ("/health", "/docs", "/openapi.json", "/redoc", "/favicon")
# User-agent fragments that indicate NOT-a-browser (SSR, uptime, bots, tooling).
_NON_BROWSER_UA = (
    "bot", "crawler", "spider", "slurp", "monitor", "uptime", "pingdom",
    "curl", "wget", "httpx", "python-requests", "python", "node", "undici",
    "next.js", "render", "go-http", "okhttp", "postman", "headless",
)

_last_alert = 0.0
_lock = threading.Lock()
_warned_unconfigured = False


def _debounce_seconds() -> int:
    try:
        return int(os.getenv("ALERT_DEBOUNCE_SECONDS", "1800"))
    except ValueError:
        return 1800


def _is_real_browser_hit(path: str, ua: str) -> bool:
    if any(path.startswith(p) for p in _IGNORE_PREFIXES):
        return False
    ua = (ua or "").lower()
    if not ua:
        return False
    if any(frag in ua for frag in _NON_BROWSER_UA):
        return False
    # Real browsers all send a "mozilla/5.0" token.
    return "mozilla/" in ua


def _send_email(path: str, ip: str, ua: str, ts: str) -> None:
    global _warned_unconfigured
    token = os.getenv("ZEPTOMAIL_TOKEN")
    frm = os.getenv("ALERT_EMAIL_FROM")
    to = os.getenv("ALERT_EMAIL_TO", "prem@grx10.com")
    url = os.getenv("ZEPTOMAIL_API_URL", "https://api.zeptomail.in/v1.1/email")
    if not token or not frm:
        if not _warned_unconfigured:
            logger.warning("usage alert: ZEPTOMAIL_TOKEN / ALERT_EMAIL_FROM not set — alerts disabled")
            _warned_unconfigured = True
        return
    window = _debounce_seconds() // 60
    body = (
        "<p><strong>Someone is using the GRX10 Market Research app.</strong></p>"
        "<ul>"
        f"<li>Time: {ts}</li>"
        f"<li>First request: <code>{path}</code></li>"
        f"<li>IP: {ip}</li>"
        f"<li>Browser: {ua[:200]}</li>"
        "</ul>"
        f"<p style='color:#888'>You'll get at most one of these per {window} min of activity. "
        "The app is anonymous, so this fires on any real browser session.</p>"
    )
    try:
        r = httpx.post(
            url,
            headers={
                "Authorization": f"Zoho-enczapikey {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "from": {"address": frm},
                "to": [{"email_address": {"address": to}}],
                "subject": "GRX10 alpha — client is using the app",
                "htmlbody": body,
            },
            timeout=10.0,
        )
        if r.status_code >= 300:
            logger.warning("usage alert email non-2xx: %s %s", r.status_code, r.text[:200])
        else:
            logger.info("usage alert email sent to %s", to)
    except Exception as exc:  # noqa: BLE001
        logger.warning("usage alert email failed: %s", exc)


class UsageAlertMiddleware(BaseHTTPMiddleware):
    """Fire a debounced 'client is active' email on real browser requests."""

    async def dispatch(self, request, call_next):
        try:
            path = request.url.path
            ua = request.headers.get("user-agent", "")
            if _is_real_browser_hit(path, ua):
                now = time.time()
                fire = False
                global _last_alert
                with _lock:
                    if now - _last_alert > _debounce_seconds():
                        _last_alert = now
                        fire = True
                if fire:
                    xff = request.headers.get("x-forwarded-for", "")
                    ip = (xff.split(",")[0].strip() if xff
                          else (request.client.host if request.client else "?"))
                    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    threading.Thread(
                        target=_send_email, args=(path, ip, ua, ts), daemon=True
                    ).start()
        except Exception as exc:  # noqa: BLE001 — alerting must never break a request
            logger.debug("usage alert middleware error: %s", exc)
        return await call_next(request)
