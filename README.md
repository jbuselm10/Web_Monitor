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
