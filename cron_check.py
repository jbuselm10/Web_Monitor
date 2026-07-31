"""One-shot site checker for cron-based hosting (e.g. GreenGeeks shared hosting).

Reads urls.txt, checks every site once, and sends email + Telegram alerts,
then exits. Intended to be triggered on a schedule by cPanel Cron Jobs
instead of running as a long-lived process.

Usage:
    python cron_check.py [--notify-only-on-failure] [--label "Morning check"]
"""

import argparse
import html
import os
import smtplib
import ssl
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import certifi
import requests
from dotenv import load_dotenv

load_dotenv()

DEFAULT_TIMEOUT_SEC = 15
USER_AGENT = "WebsiteHealthAgent/1.0"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
MAX_URL_FILE_BYTES = 100_000
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_URL_LIST_FILE = os.path.join(_SCRIPT_DIR, "urls.txt")
NOTIFY_CHATS_FILE = os.path.join(_SCRIPT_DIR, "notify_chats.txt")
DEFAULT_EMAIL_RECIPIENTS_FILE = os.path.join(_SCRIPT_DIR, "email_recipients.txt")


# =====================================================================
# URL checking
# =====================================================================

def normalize_url(url: str) -> str:
    url = url.strip().rstrip(".,;)")
    if not url:
        raise ValueError("URL is empty")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError(f"Invalid URL: {url}")
    return url


def check_url(url: str) -> tuple[str, bool]:
    """Fetch a URL and return (status_label, is_working)."""
    try:
        target = normalize_url(url)
    except ValueError as exc:
        return f"NOT WORKING (invalid URL: {exc})", False

    request = Request(
        target,
        method="GET",
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*"},
    )

    start = time.perf_counter()
    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SEC, context=SSL_CONTEXT) as response:
            elapsed_ms = (time.perf_counter() - start) * 1000
            status = getattr(response, "status", None) or response.getcode()
            if 200 <= status < 400:
                return f"WORKING (HTTP {status}, {elapsed_ms:.0f} ms)", True
            return f"NOT WORKING (HTTP {status})", False
    except HTTPError as exc:
        return f"NOT WORKING (HTTP {exc.code})", False
    except ssl.SSLError as exc:
        return f"NOT WORKING (SSL error: {exc})", False
    except URLError as exc:
        return f"NOT WORKING (could not reach: {getattr(exc, 'reason', exc)})", False
    except TimeoutError:
        return f"NOT WORKING (timed out after {DEFAULT_TIMEOUT_SEC}s)", False
    except Exception as exc:
        return f"NOT WORKING ({type(exc).__name__}: {exc})", False


