import html
import os
import re
import sys

from dotenv import load_dotenv

load_dotenv()
import ssl
import smtplib
import time
import asyncio
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import certifi
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Terminal ANSI colors (green = working, red = not working)
_TERM_GREEN = "\033[92m"
_TERM_RED = "\033[91m"
_TERM_RESET = "\033[0m"


def _enable_windows_ansi() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        ctypes.windll.kernel32.SetConsoleMode(handle, 7)
    except Exception:
        pass


_enable_windows_ansi()


def _status_from_result(result: str) -> tuple[str, bool]:
    if "verdict=WORKING" in result:
        return "WORKING", True
    if "FAIL" in result:
        return "NOT WORKING", False
    return "NOT WORKING", False


def _color_terminal(text: str, working: bool) -> str:
    color = _TERM_GREEN if working else _TERM_RED
    return f"{color}{text}{_TERM_RESET}"

# =====================================================================
# STEP 1: LOCAL TOOLS
# =====================================================================

DEFAULT_TIMEOUT_SEC = 15
USER_AGENT = "WebsiteHealthAgent/1.0"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
MAX_BATCH_URLS = 25
MAX_URL_FILE_BYTES = 100_000
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_URL_LIST_FILE = os.path.join(_SCRIPT_DIR, "urls.txt")
NOTIFY_CHATS_FILE = os.path.join(_SCRIPT_DIR, "notify_chats.txt")
DEFAULT_EMAIL_RECIPIENTS_FILE = os.path.join(_SCRIPT_DIR, "email_recipients.txt")
DEFAULT_AUTO_CHECK_INTERVAL_HOURS = 6
DEFAULT_AUTO_CHECK_MINUTE = 30
DEFAULT_NOON_CHECK_HOUR = 12
DEFAULT_NOON_CHECK_MINUTE = 0
DEFAULT_MORNING_CHECK_HOUR = 6
DEFAULT_MORNING_CHECK_MINUTE = 45


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


def log_site_status(url: str, result: str) -> None:
    """Print a clear WORKING / NOT WORKING line to the terminal."""
    label, working = _status_from_result(result)
    if not working and "verdict=" in result and "FAIL" not in result:
        label = "NOT WORKING (site responded with errors)"
    colored = _color_terminal(label, working)
    print(f"\n[Site check] {url} -> {colored}")
    status_match = re.search(r"http_status=(\d+)", result)
    if status_match:
        print(f"             HTTP {status_match.group(1)}")
    time_match = re.search(r"response_time_ms=([\d.]+)", result)
    if time_match:
        print(f"             {time_match.group(1)} ms")


def check_url(url: str, *, quiet: bool = False) -> str:
    """Fetches a URL and reports whether the site appears to be working."""

    def finish(result: str) -> str:
        if not quiet:
            log_site_status(url, result)
        return result

    try:
        target = normalize_url(url)
    except ValueError as exc:
        return finish(f"FAIL — invalid URL: {exc}")

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
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "unknown")
            content_length = response.headers.get("Content-Length", "unknown")

            if 200 <= status < 400:
                verdict = "WORKING"
            elif 400 <= status < 500:
                verdict = "REACHABLE but client error (site responded)"
            else:
                verdict = "REACHABLE but server error"

            lines = [
                f"verdict={verdict}",
                f"requested_url={target}",
                f"final_url={final_url}",
                f"http_status={status}",
                f"response_time_ms={elapsed_ms:.0f}",
                f"content_type={content_type}",
                f"content_length={content_length}",
            ]
            if final_url != target:
                lines.append("note=Followed redirects")
            return finish(" | ".join(lines))

    except HTTPError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        if 400 <= exc.code < 500:
            verdict = "REACHABLE but client error"
        else:
            verdict = "REACHABLE but server error"
        return finish(
            f"verdict={verdict} | requested_url={target} | http_status={exc.code} "
            f"| response_time_ms={elapsed_ms:.0f} | reason={exc.reason}"
        )
    except ssl.SSLError as exc:
        return finish(f"FAIL — SSL error for {target}: {exc}")
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        return finish(f"FAIL — could not reach {target}: {reason}")
    except TimeoutError:
        return finish(f"FAIL — timed out after {DEFAULT_TIMEOUT_SEC}s: {target}")
    except Exception as exc:
        return finish(f"FAIL — unexpected error for {target}: {type(exc).__name__}: {exc}")


