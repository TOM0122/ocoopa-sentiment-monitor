from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional


COOKIE_NAME = "ocoopa_review_session"
CSRF_COOKIE_NAME = "ocoopa_csrf"


def issue_session(secret: str, ttl_hours: int = 12, now: Optional[int] = None) -> str:
    if not secret:
        raise ValueError("OCOOPA_SESSION_SECRET is required")
    issued = int(time.time() if now is None else now)
    payload = {"iat": issued, "exp": issued + max(1, ttl_hours) * 3600, "nonce": secrets.token_hex(8)}
    encoded = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64(hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def verify_session(token: str, secret: str, now: Optional[int] = None) -> bool:
    if not token or not secret or "." not in token:
        return False
    encoded, signature = token.rsplit(".", 1)
    expected = _b64(hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        payload = json.loads(_unb64(encoded).decode("utf-8"))
        timestamp = int(time.time() if now is None else now)
        return int(payload["iat"]) <= timestamp <= int(payload["exp"])
    except (ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
        return False


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def verify_csrf(cookie_value: str, form_value: str) -> bool:
    return bool(cookie_value and form_value and hmac.compare_digest(cookie_value, form_value))


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
