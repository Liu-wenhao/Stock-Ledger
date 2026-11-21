from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from urllib.parse import quote_plus

import requests

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def configured() -> bool:
    return bool(get_settings().dingtalk_webhook_url.strip())


def _signed_url(webhook: str, secret: str) -> str:
    if not secret:
        return webhook
    timestamp = str(round(time.time() * 1000))
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}\n{secret}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    separator = "&" if "?" in webhook else "?"
    return f"{webhook}{separator}timestamp={timestamp}&sign={quote_plus(base64.b64encode(digest))}"


def send_markdown(title: str, content: str, timeout: int = 10) -> bool:
    """Send one compact monitor message without ever logging the webhook secret."""
    settings = get_settings()
    webhook = settings.dingtalk_webhook_url.strip()
    if not webhook:
        return False
    safe_title = (title or "股票指标提醒").strip()[:100]
    text = (content or "").strip()
    if len(text.encode("utf-8")) > 18_000:
        text = text.encode("utf-8")[:18_000].decode("utf-8", errors="ignore") + "\n\n（内容已截断）"
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": safe_title,
            "text": f"### {safe_title}\n\n{text}",
        },
    }
    try:
        response = requests.post(
            _signed_url(webhook, settings.dingtalk_secret.strip()),
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("errcode") == 0:
            return True
        logger.warning(
            "DingTalk rejected monitor message: errcode=%s errmsg=%s",
            result.get("errcode"),
            result.get("errmsg"),
        )
    except Exception as exc:
        logger.warning("DingTalk monitor message failed: %s", type(exc).__name__)
    return False