# =====================================================================
# STEP 2: PARSING
# =====================================================================

def extract_urls(text: str) -> list[str]:
    pattern = r"https?://[^\s<>\"']+|(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}(?:/[^\s]*)?"
    found = re.findall(pattern, text)
    return list(dict.fromkeys(found))


def parse_urls_from_file_content(content: str) -> list[str]:
    """One or more URLs per line; # lines are comments."""
    urls: list[str] = []
    seen: set[str] = set()
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for url in extract_urls(line):
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def check_urls_batch(urls: list[str]) -> list[tuple[str, str, bool]]:
    """Check each URL; returns (url, status_label, is_working)."""
    results: list[tuple[str, str, bool]] = []
    for url in urls:
        raw = check_url(url, quiet=True)
        label, working = _status_from_result(raw)
        if not working and "verdict=" in raw and "FAIL" not in raw:
            label = "NOT WORKING"
        results.append((url, label, working))
    return results


def get_url_list_path() -> str:
    return os.environ.get("URL_LIST_FILE", DEFAULT_URL_LIST_FILE)


def is_checkall_command(text: str) -> bool:
    """True for checkall / check all / check-all (any case)."""
    return text.strip().lower().replace("-", " ") in ("checkall", "check all")


def resolve_single_url(text: str) -> str | None:
    """Return one URL from user text, or None if ambiguous / invalid."""
    urls = extract_urls(text)
    if len(urls) > 1:
        return None
    if len(urls) == 1:
        return urls[0]
    try:
        return normalize_url(text)
    except ValueError:
        return None


