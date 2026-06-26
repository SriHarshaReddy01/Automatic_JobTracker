#!/usr/bin/env python3
"""
Remote Job Digest
==================
Pulls fresh remote job postings (last 24-72h) from:
  - Working Nomads          (open public JSON API)
  - JobsCollider            (public RSS feed, software-dev category by default)
  - Gmail alert emails from LinkedIn / Indeed / Glassdoor / Wellfound
    (these you set up as native saved-search alerts; this script just
     reads, parses, and folds them into one clean digest)

Sends a single HTML email digest via Gmail SMTP (App Password).

Designed to run daily via GitHub Actions cron. See README.md for setup.
"""

import os
import sys
import re
import json
import smtplib
import hashlib
import html
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone

import requests
import feedparser

# ---------------------------------------------------------------------------
# Configuration (all from environment / GitHub Secrets — nothing hardcoded)
# ---------------------------------------------------------------------------

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
DIGEST_TO_ADDRESS = os.environ.get("DIGEST_TO_ADDRESS", GMAIL_ADDRESS)

# Comma-separated keywords used to filter / score relevance.
# Example: "software engineer,solutions consultant,sales engineer,pre-sales"
KEYWORDS = [
    k.strip().lower()
    for k in os.environ.get(
        "JOB_KEYWORDS",
        "software engineer,solutions consultant,sales engineer,pre-sales,full stack,backend",
    ).split(",")
    if k.strip()
]

LOOKBACK_HOURS_MIN = int(os.environ.get("LOOKBACK_HOURS_MIN", "24"))
LOOKBACK_HOURS_MAX = int(os.environ.get("LOOKBACK_HOURS_MAX", "72"))

# Gmail label your alert emails get filed under (set up a Gmail filter to
# auto-label LinkedIn/Indeed/Glassdoor/Wellfound alerts into this label).
ALERT_LABEL_QUERY = os.environ.get(
    "ALERT_GMAIL_QUERY",
    '(from:jobalerts-noreply@linkedin.com OR from:alert@indeed.com OR '
    'from:noreply@glassdoor.com OR from:team@wellfound.com OR from:noreply@angel.co)',
)

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Source 1: Working Nomads (open public API, no auth required)
# ---------------------------------------------------------------------------

def fetch_working_nomads():
    """
    Working Nomads exposes a free public JSON endpoint. We fetch it and
    filter by created_at / pub_date within the lookback window.
    If the endpoint shape ever changes, fail soft (return []) rather than
    crashing the whole digest.
    """
    url = "https://www.workingnomads.com/api/exposed_jobs/"
    jobs = []
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "JobDigestBot/1.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[working_nomads] fetch failed, skipping source: {e}", file=sys.stderr)
        return jobs

    if not isinstance(data, list):
        print("[working_nomads] unexpected response shape, skipping source", file=sys.stderr)
        return jobs

    for item in data:
        try:
            title = item.get("title", "")
            company = item.get("company_name", "")
            url_ = item.get("url", "")
            # The live API returns tags as a comma-separated string, e.g. "devops,azure,aws"
            tags_raw = item.get("tags", "") or ""
            tags = [t.strip() for t in tags_raw.split(",")] if isinstance(tags_raw, str) else (tags_raw or [])
            # The live API returns pub_date like "2026-06-23T10:47:55-04:00" (ISO 8601 with numeric offset)
            pub_date_raw = item.get("pub_date") or item.get("created_at") or ""
            posted_dt = _parse_date_flexible(pub_date_raw)
            if posted_dt is None:
                continue
            hours_old = (NOW - posted_dt).total_seconds() / 3600
            if hours_old > LOOKBACK_HOURS_MAX:
                continue
            if hours_old < 0:
                continue

            text_blob = f"{title} {' '.join(tags)}".lower()
            if KEYWORDS and not any(k in text_blob for k in KEYWORDS):
                continue

            jobs.append({
                "source": "Working Nomads",
                "title": title,
                "company": company,
                "location": "Remote",
                "url": url_,
                "posted": posted_dt,
                "hours_old": round(hours_old, 1),
            })
        except Exception as e:
            print(f"[working_nomads] skipped one malformed item: {e}", file=sys.stderr)
            continue

    return jobs


# ---------------------------------------------------------------------------
# Source 2: EU Remote Jobs (RSS)
# ---------------------------------------------------------------------------

