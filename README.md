# Web Monitor

A Telegram bot + scheduled watchdog that checks whether a list of websites is up, and reports status via Telegram and email.

## Features

- **Telegram bot**: send it a URL to check that one site, or send `checkall` to check every URL in [urls.txt](urls.txt).
- **Scheduled checks**: an automatic `checkall` on a recurring interval (default every 6 hours), plus a morning and noon daily check.
- **Alerts**: sends a Telegram message to registered chats and/or an email summary when a site is not working (morning check always emails a full status summary).

## Setup

1. Install [Python 3.11+](https://www.python.org/).
2. Copy `.env.example` to `.env` and fill in real values:
   - `TELEGRAM_BOT_TOKEN` - from [@BotFather](https://t.me/BotFather)
   - `GEMINI_API_KEY` - from [Google AI Studio](https://aistudio.google.com/)
   - SMTP settings if you want email alerts (Gmail requires an [app password](https://myaccount.google.com/apppasswords))
3. Edit [urls.txt](urls.txt) with the sites you want monitored (one per line, `#` for comments).
4. Edit [email_recipients.txt](email_recipients.txt) with the email addresses that should receive status alerts.

## Running

On Windows, just run:

```bat
run.bat
```

This creates a virtual environment, installs dependencies from `requirements.txt`, and starts the bot. On subsequent runs it reuses the existing `venv/`.

Manually (any OS):

```bash
python -m venv venv
source venv/bin/activate  # venv\Scripts\activate.bat on Windows
pip install -r requirements.txt
python "Agent Website.py"
```

## Usage (Telegram)

- `/start` - registers the chat for scheduled alerts and shows the current schedule.
- Send a single URL (e.g. `cnn.com`) to check that site.
- Send `checkall` (or `check all`) to check every URL in `urls.txt`.

## Deploying scheduled checks to GreenGeeks (shared/cPanel hosting)

Shared hosting can't reliably run a persistent, always-listening Telegram bot
(`Agent Website.py` uses `run_polling()`, which needs a long-lived process).
Instead, [cron_check.py](cron_check.py) is a standalone script that checks all
sites once and sends email/Telegram alerts, meant to be triggered by cPanel
Cron Jobs on a schedule — no bot polling required.

If you keep the local bot running too (e.g. for instant on-demand checks),
set `ENABLE_SCHEDULED_CHECKS=false` in its local `.env` once GreenGeeks is
confirmed working, so you don't get duplicate alerts from both places.

### Setup steps

1. In cPanel, use **Setup Python App** to create a new Python app (this gives
   you an isolated virtualenv and a Python interpreter path).
2. Upload these files to the app's directory (via File Manager, FTP, or
   `git clone` if SSH is enabled): `cron_check.py`, `requirements-cron.txt`,
   `urls.txt`, `email_recipients.txt`, `notify_chats.txt`.
3. Activate the app's virtualenv (cPanel shows the exact activation command)
   and install dependencies:
   ```bash
   pip install -r requirements-cron.txt
   ```
4. Set secrets as environment variables in the cPanel Python App's
   configuration (or upload a `.env` with the same keys as `.env.example`):
   `TELEGRAM_BOT_TOKEN`, `EMAIL_ALERTS_ENABLED`, `SMTP_HOST`, `SMTP_PORT`,
   `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `ALERT_EMAIL_SUBJECT`.
5. In cPanel → Advanced → **Cron Jobs**, create one job per schedule, using
   the venv's Python interpreter path shown by Setup Python App:
   ```bash
   # Every 6 hours at :30 — only alert if something is down
   cd /home/{user}/web-monitor && /home/{user}/virtualenv/web-monitor/3.x/bin/python cron_check.py --notify-only-on-failure --label "Auto check" >> cron.log 2>&1

   # Daily at 6:45 AM — always send a full status email
   cd /home/{user}/web-monitor && /home/{user}/virtualenv/web-monitor/3.x/bin/python cron_check.py --label "Morning check" >> cron.log 2>&1

   # Daily at 12:00 PM — only alert if something is down
   cd /home/{user}/web-monitor && /home/{user}/virtualenv/web-monitor/3.x/bin/python cron_check.py --notify-only-on-failure --label "Noon check" >> cron.log 2>&1
   ```
   Replace `{user}` and the Python version path with your actual values from
   cPanel.
6. Trigger a run manually (or wait for the next cron tick) and check
   `cron.log` plus your email/Telegram to confirm alerts arrive.