def load_urls_from_local_file() -> tuple[list[str] | None, str | None]:
    """Read URLs from the local list file. Returns (urls, error_message)."""
    path = get_url_list_path()
    if not os.path.isfile(path):
        return None, f"URL list file not found:\n{path}"
    if os.path.getsize(path) > MAX_URL_FILE_BYTES:
        return None, f"URL list file too large (max {MAX_URL_FILE_BYTES // 1000} KB)."
    with open(path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    urls = parse_urls_from_file_content(content)
    if not urls:
        return None, (
            f"No URLs in file:\n{path}\n\n"
            "Add one URL per line (# for comments)."
        )
    if len(urls) > MAX_BATCH_URLS:
        return None, (
            f"Found {len(urls)} URLs in file (max {MAX_BATCH_URLS}). "
            "Remove some or raise MAX_BATCH_URLS in code."
        )
    return urls, None


def auto_check_enabled() -> bool:
    return os.environ.get("AUTO_CHECK_ENABLED", "true").lower() in ("1", "true", "yes")


def auto_check_interval_hours() -> int:
    raw = os.environ.get(
        "AUTO_CHECK_INTERVAL_HOURS", str(DEFAULT_AUTO_CHECK_INTERVAL_HOURS)
    )
    return max(1, min(24, int(raw)))


def auto_check_minute() -> int:
    raw = os.environ.get("AUTO_CHECK_MINUTE", str(DEFAULT_AUTO_CHECK_MINUTE))
    return max(0, min(59, int(raw)))


def get_auto_check_times() -> list[tuple[int, int]]:
    """Clock times (hour, minute) for recurring auto checkall, e.g. every 6 h at :30."""
    minute = auto_check_minute()
    return [(hour, minute) for hour in range(0, 24, auto_check_interval_hours())]


def seconds_until_next_auto_check() -> tuple[float, datetime]:
    now = datetime.now()
    best_secs = None
    best_target = now
    for hour, minute in get_auto_check_times():
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        secs = (target - now).total_seconds()
        if best_secs is None or secs < best_secs:
            best_secs = secs
            best_target = target
    return best_secs or 86400.0, best_target


def format_auto_check_schedule_line() -> str:
    interval_h = auto_check_interval_hours()
    minute = auto_check_minute()
    times = [format_time_12h(h, minute) for h in range(0, 24, interval_h)]
    return (
        f"every {interval_h} h at :{minute:02d} "
        f"({', '.join(times)}) — email if NOT WORKING"
    )


def _env_flag(name: str, default: str = "true") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


def morning_check_enabled() -> bool:
    return _env_flag("MORNING_CHECK_ENABLED", "true")


def noon_check_enabled() -> bool:
    return _env_flag("NOON_CHECK_ENABLED", "true")


def scheduled_checks_enabled() -> bool:
    """Set ENABLE_SCHEDULED_CHECKS=false when scheduled checks run elsewhere
    (e.g. a GreenGeeks cron job calling cron_check.py), so this local bot only
    handles on-demand messages and doesn't send duplicate alerts."""
    return _env_flag("ENABLE_SCHEDULED_CHECKS", "true")


def morning_check_time() -> tuple[int, int]:
    hour = int(os.environ.get("MORNING_CHECK_HOUR", str(DEFAULT_MORNING_CHECK_HOUR)))
    minute = int(
        os.environ.get("MORNING_CHECK_MINUTE", str(DEFAULT_MORNING_CHECK_MINUTE))
    )
    return hour % 24, max(0, min(59, minute))


def noon_check_time() -> tuple[int, int]:
    hour = int(os.environ.get("NOON_CHECK_HOUR", str(DEFAULT_NOON_CHECK_HOUR)))
    minute = int(os.environ.get("NOON_CHECK_MINUTE", str(DEFAULT_NOON_CHECK_MINUTE)))
    return hour % 24, max(0, min(59, minute))


def format_time_12h(hour: int, minute: int) -> str:
    hour_12 = hour % 12 or 12
    am_pm = "AM" if hour < 12 else "PM"
    return f"{hour_12}:{minute:02d} {am_pm}"


def get_daily_alert_schedules() -> list[tuple[int, int, str, bool]]:
    """(hour, minute, label, notify_only_on_failure) for each daily run."""
    schedules: list[tuple[int, int, str, bool]] = []
    if morning_check_enabled():
        h, m = morning_check_time()
        # Morning: always send status email
        schedules.append((h, m, "Morning check", False))
    if noon_check_enabled():
        h, m = noon_check_time()
        schedules.append((h, m, "Noon check", True))
    return schedules


def seconds_until_next_daily_alert() -> tuple[float, str, datetime, bool]:
    """Seconds until next run, label, datetime, and notify_only_on_failure flag."""
    now = datetime.now()
    best_secs = None
    best_label = ""
    best_target = now
    best_notify_only = True
    for hour, minute, label, notify_only in get_daily_alert_schedules():
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        secs = (target - now).total_seconds()
        if best_secs is None or secs < best_secs:
            best_secs = secs
            best_label = label
            best_target = target
            best_notify_only = notify_only
    return best_secs or 86400.0, best_label, best_target, best_notify_only


def format_daily_alert_schedule_line() -> str:
    parts = []
    for hour, minute, label, notify_only in get_daily_alert_schedules():
        time_str = format_time_12h(hour, minute)
        if label == "Morning check" and not notify_only:
            parts.append(f"{time_str} (always email)")
        else:
            parts.append(f"{time_str} (if NOT WORKING)")
    return ", ".join(parts) if parts else "none"


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


def register_notify_chat(update: Update) -> None:
    """Remember this chat for scheduled checkall Telegram alerts."""
    chat_id = update.effective_chat.id
    ids = load_notify_chat_ids()
    if chat_id in ids:
        return
    ids.add(chat_id)
    with open(NOTIFY_CHATS_FILE, "w", encoding="utf-8") as f:
        for cid in sorted(ids):
            f.write(f"{cid}\n")
    print(f"[Notify] Registered chat {chat_id} for auto-check alerts")


def format_datetime_display(dt: datetime) -> str:
    """MM/DD/YYYY and 12-hour time with AM/PM (e.g. 06/03/2026 4:30 PM)."""
    date_part = dt.strftime("%m/%d/%Y")
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{date_part} {hour}:{dt.strftime('%M')} {dt.strftime('%p')}"


def log_batch_summary(
    results: list[tuple[str, str, bool]], completed_at: datetime
) -> None:
    """Terminal summary after entire batch finishes."""
    ts = format_datetime_display(completed_at)
    working_count = sum(1 for _, _, ok in results if ok)
    print(f"\n[Batch check] Complete at {ts} — {working_count}/{len(results)} WORKING")
    for url, label, working in results:
        print(f"  {_color_terminal(label, working)}  {url}")


def format_batch_for_telegram(
    results: list[tuple[str, str, bool]], completed_at: datetime
) -> str:
    ts = format_datetime_display(completed_at)
    working_count = sum(1 for _, _, ok in results if ok)
    lines = [
        "<b>Batch check complete</b>",
        f"Checked at: <code>{html.escape(ts)}</code>",
        "",
        f"<b>Sites ({working_count}/{len(results)} working):</b>",
    ]
    for url, label, working in results:
        marker = "🟢" if working else "🔴"
        lines.append(f"{marker} <code>{html.escape(url)}</code> — <b>{html.escape(label)}</b>")
    return "\n".join(lines)


def format_failure_alert(
    results: list[tuple[str, str, bool]], completed_at: datetime
) -> str | None:
    """Telegram message when one or more sites are not working."""
    failed = [(url, label) for url, label, ok in results if not ok]
    if not failed:
        return None
    ts = format_datetime_display(completed_at)
    lines = [
        "🔴 <b>Alert: site not working</b>",
        f"Checked at: <code>{html.escape(ts)}</code>",
        "",
    ]
    for url, label in failed:
        lines.append(f"🔴 <code>{html.escape(url)}</code> — <b>{html.escape(label)}</b>")
    return "\n".join(lines)


def format_status_alert_email(
    results: list[tuple[str, str, bool]], completed_at: datetime
) -> tuple[str, str] | None:
    """HTML + plain email body: Not Working (red) first, then Working (green)."""
    if not results:
        return None
    failed = [(url, label) for url, label, ok in results if not ok]
    working = [(url, label) for url, label, ok in results if ok]
    ts = format_datetime_display(completed_at)

    plain_lines = [
        f"BuselWorks Daily Site Status — {ts}",
        "",
        "NOT WORKING:",
    ]
    if failed:
        for url, label in failed:
            plain_lines.append(f"  - {url} ({label})")
    else:
        plain_lines.append("  - None (all sites passed)")
    plain_lines.extend(["", "WORKING:"])
    for url, label in working:
        plain_lines.append(f"  - {url} ({label})")
    plain_body = "\n".join(plain_lines)

    failed_items = (
        "".join(
            f"<li style='color:#cc0000'><strong>{html.escape(url)}</strong> — "
            f"{html.escape(label)}</li>"
            for url, label in failed
        )
        or "<li style='color:#666'>None — all sites passed this check</li>"
    )
    working_items = "".join(
        f"<li style='color:#008800'><strong>{html.escape(url)}</strong> — "
        f"{html.escape(label)}</li>"
        for url, label in working
    ) or "<li style='color:#666'>None</li>"

    html_body = (
        "<!DOCTYPE html><html><body style='font-family:Arial,sans-serif'>"
        f"<p><strong>BuselWorks Daily Site Status</strong><br>"
        f"Checked at: {html.escape(ts)}</p>"
        "<h2 style='color:#cc0000;margin-bottom:0.25em'>Not Working</h2>"
        f"<ul style='margin-top:0'>{failed_items}</ul>"
        "<h2 style='color:#008800;margin-bottom:0.25em'>Working</h2>"
        f"<ul style='margin-top:0'>{working_items}</ul>"
        "</body></html>"
    )
    return html_body, plain_body


def get_recipients_file_path() -> str:
    return os.environ.get(
        "ALERT_RECIPIENTS_FILE",
        os.environ.get("EMAIL_RECIPIENTS_FILE", DEFAULT_EMAIL_RECIPIENTS_FILE),
    )


def parse_recipient_email(line: str) -> str | None:
    """One email address per line in email_recipients.txt."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "@" not in line:
        print(f"[Email] Skipping invalid line: {line!r} (use you@example.com)")
        return None
    return line.split("#")[0].strip()


def load_alert_email_recipients() -> list[str]:
    path = get_recipients_file_path()
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


def email_alerts_enabled() -> bool:
    raw = os.environ.get("EMAIL_ALERTS_ENABLED", "false")
    return raw.lower() in ("1", "true", "yes")


def smtp_missing_vars() -> list[str]:
    missing = []
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        if not os.environ.get(key, "").strip():
            missing.append(key)
    return missing


def smtp_configured() -> bool:
    return not smtp_missing_vars()


def email_alerts_configured() -> bool:
    return email_alerts_enabled() and smtp_configured()


def send_alert_email(
    to_addr: str, subject: str, html_body: str, plain_body: str
) -> None:
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


def send_status_emails(html_body: str, plain_body: str) -> None:
    """Email everyone listed in email_recipients.txt after each batch check."""
    if not html_body:
        return
    recipients = load_alert_email_recipients()
    if not recipients:
        print(f"[Email] No addresses in {get_recipients_file_path()}")
        return
    if not email_alerts_configured():
        missing = ", ".join(smtp_missing_vars())
        print(
            "[Email] Alerts enabled but SMTP incomplete. "
            f"Set in .env: {missing or 'SMTP_HOST, SMTP_USER, SMTP_PASSWORD'}"
        )
        return

    subject = os.environ.get(
        "ALERT_EMAIL_SUBJECT", "BuselWorks Daily Site Status Message"
    )
    print(f"[Email] Sending to {len(recipients)} recipient(s)")
    for addr in recipients:
        try:
            send_alert_email(addr, subject, html_body, plain_body)
        except Exception as exc:
            print(f"[Email] Failed for {addr}: {exc}")


async def send_batch_alerts(
    application: Application | None,
    results: list[tuple[str, str, bool]],
    completed_at: datetime,
    *,
    notify_only_on_failure: bool = False,
) -> None:
    """Send email/Telegram. If notify_only_on_failure, skip when all sites are working."""
    has_failure = any(not ok for _, _, ok in results)

    if notify_only_on_failure and not has_failure:
        print("[Alert] All sites working — no email or Telegram sent.")
        return

    email_parts = format_status_alert_email(results, completed_at)
    if email_parts:
        html_body, plain_body = email_parts
        await asyncio.to_thread(send_status_emails, html_body, plain_body)

    telegram_text = format_failure_alert(results, completed_at)
    if not telegram_text:
        return
    if application and load_notify_chat_ids():
        print("[Alert] Sending NOT WORKING alert to Telegram")
        await send_notify_chats(application, telegram_text)
    elif application:
        print("[Alert] No Telegram chats registered for alerts")


def format_check_for_telegram(result: str) -> str:
    """Telegram HTML: green WORKING / red NOT WORKING (via colored emoji + bold)."""
    label, working = _status_from_result(result)
    if not working and "verdict=" in result and "FAIL" not in result:
        label = "NOT WORKING"
    marker = "🟢" if working else "🔴"
    body = re.sub(r"verdict=[^\n|]+\s*", "", result.replace(" | ", "\n")).strip()
    return f"{marker} <b>{html.escape(label)}</b>\n\n<code>{html.escape(body)}</code>"


def check_single_url(url: str) -> str:
    result = check_url(url)
    return format_check_for_telegram(result)


USAGE_MESSAGE = (
    "Send <b>one URL</b> (e.g. <code>cnn.com</code>) to check that site,\n"
    "or send <b>checkall</b> / <b>check all</b> to test every URL in urls.txt."
)


# =====================================================================
# STEP 3: TELEGRAM
# =====================================================================

async def perform_batch_check() -> tuple[
    str | None, str | None, list[tuple[str, str, bool]] | None, datetime | None
]:
    """Run checkall logic. Returns (html_answer, error, results, completed_at)."""
    urls, err = await asyncio.to_thread(load_urls_from_local_file)
    if err:
        return None, err, None, None
    results = await asyncio.to_thread(check_urls_batch, urls)
    completed_at = datetime.now()
    log_batch_summary(results, completed_at)
    return format_batch_for_telegram(results, completed_at), None, results, completed_at


async def send_notify_chats(application: Application, text: str) -> None:
    for chat_id in load_notify_chat_ids():
        try:
            await application.bot.send_message(
                chat_id, text[:4000], parse_mode=ParseMode.HTML
            )
        except Exception as exc:
            print(f"[Notify] Could not message chat {chat_id}: {exc}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_notify_chat(update)
    auto_schedule = format_auto_check_schedule_line()
    daily_times = format_daily_alert_schedule_line()
    auto_line = (
        f"Auto <b>checkall</b> {auto_schedule} "
        f"({'on' if auto_check_enabled() else 'off'}).\n"
        f"Daily checks: <b>{daily_times}</b>.\n"
        "Message this bot once to register for Telegram alerts."
    )
    email_line = (
        "Email list: <code>email_recipients.txt</code> + SMTP in .env."
        if email_alerts_configured() or os.path.isfile(get_recipients_file_path())
        else ""
    )
    await update.message.reply_text(
        "Website Health Agent online.\n\n" + USAGE_MESSAGE + "\n\n"
        f"Batch file: <code>{html.escape(get_url_list_path())}</code> "
        f"(max {MAX_BATCH_URLS} URLs).\n\n" + auto_line
        + (f"\n\n{email_line}" if email_line else ""),
        parse_mode=ParseMode.HTML,
    )


async def run_checkall(update: Update) -> None:
    """Read urls.txt and check each URL (no LLM)."""
    register_notify_chat(update)
    path = get_url_list_path()
    status_message = await update.message.reply_text(
        f"Reading local URL list...\n<code>{html.escape(path)}</code>",
        parse_mode=ParseMode.HTML,
    )
    await status_message.edit_text(f"Checking URLs from file...")
    answer, err, results, completed_at = await perform_batch_check()
    if err:
        await status_message.edit_text(err[:4000])
        return
    if results and completed_at:
        await send_batch_alerts(None, results, completed_at, notify_only_on_failure=False)
    edit_kwargs = {"text": answer[:4000], "parse_mode": ParseMode.HTML}
    try:
        await status_message.edit_text(**edit_kwargs)
    except Exception:
        await status_message.edit_text(answer[:4000])


async def checkall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_checkall(update)


async def run_scheduled_checkall(
    application: Application,
    *,
    label: str = "Auto check",
    send_notifications: bool = False,
    notify_only_on_failure: bool = False,
) -> None:
    path = get_url_list_path()
    print(f"\n[{label}] Scheduled run ({path})")
    answer, err, results, completed_at = await perform_batch_check()
    if err:
        print(f"[{label}] {err}")
        return
    if results is None or completed_at is None:
        return
    if send_notifications:
        await send_batch_alerts(
            application,
            results,
            completed_at,
            notify_only_on_failure=notify_only_on_failure,
        )


async def auto_checkall_loop(application: Application) -> None:
    """Recurring checkall on a clock schedule — email/Telegram only if NOT WORKING."""
    if auto_check_enabled():
        wait_sec, target = seconds_until_next_auto_check()
        print(
            f"[Auto check] Next run at {format_datetime_display(target)} "
            f"({wait_sec / 3600:.1f} h)"
        )
    while True:
        if not auto_check_enabled():
            await asyncio.sleep(3600)
            continue
        wait_sec, target = seconds_until_next_auto_check()
        print(
            f"[Auto check] Sleeping until {format_datetime_display(target)} "
            f"({wait_sec / 3600:.1f} h)"
        )
        await asyncio.sleep(wait_sec)
        try:
            await run_scheduled_checkall(
                application,
                label="Auto check",
                send_notifications=True,
                notify_only_on_failure=True,
            )
        except Exception as exc:
            print(f"[Auto check] Error: {type(exc).__name__}: {exc}")


async def daily_alert_check_loop(application: Application) -> None:
    """6:45 AM (always email) and 12:00 PM (notify only if NOT WORKING)."""
    schedules = get_daily_alert_schedules()
    if schedules:
        wait_sec, label, target, _ = seconds_until_next_daily_alert()
        print(
            f"[Daily alert] Enabled — next: {label} at "
            f"{format_datetime_display(target)}"
        )
    while True:
        schedules = get_daily_alert_schedules()
        if not schedules:
            await asyncio.sleep(3600)
            continue
        wait_sec, label, target, notify_only = seconds_until_next_daily_alert()
        print(
            f"[Daily alert] {label} — sleeping until "
            f"{format_datetime_display(target)} ({wait_sec / 3600:.1f} h)"
        )
        await asyncio.sleep(wait_sec)
        try:
            await run_scheduled_checkall(
                application,
                label=label,
                send_notifications=True,
                notify_only_on_failure=notify_only,
            )
        except Exception as exc:
            print(f"[Daily alert] Error: {type(exc).__name__}: {exc}")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        print(f"[Telegram] Transient network error (will retry): {err}")
        return
    print(f"[Telegram] Unhandled error: {type(err).__name__}: {err}")


async def _start_background_loops(application: Application) -> None:
    while not application.running:
        await asyncio.sleep(0.05)
    application.create_task(auto_checkall_loop(application), name="auto_checkall_loop")
    if get_daily_alert_schedules():
        application.create_task(
            daily_alert_check_loop(application), name="daily_alert_check_loop"
        )


async def post_init(application: Application) -> None:
    if not scheduled_checks_enabled():
        print(
            "Scheduled checks disabled locally (ENABLE_SCHEDULED_CHECKS=false) — "
            "handled elsewhere (e.g. GreenGeeks cron_check.py). This bot only "
            "responds to on-demand messages."
        )
        return
    print(
        f"Auto checkall: {format_auto_check_schedule_line()}, "
        f"enabled={auto_check_enabled()}"
    )
    print(f"Daily alert times: {format_daily_alert_schedule_line()}")
    asyncio.create_task(_start_background_loops(application))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = (update.message.text or "").strip()
    print(f"\nTelegram message: {user_text!r}")
    register_notify_chat(update)

    if is_checkall_command(user_text):
        await run_checkall(update)
        return

    url = resolve_single_url(user_text)
    if url is None:
        if len(extract_urls(user_text)) > 1:
            hint = "Multiple URLs detected. Send one URL at a time, or use checkall."
        else:
            hint = USAGE_MESSAGE
        await update.message.reply_text(hint, parse_mode=ParseMode.HTML)
        return

    status_message = await update.message.reply_text("Checking website...")
    try:
        answer = await asyncio.to_thread(check_single_url, url)
    except Exception as exc:
        answer = f"Error while checking: {type(exc).__name__}: {exc}"
        await status_message.edit_text(answer[:4000])
        return

    edit_kwargs = {"text": answer[:4000], "parse_mode": ParseMode.HTML}
    try:
        await status_message.edit_text(**edit_kwargs)
    except Exception:
        await status_message.edit_text(answer[:4000])


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in your environment before starting the bot.")

    print("Initializing Telegram bot...")
    app = (
        Application.builder()
        .token(token)
        .get_updates_read_timeout(30.0)
        .get_updates_connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .connect_timeout(30.0)
        .post_init(post_init)
        .build()
    )
    url_list = get_url_list_path()
    print(f"URL list file for checkall: {url_list}")
    print(f"Alert email list: {get_recipients_file_path()}")
    print(f"Email alerts: {'enabled' if email_alerts_configured() else 'disabled'}")
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("checkall", checkall_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(global_error_handler)
    print("Bot is live. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
