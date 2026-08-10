from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException, status

from app.core.config import settings


QUOTE_TTL_SECONDS = 10 * 60


@dataclass(frozen=True)
class PaymentQuote:
    reference: str
    organization_id: int
    customer_id: str
    service_id: int
    plan_id: int
    billing_periods: int
    amount_minor: int
    currency: str
    expires_at: int


def _secret() -> bytes:
    if not settings.jwt_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Payment quoting is unavailable")
    return settings.jwt_secret.encode("utf-8")


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_quote(**values: object) -> tuple[str, PaymentQuote]:
    now = int(time.time())
    payload = {
        **values,
        "reference": f"Q-{secrets.token_urlsafe(18)}",
        "expires_at": now + QUOTE_TTL_SECONDS,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(_secret(), raw, hashlib.sha256).digest()
    quote = PaymentQuote(**payload)
    return f"{_encode(raw)}.{_encode(signature)}", quote


def read_quote(token: str) -> PaymentQuote:
    try:
        payload_part, signature_part = token.split(".", 1)
        raw = _decode(payload_part)
        supplied = _decode(signature_part)
        expected = hmac.new(_secret(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError("signature")
        payload = json.loads(raw.decode("utf-8"))
        quote = PaymentQuote(**payload)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payment quote") from exc
    if quote.expires_at < int(time.time()):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Payment quote has expired")
    return quote
