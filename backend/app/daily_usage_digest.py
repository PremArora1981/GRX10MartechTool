"""Daily client-usage digest email (alpha, ~90 days).

Summarizes the last 24h of ``usage_events`` and emails ALERT_EMAIL_TO via
ZeptoMail — ALWAYS, even on a zero-usage day. For each distinct visitor IP it
geolocates the city/country (ip-api.com, free) and reports hit count, first/last
seen, and sample paths.

Run daily (Render Cron):  python -m backend.app.daily_usage_digest

Auto-stops after ALERT_DIGEST_UNTIL (an ISO date). Past that, it exits without
sending, so the cron becomes a no-op at the end of the alpha window without
needing to be torn down. Defaults to 90 days is enforced by the cron config /
the env var set at deploy time.
"""

from __future__ import annotations

import html
import logging
import os
from datetime import date, datetime, timezone

import httpx
from sqlalchemy import text

from backend.app.db import get_session
from backend.app.usage_alert import send_html

logger = logging.getLogger("grx10.daily_usage_digest")

_PRIVATE_PREFIXES = ("10.", "192.168.", "127.", "172.16.", "172.17.", "172.18.",
                     "172.19.", "172.2", "172.30.", "172.31.", "::1", "fc", "fd")


def _geolocate(ips: list[str]) -> dict[str, str]:
    """IP -> 'City, Country' via ip-api.com batch (best-effort)."""
    out: dict[str, str] = {}
    public = [ip for ip in ips if ip and not ip.startswith(_PRIVATE_PREFIXES) and ip != "?"]
    if not public:
        return out
    try:
        r = httpx.post(
            "http://ip-api.com/batch?fields=query,status,country,city,regionName,isp",
            json=[{"query": ip} for ip in public[:100]],
            timeout=15.0,
        )
        for row in r.json() if r.status_code < 300 else []:
            if row.get("status") == "success":
                city = row.get("city") or row.get("regionName") or ""
                country = row.get("country") or ""
                isp = row.get("isp") or ""
                label = ", ".join(p for p in (city, country) if p) or "unknown"
                if isp:
                    label += f" · {isp}"
                out[row.get("query", "")] = label
    except Exception as exc:  # noqa: BLE001
        logger.warning("geolocation failed: %s", exc)
    return out


def build_and_send(hours: int = 24) -> bool:
    # Respect the alpha end date, if set.
    until = os.getenv("ALERT_DIGEST_UNTIL")
    if until:
        try:
            if date.today() > datetime.fromisoformat(until).date():
                logger.info("digest window ended (%s) — skipping", until)
                return False
        except ValueError:
            pass

    session = next(get_session())
    try:
        rows = session.execute(
            text(
                "SELECT ip, COUNT(*) hits, MIN(ts) first_seen, MAX(ts) last_seen, "
                "       (array_agg(DISTINCT path))[1:5] paths "
                "FROM usage_events WHERE ts > now() - make_interval(hours => :h) "
                "GROUP BY ip ORDER BY hits DESC"
            ),
            {"h": hours},
        ).mappings().all()
        total = session.execute(
            text("SELECT COUNT(*) FROM usage_events WHERE ts > now() - make_interval(hours => :h)"),
            {"h": hours},
        ).scalar_one()
    finally:
        session.close()

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    visitors = len(rows)

    if not rows:
        subject = f"GRX10 usage {day} — no activity"
        html = (f"<p><strong>No usage in the last {hours}h.</strong> "
                "The client did not open the app today.</p>"
                "<p style='color:#888'>Daily digest — GRX10 alpha.</p>")
        return send_html(subject, html)

    geo = _geolocate([r["ip"] for r in rows])
    tr = []
    for r in rows:
        # All of ip / location / paths derive from untrusted request data (or an
        # external geo API), so escape before embedding in the HTML email.
        ip = html.escape(r["ip"] or "—")
        loc = html.escape(geo.get(r["ip"], "—"))
        fs = r["first_seen"].strftime("%H:%M") if r["first_seen"] else ""
        ls = r["last_seen"].strftime("%H:%M") if r["last_seen"] else ""
        paths = html.escape(", ".join(r["paths"] or [])[:120])
        tr.append(
            f"<tr><td style='padding:4px 10px'>{ip}</td>"
            f"<td style='padding:4px 10px'>{loc}</td>"
            f"<td style='padding:4px 10px;text-align:right'>{int(r['hits'])}</td>"
            f"<td style='padding:4px 10px'>{fs}–{ls} UTC</td>"
            f"<td style='padding:4px 10px;color:#666;font-size:12px'>{paths}</td></tr>"
        )
    subject = f"GRX10 usage {day} — {visitors} visitor(s), {total} hits"
    html = (
        f"<p><strong>{visitors} visitor(s)</strong> and <strong>{total} requests</strong> "
        f"in the last {hours}h.</p>"
        "<table style='border-collapse:collapse;font-family:sans-serif;font-size:13px'>"
        "<tr style='background:#f2f2f2'>"
        "<th style='padding:4px 10px;text-align:left'>IP</th>"
        "<th style='padding:4px 10px;text-align:left'>Location · ISP</th>"
        "<th style='padding:4px 10px'>Hits</th>"
        "<th style='padding:4px 10px;text-align:left'>Window</th>"
        "<th style='padding:4px 10px;text-align:left'>Sample paths</th></tr>"
        + "".join(tr) +
        "</table>"
        "<p style='color:#888;margin-top:12px'>Anonymous app — any real browser session is "
        "assumed to be the client. City/ISP via ip-api.com (best-effort).</p>"
    )
    return send_html(subject, html)


def _already_sent_today() -> bool:
    """True if today's digest row already exists (idempotency across restarts)."""
    session = next(get_session())
    try:
        return session.execute(
            text("SELECT 1 FROM digest_log WHERE sent_date = current_date")
        ).first() is not None
    except Exception:  # noqa: BLE001 — table may not exist yet
        return False
    finally:
        session.close()


def _mark_sent_today() -> None:
    session = next(get_session())
    try:
        session.execute(
            text("INSERT INTO digest_log (sent_date) VALUES (current_date) ON CONFLICT DO NOTHING"))
        session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("mark_sent failed: %s", exc)
    finally:
        session.close()


async def scheduler_loop() -> None:
    """In-process daily scheduler for the API web service (always-on, paid plan).

    Every ~30 min: once the UTC hour reaches ALERT_DIGEST_HOUR (default 01:00) and
    today's digest hasn't been sent, build + send it, then record the day so a
    restart won't re-send. Missed sends (send failure) retry on the next tick.
    """
    import asyncio
    target_hour = int(os.getenv("ALERT_DIGEST_HOUR", "1"))
    logger.info("daily digest scheduler started (target %02d:00 UTC)", target_hour)
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.hour >= target_hour and not _already_sent_today():
                sent = await asyncio.to_thread(build_and_send)
                if sent:
                    _mark_sent_today()
                    logger.info("daily digest sent + recorded for %s", now.date())
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            logger.warning("digest scheduler tick error: %s", exc)
        await asyncio.sleep(int(os.getenv("ALERT_DIGEST_CHECK_SECONDS", "1800")))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ok = build_and_send()
    if ok:
        _mark_sent_today()
    print("digest sent" if ok else "digest not sent (unconfigured, out of window, or send failed)")
