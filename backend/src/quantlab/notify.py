from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
import urllib.parse

import requests

log = logging.getLogger(__name__)


def _signed_url(webhook: str, secret: str) -> str:
    if not secret:
        return webhook
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    separator = "&" if "?" in webhook else "?"
    return f"{webhook}{separator}timestamp={timestamp}&sign={sign}"


def send_dingtalk(title: str, text: str) -> bool:
    webhook = os.getenv("DINGTALK_WEBHOOK", "").strip()
    secret = os.getenv("DINGTALK_SECRET", "").strip()
    if not webhook:
        log.info("未配置 DINGTALK_WEBHOOK，跳过推送")
        return False
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
    }
    try:
        response = requests.post(_signed_url(webhook, secret), json=payload, timeout=10)
        response.raise_for_status()
        result = response.json()
        if result.get("errcode") != 0:
            log.error("钉钉推送失败: %s", result)
            return False
        return True
    except Exception as exc:
        log.exception("钉钉推送异常: %s", exc)
        return False