def parse_urls_from_file(path: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line not in seen:
                seen.add(line)
                urls.append(line)
    return urls


def check_urls_batch(urls: list[str]) -> list[tuple[str, str, bool]]:
    results: list[tuple[str, str, bool]] = []
    for url in urls:
        label, working = check_url(url)
        results.append((url, label, working))
    return results


# =====================================================================
# Formatting
# =====================================================================

def format_datetime_display(dt: datetime) -> str:
    date_part = dt.strftime("%m/%d/%Y")
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{date_part} {hour}:{dt.strftime('%M')} {dt.strftime('%p')}"


def format_status_alert_email(
    results: list[tuple[str, str, bool]], completed_at: datetime
) -> tuple[str, str]:
    failed = [(url, label) for url, label, ok in results if not ok]
    working = [(url, label) for url, label, ok in results if ok]
    ts = format_datetime_display(completed_at)

    plain_lines = [f"BuselWorks Daily Site Status — {ts}", "", "NOT WORKING:"]
    plain_lines += [f"  - {url} ({label})" for url, label in failed] or ["  - None (all sites passed)"]
    plain_lines += ["", "WORKING:"]
    plain_lines += [f"  - {url} ({label})" for url, label in working]
    plain_body = "\n".join(plain_lines)

    failed_items = "".join(
        f"<li style='color:#cc0000'><strong>{html.escape(url)}</strong> — {html.escape(label)}</li>"
        for url, label in failed
    ) or "<li style='color:#666'>None — all sites passed this check</li>"
    working_items = "".join(
        f"<li style='color:#008800'><strong>{html.escape(url)}</strong> — {html.escape(label)}</li>"
        for url, label in working
    ) or "<li style='color:#666'>None</li>"

    html_body = (
        "<!DOCTYPE html><html><body style='font-family:Arial,sans-serif'>"
        f"<p><strong>BuselWorks Daily Site Status</strong><br>Checked at: {html.escape(ts)}</p>"
        "<h2 style='color:#cc0000;margin-bottom:0.25em'>Not Working</h2>"
        f"<ul style='margin-top:0'>{failed_items}</ul>"
        "<h2 style='color:#008800;margin-bottom:0.25em'>Working</h2>"
        f"<ul style='margin-top:0'>{working_items}</ul>"
        "</body></html>"
    )
    return html_body, plain_body


def format_failure_alert_telegram(
    results: list[tuple[str, str, bool]], completed_at: datetime
) -> str | None:
    failed = [(url, label) for url, label, ok in results if not ok]
    if not failed:
        return None
    ts = format_datetime_display(completed_at)
    lines = ["🔴 <b>Alert: site not working</b>", f"Checked at: <code>{html.escape(ts)}</code>", ""]
    lines += [f"🔴 <code>{html.escape(url)}</code> — <b>{html.escape(label)}</b>" for url, label in failed]
    return "\n".join(lines)


# =====================================================================
# Email
# =====================================================================

def parse_recipient_email(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "@" not in line:
        return None
    return line.split("#")[0].strip()


def load_alert_email_recipients(path: str) -> list[str]:
    if not os.path.isfile(path):
        return []
    emails: list[str] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            addr = parse_recipient_email(line)
            if addr and addr.lower() not in seen:
                seen.add(addr.lower())
                emails.append(addr)
    return emails


def send_alert_email(to_addr: str, subject: str, html_body: str, plain_body: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ.get("SMTP_PASSWORD", "").replace(" ", "")
    from_addr = os.environ.get("SMTP_FROM", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(user, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())
    print(f"[Email] Sent alert to {to_addr}")


def email_alerts_configured() -> bool:
    enabled = os.environ.get("EMAIL_ALERTS_ENABLED", "false").lower() in ("1", "true", "yes")
    has_smtp = all(os.environ.get(k, "").strip() for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))
    return enabled and has_smtp


def send_status_emails(html_body: str, plain_body: str) -> None:
    recipients_path = os.environ.get(
        "ALERT_RECIPIENTS_FILE",
        os.environ.get("EMAIL_RECIPIENTS_FILE", DEFAULT_EMAIL_RECIPIENTS_FILE),
    )
    recipients = load_alert_email_recipients(recipients_path)
    if not recipients:
        print(f"[Email] No addresses in {recipients_path}")
        return
    if not email_alerts_configured():
        print("[Email] Alerts not fully configured (EMAIL_ALERTS_ENABLED / SMTP_* in .env) — skipping")
        return
    subject = os.environ.get("ALERT_EMAIL_SUBJECT", "BuselWorks Daily Site Status Message")
    for addr in recipients:
        try:
            send_alert_email(addr, subject, html_body, plain_body)
        except Exception as exc:
            print(f"[Email] Failed for {addr}: {exc}")


# =====================================================================
# Telegram (plain HTTP, no bot polling needed)
# =====================================================================

def load_notify_chat_ids() -> set[int]:
    ids: set[int] = set()
    env_id = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID", "").strip()
    if env_id:
        ids.add(int(env_id))
    if os.path.isfile(NOTIFY_CHATS_FILE):
        with open(NOTIFY_CHATS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.isdigit():
                    ids.add(int(line))
    return ids


def send_telegram_message(token: str, chat_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        data={"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML"},
        timeout=15,
    )
    resp.raise_for_status()


def send_telegram_alerts(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("[Telegram] TELEGRAM_BOT_TOKEN not set — skipping")
        return
    chat_ids = load_notify_chat_ids()
    if not chat_ids:
        print("[Telegram] No chat IDs in notify_chats.txt — skipping")
        return
    for chat_id in chat_ids:
        try:
            send_telegram_message(token, chat_id, text)
            print(f"[Telegram] Sent alert to chat {chat_id}")
        except Exception as exc:
            print(f"[Telegram] Could not message chat {chat_id}: {exc}")


# =====================================================================
# Main
# =====================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Check all sites once and send alerts.")
    parser.add_argument(
        "--notify-only-on-failure",
        action="store_true",
        help="Only send email/Telegram if at least one site is not working.",
    )
    parser.add_argument("--label", default="Scheduled check", help="Label used in log output.")
    args = parser.parse_args()

    url_list_path = os.environ.get("URL_LIST_FILE", DEFAULT_URL_LIST_FILE)
    if not os.path.isfile(url_list_path):
        print(f"[{args.label}] URL list file not found: {url_list_path}")
        return 1
    if os.path.getsize(url_list_path) > MAX_URL_FILE_BYTES:
        print(f"[{args.label}] URL list file too large (max {MAX_URL_FILE_BYTES // 1000} KB)")
        return 1

    urls = parse_urls_from_file(url_list_path)
    if not urls:
        print(f"[{args.label}] No URLs found in {url_list_path}")
        return 1

    print(f"[{args.label}] Checking {len(urls)} URL(s)...")
    results = check_urls_batch(urls)
    completed_at = datetime.now()

    working_count = sum(1 for _, _, ok in results if ok)
    print(f"[{args.label}] Complete — {working_count}/{len(results)} WORKING")
    for url, label, ok in results:
        print(f"  {'WORKING' if ok else 'NOT WORKING':12s} {url} ({label})")

    has_failure = any(not ok for _, _, ok in results)
    if args.notify_only_on_failure and not has_failure:
        print(f"[{args.label}] All sites working — no email or Telegram sent.")
        return 0

    html_body, plain_body = format_status_alert_email(results, completed_at)
    send_status_emails(html_body, plain_body)

    telegram_text = format_failure_alert_telegram(results, completed_at)
    if telegram_text:
        send_telegram_alerts(telegram_text)
    else:
        print(f"[{args.label}] All sites working — no Telegram alert needed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