def fetch_eu_remote_jobs():
    """
    Source: JobsCollider RSS feeds (https://github.com/JobsCollider/remote-jobs-rss)
    Confirmed live, hourly-updated, no-auth-required RSS feeds. Default feed
    is Software Development; override EU_REMOTE_RSS_URL to point at a
    different category (see the JobsCollider README for the full list,
    e.g. remote-sales-jobs.rss, remote-business-jobs.rss).

    Per JobsCollider's usage terms: we attribute "via JobsCollider" in the
    digest and do not republish these jobs to any other third-party site.
    """
    feed_url = os.environ.get(
        "EU_REMOTE_RSS_URL", "https://jobscollider.com/remote-software-development-jobs.rss"
    )
    jobs = []
    try:
        feed = feedparser.parse(feed_url)
    except Exception as e:
        print(f"[jobscollider] fetch failed, skipping source: {e}", file=sys.stderr)
        return jobs

    if not feed.entries:
        print("[jobscollider] no entries returned, skipping source", file=sys.stderr)
        return jobs

    for entry in feed.entries:
        try:
            posted_dt = None
            if getattr(entry, "published_parsed", None):
                posted_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            if posted_dt is None:
                continue
            hours_old = (NOW - posted_dt).total_seconds() / 3600
            if hours_old > LOOKBACK_HOURS_MAX or hours_old < 0:
                continue

            title = html.unescape(entry.get("title", ""))
            text_blob = title.lower()
            if KEYWORDS and not any(k in text_blob for k in KEYWORDS):
                continue

            jobs.append({
                "source": "JobsCollider",
                "title": title,
                "company": "",
                "location": "Remote",
                "url": entry.get("link", ""),
                "posted": posted_dt,
                "hours_old": round(hours_old, 1),
            })
        except Exception as e:
            print(f"[jobscollider] skipped one malformed entry: {e}", file=sys.stderr)
            continue

    return jobs


# ---------------------------------------------------------------------------
# Source 3: Gmail alert emails (LinkedIn / Indeed / Glassdoor / Wellfound)
# ---------------------------------------------------------------------------
# NOTE: This requires the Gmail API (not SMTP) for reading, with its own
# OAuth credentials. To keep this script self-contained and runnable purely
# from GitHub Actions secrets, we use the Gmail API via a refresh token.
# See README.md "Gmail API read setup" for how to obtain these once.

def fetch_gmail_alert_jobs():
    refresh_token = os.environ.get("GMAIL_OAUTH_REFRESH_TOKEN")
    client_id = os.environ.get("GMAIL_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GMAIL_OAUTH_CLIENT_SECRET")

    if not (refresh_token and client_id and client_secret):
        print("[gmail_alerts] OAuth creds not configured, skipping source", file=sys.stderr)
        return []

    jobs = []
    try:
        token_resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]
    except Exception as e:
        print(f"[gmail_alerts] token refresh failed, skipping source: {e}", file=sys.stderr)
        return jobs

    headers = {"Authorization": f"Bearer {access_token}"}
    after_ts = int((NOW - timedelta(hours=LOOKBACK_HOURS_MAX)).timestamp())
    query = f"{ALERT_LABEL_QUERY} after:{after_ts}"

    try:
        list_resp = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers,
            params={"q": query, "maxResults": 50},
            timeout=20,
        )
        list_resp.raise_for_status()
        message_ids = [m["id"] for m in list_resp.json().get("messages", [])]
    except Exception as e:
        print(f"[gmail_alerts] message list failed, skipping source: {e}", file=sys.stderr)
        return jobs

    for msg_id in message_ids:
        try:
            msg_resp = requests.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
                headers=headers,
                params={"format": "full"},
                timeout=20,
            )
            msg_resp.raise_for_status()
            msg = msg_resp.json()
            jobs.extend(_extract_jobs_from_alert_email(msg))
        except Exception as e:
            print(f"[gmail_alerts] skipped one message: {e}", file=sys.stderr)
            continue

    return jobs


def _extract_jobs_from_alert_email(msg):
    """
    Heuristic extraction of job title / company / link from LinkedIn,
    Indeed, Glassdoor, and Wellfound alert email HTML. These templates
    change occasionally; this is intentionally tolerant and skips
    anything it can't confidently parse rather than guessing.
    """
    import base64

    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    sender = headers.get("from", "").lower()

    if "linkedin" in sender:
        source = "LinkedIn"
    elif "indeed" in sender:
        source = "Indeed"
    elif "glassdoor" in sender:
        source = "Glassdoor"
    elif "wellfound" in sender or "angel.co" in sender:
        source = "Wellfound"
    else:
        source = "Job Alert"

    html_body = _get_html_body(msg.get("payload", {}))
    if not html_body:
        return []

    # Generic pattern: anchor tags whose href contains a job-view-style path
    # and whose visible text looks like a job title.
    job_link_pattern = re.compile(
        r'<a[^>]+href="([^"]+)"[^>]*>\s*([^<]{4,120}?)\s*</a>', re.IGNORECASE
    )
    seen = set()
    jobs = []
    for href, text in job_link_pattern.findall(html_body):
        text_clean = html.unescape(re.sub(r"\s+", " ", text)).strip()
        if len(text_clean) < 6 or len(text_clean) > 120:
            continue
        if any(skip in text_clean.lower() for skip in
               ["unsubscribe", "view all", "see all", "manage", "settings", "privacy", "help center"]):
            continue
        key = (text_clean, href[:80])
        if key in seen:
            continue
        seen.add(key)
        jobs.append({
            "source": source,
            "title": text_clean,
            "company": "",
            "location": "Remote",
            "url": href,
            "posted": NOW,  # alert email arrival time stands in for posted time
            "hours_old": None,
        })

    return jobs[:25]  # cap per email to avoid noise from footer links


