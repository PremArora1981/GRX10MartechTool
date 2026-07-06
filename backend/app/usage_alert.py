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

import html
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


def send_html(subject: str, html: str) -> bool:
    """Send one HTML email via ZeptoMail. Returns True on success. Shared by the
    real-time alert and the daily digest. No-op (False) if unconfigured."""
    global _warned_unconfigured
    token = os.getenv("ZEPTOMAIL_TOKEN")
    frm = os.getenv("ALERT_EMAIL_FROM")
    to = os.getenv("ALERT_EMAIL_TO", "prem@grx10.com")
    url = os.getenv("ZEPTOMAIL_API_URL", "https://api.zeptomail.in/v1.1/email")
    if not token or not frm:
        if not _warned_unconfigured:
            logger.warning("email: ZEPTOMAIL_TOKEN / ALERT_EMAIL_FROM not set — email disabled")
            _warned_unconfigured = True
        return False
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
                "subject": subject,
                "htmlbody": html,
            },
            timeout=15.0,
        )
        if r.status_code >= 300:
            logger.warning("email non-2xx: %s %s", r.status_code, r.text[:200])
            return False
        logger.info("email sent to %s: %s", to, subject)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("email send failed: %s", exc)
        return False


def _send_email(path: str, ip: str, ua: str, ts: str) -> None:
    window = _debounce_seconds() // 60
    # path / ip / ua come from the untrusted request — escape for the HTML email.
    body = (
        "<p><strong>Someone is using the GRX10 Market Research app.</strong></p>"
        "<ul>"
        f"<li>Time: {html.escape(ts)}</li>"
        f"<li>First request: <code>{html.escape(path)}</code></li>"
        f"<li>IP: {html.escape(ip)}</li>"
        f"<li>Browser: {html.escape(ua[:200])}</li>"
        "</ul>"
        f"<p style='color:#888'>You'll get at most one of these per {window} min of activity. "
        "The app is anonymous, so this fires on any real browser session.</p>"
    )
    send_html("GRX10 alpha — client is using the app", body)


def _log_event(ip: str, path: str, ua: str) -> None:
    """Persist one usage row (best-effort) for the daily digest."""
    try:
        from sqlalchemy import text
        from backend.app.db import get_session
        session = next(get_session())
        try:
            session.execute(
                text("INSERT INTO usage_events (ip, path, user_agent) VALUES (:ip, :p, :ua)"),
                {"ip": ip, "p": path[:300], "ua": ua[:400]},
            )
            session.commit()
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001 — logging must never break a request
        logger.debug("usage log insert failed: %s", exc)


class UsageAlertMiddleware(BaseHTTPMiddleware):
    """Log every real browser request + fire a debounced 'client is active' email."""

    async def dispatch(self, request, call_next):
        try:
            path = request.url.path
            ua = request.headers.get("user-agent", "")
            if _is_real_browser_hit(path, ua):
                xff = request.headers.get("x-forwarded-for", "")
                ip = (xff.split(",")[0].strip() if xff
                      else (request.client.host if request.client else "?"))
                # Log every real hit (for the daily digest) on a background thread.
                threading.Thread(target=_log_event, args=(ip, path, ua), daemon=True).start()
                # Debounced real-time email.
                now = time.time()
                fire = False
                global _last_alert
                with _lock:
                    if now - _last_alert > _debounce_seconds():
                        _last_alert = now
                        fire = True
                if fire:
                    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    threading.Thread(
                        target=_send_email, args=(path, ip, ua, ts), daemon=True
                    ).start()
        except Exception as exc:  # noqa: BLE001 — alerting must never break a request
            logger.debug("usage alert middleware error: %s", exc)
        return await call_next(request)
