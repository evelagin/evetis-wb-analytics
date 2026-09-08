"""Register the Telegram webhook pointing at the Cloud Run service.

Usage:
    TELEGRAM_WEBHOOK_URL=https://<service-url>/telegram-webhook \
    GCP_PROJECT_ID=... python -m scripts.set_telegram_webhook

Reads the bot token and webhook secret from Secret Manager (or env for local),
and calls setWebhook with `secret_token` so Telegram signs every update.
"""
from __future__ import annotations

import sys

from app.config import get_settings
from app.services.telegram_client import TelegramClient


def main() -> int:
    settings = get_settings()
    url = settings.telegram_webhook_url
    if not url:
        print("ERROR: set TELEGRAM_WEBHOOK_URL to https://<service-url>/telegram-webhook")
        return 2
    secrets = settings.secrets
    if not secrets.telegram_bot_token or not secrets.telegram_webhook_secret:
        print("ERROR: EVETIS_TELEGRAM_BOT_TOKEN / EVETIS_TELEGRAM_WEBHOOK_SECRET missing")
        return 2
    client = TelegramClient(secrets.telegram_bot_token)
    result = client.set_webhook(url, secrets.telegram_webhook_secret)
    print("setWebhook OK:", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
