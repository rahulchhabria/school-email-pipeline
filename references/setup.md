# Setup

## Overview

This skill is designed to be called by a local webhook receiver, not by Gmail polling.

Recommended flow:

1. Forward selected emails to a dedicated inbox or inbound-email route.
2. Configure the inbound-email provider to POST the parsed message to the local FastAPI receiver.
3. The receiver normalizes the email, dedupes by `Message-ID`, invokes Ash with `sfday-telegram-alert` in direct-email mode, and forwards the result to Telegram.

## Receiver script

Run the local server with:

```bash
uv run /home/rahul/.ash/workspace/skills/email-forward-summary/scripts/email_webhook_server.py serve --host 0.0.0.0 --port 8787
```

## Required environment

```bash
export TELEGRAM_BOT_TOKEN="<rotated-bot-token>"
export TELEGRAM_CHAT_ID="<chat-id>"
export EMAIL_FORWARD_WEBHOOK_SECRET="<shared-secret>"
export EMAIL_FORWARD_ASH_CWD="/home/rahul/GitHub/ash-main"
```

Optional:

```bash
export EMAIL_FORWARD_ASH_MODEL="default"
export EMAIL_FORWARD_STATE_PATH="/home/rahul/.ash/workspace/skills/email-forward-summary/data/state.json"
export EMAIL_FORWARD_DRY_RUN="0"
export EMAIL_FORWARD_BODY_CHAR_LIMIT="12000"
```

## Webhook contract

The receiver accepts:

- Generic JSON with keys like `message_id`, `from`, `to`, `subject`, `date`, `text`, `html`
- URL-encoded form fields such as `Message-Id`, `sender`, `recipient`, `subject`, `body-plain`, `body-html`
- Postmark-style JSON with fields like `MessageID`, `From`, `To`, `Subject`, `TextBody`, `HtmlBody`, `Date`

This local build does not accept `multipart/form-data`. If your inbound-email provider defaults to multipart delivery, configure it to send JSON or URL-encoded fields instead.

Send the shared secret in:

```txt
X-Email-Webhook-Secret: <shared-secret>
```

## Local test

```bash
curl -X POST http://127.0.0.1:8787/webhooks/email \
  -H 'Content-Type: application/json' \
  -H 'X-Email-Webhook-Secret: test-secret' \
  -d '{
    "message_id": "<demo-1@example.com>",
    "from": "Alice <alice@example.com>",
    "to": "ash-mail@example.com",
    "subject": "Please review the school trip form by Friday",
    "date": "2026-03-21T12:00:00Z",
    "text": "Can you review and submit the school trip form by Friday at 5pm?"
  }'
```

## Notes

- Telegram delivery is handled by the receiver, not the skill.
- Deduplication is based on `Message-ID` when available.
- The receiver currently invokes `sfday-telegram-alert` in direct-email mode so you keep the same parent-oriented summary parameters.
- If the skill returns `[NO_REPLY]`, the receiver suppresses Telegram delivery.