def _get_html_body(payload):
    import base64

    if payload.get("mimeType") == "text/html" and "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
    for part in payload.get("parts", []) or []:
        result = _get_html_body(part)
        if result:
            return result
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date_flexible(raw):
    if not raw:
        return None
    fmts = [
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def dedupe_jobs(jobs):
    seen_hashes = set()
    deduped = []
    for job in jobs:
        key = f"{job['title'].lower().strip()}|{job.get('company', '').lower().strip()}"
        h = hashlib.sha256(key.encode()).hexdigest()
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        deduped.append(job)
    return deduped


def build_html_digest(jobs_by_source, run_date):
    total = sum(len(v) for v in jobs_by_source.values())
    parts = [f"""
    <html><body style="font-family: -apple-system, Arial, sans-serif; color: #222; max-width: 700px; margin: 0 auto;">
    <h2 style="margin-bottom: 4px;">🧭 Remote Job Digest — {run_date}</h2>
    <p style="color: #666; margin-top: 0;">{total} new postings found in the last {LOOKBACK_HOURS_MAX}h, filtered to your keywords.</p>
    <hr style="border: none; border-top: 1px solid #ddd;">
    """]

    if total == 0:
        parts.append("<p>No new matching postings today. The script ran successfully — nothing met the filters.</p>")

    for source, jobs in jobs_by_source.items():
        if not jobs:
            continue
        parts.append(f'<h3 style="margin-top: 28px;">{html.escape(source)} ({len(jobs)})</h3>')
        for job in jobs:
            title = html.escape(job["title"])
            company = html.escape(job.get("company", "") or "")
            location = html.escape(job.get("location", "") or "")
            url_ = job.get("url", "#")
            age = f"{job['hours_old']}h ago" if job.get("hours_old") is not None else ""
            subtitle_bits = [b for b in [company, location, age] if b]
            subtitle = " · ".join(subtitle_bits)
            parts.append(f"""
            <div style="margin-bottom: 14px; padding-bottom: 10px; border-bottom: 1px solid #eee;">
              <a href="{url_}" style="font-size: 15px; font-weight: 600; color: #1a73e8; text-decoration: none;">{title}</a><br>
              <span style="font-size: 13px; color: #666;">{subtitle}</span>
            </div>
            """)

    parts.append(f"""
    <hr style="border: none; border-top: 1px solid #ddd; margin-top: 30px;">
    <p style="font-size: 11px; color: #999;">
      Sources: Working Nomads · Software-dev jobs via <a href="https://jobscollider.com" style="color: #999;">JobsCollider</a> ·
      LinkedIn / Indeed / Glassdoor / Wellfound alert emails (your own saved searches).
    </p>
    </body></html>""")
    return "".join(parts)


def send_digest_email(html_body, run_date):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        print("ERROR: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set, cannot send email.", file=sys.stderr)
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Remote Job Digest — {run_date}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = DIGEST_TO_ADDRESS
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [DIGEST_TO_ADDRESS], msg.as_string())

    print(f"Digest sent to {DIGEST_TO_ADDRESS}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    run_date = NOW.strftime("%Y-%m-%d")
    print(f"Running job digest for {run_date} (UTC now: {NOW.isoformat()})")

    wn_jobs = fetch_working_nomads()
    print(f"Working Nomads: {len(wn_jobs)} matching jobs")

    eu_jobs = fetch_eu_remote_jobs()
    print(f"JobsCollider (software-dev RSS): {len(eu_jobs)} matching jobs")

    alert_jobs = fetch_gmail_alert_jobs()
    print(f"Gmail alerts (LinkedIn/Indeed/Glassdoor/Wellfound): {len(alert_jobs)} matching jobs")

    jobs_by_source = {}
    for job in dedupe_jobs(wn_jobs + eu_jobs + alert_jobs):
        jobs_by_source.setdefault(job["source"], []).append(job)

    for source in jobs_by_source:
        jobs_by_source[source].sort(key=lambda j: j.get("hours_old") or 0)

    html_body = build_html_digest(jobs_by_source, run_date)
    send_digest_email(html_body, run_date)


if __name__ == "__main__":
    main()
