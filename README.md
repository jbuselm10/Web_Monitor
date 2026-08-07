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

`cron_check.py` uses only the Python standard library, so there is no
virtualenv to create and nothing to `pip install` — cron calls the system
`python3` directly. You do not need cPanel's "Setup Python App" at all.

### Setup steps

1. In cPanel → **Git™ Version Control**, clone
   `https://github.com/jbuselm10/Web_Monitor.git` into a **non-web-accessible**
   staging path such as `/home/{user}/repos/Web_Monitor`. Never clone into
   `public_html` or a subdomain document root — an exposed `.git` directory
   lets anyone reconstruct your source.
2. Open **Manage** on that repo → **Pull or Deploy** → **Deploy HEAD Commit**.
   The [.cpanel.yml](.cpanel.yml) in this repo copies only the four files cron
   needs (`cron_check.py`, `urls.txt`, `email_recipients.txt`,
   `notify_chats.txt`) into `/home/{user}/webmonitor`, creating it if needed.
   To update later: **Update from Remote**, then **Deploy HEAD Commit** again.
3. Upload a `.env` file (same keys as `.env.example`) into the same folder and
   set its permissions to **600** (File Manager → right-click → Change
   Permissions). Cron jobs run outside Phusion Passenger, so environment
   variables set in the Setup Python App UI would **not** be visible to them —
   Passenger only injects those for HTTP requests. The script loads `.env` from
   its own directory by absolute path, so an uploaded file is what works.
   Required keys: `TELEGRAM_BOT_TOKEN`, `EMAIL_ALERTS_ENABLED`, `SMTP_HOST`,
   `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`,
   `ALERT_EMAIL_SUBJECT`. Deploys never overwrite it, since `.cpanel.yml`
   only copies the four tracked files.
4. In cPanel → Advanced → **Cron Jobs**, create one job per schedule:
   ```bash
   # Every 6 hours at :30 (minute 30, hour */6) — only alert if something is down
   cd /home/{user}/webmonitor && /usr/bin/python3 cron_check.py --notify-only-on-failure --label "Auto check" >> cron.log 2>&1

   # Daily at 6:45 AM (minute 45, hour 6) — always send a full status email
   cd /home/{user}/webmonitor && /usr/bin/python3 cron_check.py --label "Morning check" >> cron.log 2>&1

   # Daily at 12:00 PM (minute 0, hour 12) — only alert if something is down
   cd /home/{user}/webmonitor && /usr/bin/python3 cron_check.py --notify-only-on-failure --label "Noon check" >> cron.log 2>&1
   ```
   Replace `{user}` with your cPanel username. If `/usr/bin/python3` isn't
   present, try `/usr/local/bin/python3` or plain `python3`.
5. To test, temporarily set one job to run every minute, wait, then check
   `cron.log` in the same folder plus your email/Telegram. Restore the real
   schedule afterwards.
