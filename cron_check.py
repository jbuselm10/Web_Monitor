"""One-shot site checker for cron-based hosting (e.g. GreenGeeks shared hosting).

Reads urls.txt, checks every site once, and sends email + Telegram alerts,
then exits. Intended to be triggered on a schedule by cPanel Cron Jobs
instead of running as a long-lived process.

Uses only the Python standard library, so it needs no virtualenv and no pip
install — cron can call the system python3 directly.

Compatible with Python 3.6+.

Usage:
    python3 cron_check.py [--notify-only-on-failure] [--label "Morning check"]
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
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

DEFAULT_TIMEOUT_SEC = 15
USER_AGENT = "WebsiteHealthAgent/1.0"
MAX_URL_FILE_BYTES = 100_000
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_URL_LIST_FILE = os.path.join(_SCRIPT_DIR, "urls.txt")
NOTIFY_CHATS_FILE = os.path.join(_SCRIPT_DIR, "notify_chats.txt")
DEFAULT_EMAIL_RECIPIENTS_FILE = os.path.join(_SCRIPT_DIR, "email_recipients.txt")
DEFAULT_ENV_FILE = os.path.join(_SCRIPT_DIR, ".env")


def load_env_file(path):
    """Minimal .env loader. Already-set environment variables take precedence,
    matching python-dotenv's default behaviour. Inline comments are not
    stripped, so values may contain '#'."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


