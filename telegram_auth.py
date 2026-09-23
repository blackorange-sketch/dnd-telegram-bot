"""Verifies Telegram Mini App `initData` per Telegram's documented scheme,
so the web API can trust which Telegram user is making a request without a
separate login system.

https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def verify_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> dict | None:
    """Return the parsed `user` dict if `init_data` is a genuine, recent
    payload signed by Telegram for this bot; None if it's missing, forged,
    malformed, or expired."""
    if not init_data or not bot_token:
        return None

    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = parsed.get("auth_date")
    if auth_date:
        try:
            if time.time() - int(auth_date) > max_age_seconds:
                return None
        except ValueError:
            return None

    user_json = parsed.get("user")
    if not user_json:
        return None

    try:
        return json.loads(user_json)
    except json.JSONDecodeError:
        return None
