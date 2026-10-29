"""Tests for Telegram and Webhook notifiers."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from jobs.notifier import (
    format_notice_markdown,
    telegram_notifier,
    webhook_notifier,
)

SAMPLE_NOTICE = {
    "event": "deadline",
    "household_id": "default",
    "item_id": "item-abc12345",
    "kind": "insurance",
    "provider": "Health Shield",
    "holder": "Devin Sullivan",
    "reference": "REF-9999",
    "renewal_date": "2026-11-15",
    "days_until": 17,
    "amount": 89.50,
}


def test_format_notice_markdown():
    text = format_notice_markdown(SAMPLE_NOTICE)
    assert "Health Shield" in text
    assert "insurance" in text
    assert "2026-11-15 (17 days remaining)" in text
    assert "$89.50" in text
    assert "Devin Sullivan" in text
    assert "REF-9999" in text


def test_telegram_notifier_missing_config():
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        telegram_notifier(bot_token="", chat_id="12345")

    with pytest.raises(ValueError, match="TELEGRAM_CHAT_ID"):
        telegram_notifier(bot_token="token123", chat_id="")


def test_telegram_notifier_success():
    notifier = telegram_notifier(bot_token="token123", chat_id="chat999")

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        notifier(SAMPLE_NOTICE)

        assert mock_urlopen.call_count == 1
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api.telegram.org/bottoken123/sendMessage"
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["chat_id"] == "chat999"
        assert "Health Shield" in payload["text"]
        assert payload["parse_mode"] == "Markdown"


def test_telegram_notifier_http_error():
    notifier = telegram_notifier(bot_token="token123", chat_id="chat999")

    err = urllib.error.HTTPError(
        url="https://api.telegram.org",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(b'{"description": "Unauthorized"}'),
    )

    with (
        patch("urllib.request.urlopen", side_effect=err),
        pytest.raises(RuntimeError, match="Telegram API HTTP error 401"),
    ):
        notifier(SAMPLE_NOTICE)


def test_webhook_notifier_missing_config():
    with pytest.raises(ValueError, match="webhook URL"):
        webhook_notifier(url="")


def test_webhook_notifier_success():
    notifier = webhook_notifier(url="https://hooks.slack.com/services/T00/B00/X00")

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        notifier(SAMPLE_NOTICE)

        assert mock_urlopen.call_count == 1
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://hooks.slack.com/services/T00/B00/X00"
        payload = json.loads(req.data.decode("utf-8"))
        assert "Health Shield" in payload["text"]
        assert payload["notice"]["item_id"] == "item-abc12345"