def build_ssl_context():
    """Prefer certifi's CA bundle when installed, else the system trust store."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


load_env_file(os.environ.get("ENV_FILE", DEFAULT_ENV_FILE))
SSL_CONTEXT = build_ssl_context()


# =====================================================================
# URL checking
# =====================================================================

def normalize_url(url):
    url = url.strip().rstrip(".,;)")
    if not url:
        raise ValueError("URL is empty")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError("Invalid URL: {0}".format(url))
    return url


def check_url(url):
    """Fetch a URL and return (status_label, is_working)."""
    try:
        target = normalize_url(url)
    except ValueError as exc:
        return "NOT WORKING (invalid URL: {0})".format(exc), False

    request = Request(
        target,
        data=None,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*"},
    )
    request.get_method = lambda: "GET"

    start = time.perf_counter()
    try:
        response = urlopen(request, timeout=DEFAULT_TIMEOUT_SEC, context=SSL_CONTEXT)
        try:
            elapsed_ms = (time.perf_counter() - start) * 1000
            status = getattr(response, "status", None) or response.getcode()
            if 200 <= status < 400:
                return "WORKING (HTTP {0}, {1:.0f} ms)".format(status, elapsed_ms), True
            return "NOT WORKING (HTTP {0})".format(status), False
        finally:
            response.close()
    except HTTPError as exc:
        return "NOT WORKING (HTTP {0})".format(exc.code), False
    except ssl.SSLError as exc:
        return "NOT WORKING (SSL error: {0})".format(exc), False
    except URLError as exc:
        return "NOT WORKING (could not reach: {0})".format(getattr(exc, "reason", exc)), False
    except Exception as exc:
        # TimeoutError is a subclass of OSError on 3.x; catch broadly for old hosts.
        if type(exc).__name__ == "TimeoutError" or isinstance(exc, socket_timeout_types()):
            return "NOT WORKING (timed out after {0}s)".format(DEFAULT_TIMEOUT_SEC), False
        return "NOT WORKING ({0}: {1})".format(type(exc).__name__, exc), False


def socket_timeout_types():
    types = []
    try:
        import socket
        types.append(socket.timeout)
    except Exception:
        pass
    try:
        types.append(TimeoutError)
    except NameError:
        pass
    return tuple(types) if types else ()


def parse_urls_from_file(path):
    urls = []
    seen = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line not in seen:
                seen.add(line)
                urls.append(line)
    return urls


def check_urls_batch(urls):
    results = []
    for url in urls:
        label, working = check_url(url)
        results.append((url, label, working))
    return results


# =====================================================================
# Formatting
# =====================================================================

def format_datetime_display(dt):
    date_part = dt.strftime("%m/%d/%Y")
    hour = dt.strftime("%I").lstrip("0") or "12"
    return "{0} {1}:{2} {3}".format(date_part, hour, dt.strftime("%M"), dt.strftime("%p"))


def format_status_alert_email(results, completed_at):
    failed = [(url, label) for url, label, ok in results if not ok]
    working = [(url, label) for url, label, ok in results if ok]
    ts = format_datetime_display(completed_at)

    plain_lines = ["BuselWorks Daily Site Status — {0}".format(ts), "", "NOT WORKING:"]
    plain_lines += ["  - {0} ({1})".format(url, label) for url, label in failed] or [
        "  - None (all sites passed)"
    ]
    plain_lines += ["", "WORKING:"]
    plain_lines += ["  - {0} ({1})".format(url, label) for url, label in working]
    plain_body = "\n".join(plain_lines)

    failed_items = "".join(
        "<li style='color:#cc0000'><strong>{0}</strong> — {1}</li>".format(
            html.escape(url), html.escape(label)
        )
        for url, label in failed
    ) or "<li style='color:#666'>None — all sites passed this check</li>"
    working_items = "".join(
        "<li style='color:#008800'><strong>{0}</strong> — {1}</li>".format(
            html.escape(url), html.escape(label)
        )
        for url, label in working
    ) or "<li style='color:#666'>None</li>"

    html_body = (
        "<!DOCTYPE html><html><body style='font-family:Arial,sans-serif'>"
        "<p><strong>BuselWorks Daily Site Status</strong><br>Checked at: {0}</p>"
        "<h2 style='color:#cc0000;margin-bottom:0.25em'>Not Working</h2>"
        "<ul style='margin-top:0'>{1}</ul>"
        "<h2 style='color:#008800;margin-bottom:0.25em'>Working</h2>"
        "<ul style='margin-top:0'>{2}</ul>"
        "</body></html>"
    ).format(html.escape(ts), failed_items, working_items)
    return html_body, plain_body


def format_failure_alert_telegram(results, completed_at):
    failed = [(url, label) for url, label, ok in results if not ok]
    if not failed:
        return None
    ts = format_datetime_display(completed_at)
    lines = [
        "🔴 <b>Alert: site not working</b>",
        "Checked at: <code>{0}</code>".format(html.escape(ts)),
        "",
    ]
    lines += [
        "🔴 <code>{0}</code> — <b>{1}</b>".format(html.escape(url), html.escape(label))
        for url, label in failed
    ]
    return "\n".join(lines)


# =====================================================================
# Email
# =====================================================================

def parse_recipient_email(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "@" not in line:
        return None
    return line.split("#")[0].strip()


def load_alert_email_recipients(path):
    if not os.path.isfile(path):
        return []
    emails = []
    seen = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            addr = parse_recipient_email(line)
            if addr and addr.lower() not in seen:
                seen.add(addr.lower())
                emails.append(addr)
    return emails


def send_alert_email(to_addr, subject, html_body, plain_body):
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

    server = smtplib.SMTP(host, port, timeout=30)
    try:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(user, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass
    print("[Email] Sent alert to {0}".format(to_addr))


def email_alerts_configured():
    enabled = os.environ.get("EMAIL_ALERTS_ENABLED", "false").lower() in ("1", "true", "yes")
    has_smtp = all(os.environ.get(k, "").strip() for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))
    return enabled and has_smtp


def send_status_emails(html_body, plain_body):
    recipients_path = os.environ.get(
        "ALERT_RECIPIENTS_FILE",
        os.environ.get("EMAIL_RECIPIENTS_FILE", DEFAULT_EMAIL_RECIPIENTS_FILE),
    )
    recipients = load_alert_email_recipients(recipients_path)
    if not recipients:
        print("[Email] No addresses in {0}".format(recipients_path))
        return
    if not email_alerts_configured():
        print("[Email] Alerts not fully configured (EMAIL_ALERTS_ENABLED / SMTP_* in .env) — skipping")
        return
    subject = os.environ.get("ALERT_EMAIL_SUBJECT", "BuselWorks Daily Site Status Message")
    for addr in recipients:
        try:
            send_alert_email(addr, subject, html_body, plain_body)
        except Exception as exc:
            print("[Email] Failed for {0}: {1}".format(addr, exc))


# =====================================================================
# Telegram (plain HTTP, no bot polling needed)
# =====================================================================

def load_notify_chat_ids():
    ids = set()
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


def send_telegram_message(token, chat_id, text):
    url = "https://api.telegram.org/bot{0}/sendMessage".format(token)
    payload = urlencode(
        {"chat_id": str(chat_id), "text": text[:4000], "parse_mode": "HTML"}
    ).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
    )
    response = urlopen(request, timeout=15, context=SSL_CONTEXT)
    try:
        status = getattr(response, "status", None) or response.getcode()
        if status != 200:
            raise RuntimeError("Telegram API returned HTTP {0}".format(status))
    finally:
        response.close()


def send_telegram_alerts(text):
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
            print("[Telegram] Sent alert to chat {0}".format(chat_id))
        except Exception as exc:
            print("[Telegram] Could not message chat {0}: {1}".format(chat_id, exc))


# =====================================================================
# Main
# =====================================================================

def main():
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
        print("[{0}] URL list file not found: {1}".format(args.label, url_list_path))
        return 1
    if os.path.getsize(url_list_path) > MAX_URL_FILE_BYTES:
        print("[{0}] URL list file too large (max {1} KB)".format(
            args.label, MAX_URL_FILE_BYTES // 1000
        ))
        return 1

    urls = parse_urls_from_file(url_list_path)
    if not urls:
        print("[{0}] No URLs found in {1}".format(args.label, url_list_path))
        return 1

    print("[{0}] Checking {1} URL(s)...".format(args.label, len(urls)))
    results = check_urls_batch(urls)
    completed_at = datetime.now()

    working_count = sum(1 for _, _, ok in results if ok)
    print("[{0}] Complete — {1}/{2} WORKING".format(
        args.label, working_count, len(results)
    ))
    for url, label, ok in results:
        status = "WORKING" if ok else "NOT WORKING"
        print("  {0:12s} {1} ({2})".format(status, url, label))

    has_failure = any(not ok for _, _, ok in results)
    if args.notify_only_on_failure and not has_failure:
        print("[{0}] All sites working — no email or Telegram sent.".format(args.label))
        return 0

    html_body, plain_body = format_status_alert_email(results, completed_at)
    send_status_emails(html_body, plain_body)

    telegram_text = format_failure_alert_telegram(results, completed_at)
    if telegram_text:
        send_telegram_alerts(telegram_text)
    else:
        print("[{0}] All sites working — no Telegram alert needed.".format(args.label))

    return 0


if __name__ == "__main__":
    sys.exit(main())
