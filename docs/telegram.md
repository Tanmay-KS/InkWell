# Telegram adapter

`interface/chat/telegram.py` is a thin transport around the same
`LedgerBot` (`interface/bot/handlers.py`) and the same household database
as the web view. It answers questions, records a cancellation or
reactivation, and ingests a notice dropped into chat.

| Capability | Status |
|---|---|
| Inbound text and commands | yes, long polling |
| `/due`, `/cancel`, `/active`, `help` / `/start` | yes |
| Natural-language questions | yes |
| Attachment ingestion | documents |
| Proactive deadline notices | yes, through `TELEGRAM_CHAT_ID` |
| Needs a public URL or webhook | no |
| Live status (2026-10-30) | **verified** with `@arcky_bot` |

Slack is not implemented as an interactive bot. The existing Slack
incoming webhook remains a one-way notification destination.

---

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and put the token
   in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Message the bot once. Long polling reads the chat ID from that update;
   put it in `TELEGRAM_CHAT_ID` if you also want proactive notices from the
   deadline monitor.
3. Run it:

   ```bash
   make telegram-bot
   # or: python -m interface.chat
   ```

The bot calls `getUpdates`; it never registers a webhook. It therefore
works from a laptop behind NAT and cannot be reached from the internet.

## Commands

```
/start or /help              command list
/due                         what is due in the next 30 days
/cancel ITEM_ID [DATE]       record a cancellation
/active ITEM_ID [DATE]       record a reactivation
<provider name>              look up a tracked item
```

A PDF or text notice sent to the bot is downloaded to a temporary file,
parsed by the local pipeline, stored in the household database, and
deleted. The reply is a status line and a **redacted** draft preview. The
full draft stays in the loopback web view.

## Privacy boundary

Chat is an external destination, so the adapter is deliberately narrow:

* attachments are written to a temporary directory, parsed locally, and
  deleted; nothing is re-uploaded;
* replies contain the provider, dates and a redacted draft preview, never
  the full letter;
* the chat path does not call a model, so a notice sent over Telegram is
  not sent to Token Factory by this adapter;
* Ledger never sends a letter from chat. An approved draft is something a
  person sends themselves.

The bot treats inbound text as data. The injection guard is still the
place that decides whether document text is instructions; the adapter only
moves bytes.

## Failure behaviour

* A malformed or unsupported update is skipped; the rest of the batch is
  still handled.
* A failed long poll is logged without personal content and retried after
  a short backoff.
* A rejected bot token (`401`) is fatal. The process exits with a clear
  message instead of retrying forever.
* A failed attachment download is reported in chat and does not stop the
  poller.

## Tests

```bash
pytest tests/test_chat_telegram.py tests/test_bot_handlers.py
```

The adapter tests use a fake Bot API, so they touch no network. They cover
reply routing, commands, attachment ingestion, failed downloads, offset
advancement, malformed updates, long-reply splitting, and fatal handling of
a rejected token.
