from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any

from app.core.config import settings
from app.integrations.base import (
    GatewayInitializeResult,
    GatewayVerifyResult,
    GatewayWebhookEvent,
    PaymentGatewayError,
)


def _safe_http_error_code(http_status: int, message: str) -> str:
    normalized = message.lower()
    if "email" in normalized:
        category = "invalid_email"
    elif "callback" in normalized or "url" in normalized:
        category = "invalid_callback"
    elif "amount" in normalized:
        category = "invalid_amount"
    elif "authorization" in normalized or "secret key" in normalized or "api key" in normalized:
        category = "authentication_failed"
    elif http_status >= 500:
        category = "upstream_error"
    else:
        category = "request_rejected"
    return f"gateway_http_{http_status}_{category}"


class PaystackGateway:
    gateway_name = "paystack"

    def __init__(
        self,
        *,
        secret_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.secret_key = secret_key or settings.paystack_secret_key
        self.base_url = (base_url or settings.paystack_base_url).rstrip("/")
        self.timeout_seconds = timeout_seconds
        if not self.secret_key:
            raise PaymentGatewayError("Paystack secret key is not configured.", code="gateway_not_configured")

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.secret_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "RadiusFiber/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                error_payload = {"message": "Paystack HTTP error"}
            provider_message = str(error_payload.get("message") or "Paystack HTTP error")
            raise PaymentGatewayError(
                "Paystack rejected the transaction request.",
                code=_safe_http_error_code(exc.code, provider_message),
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise PaymentGatewayError("Unable to reach Paystack.", code="gateway_network_error") from exc
        except json.JSONDecodeError as exc:
            raise PaymentGatewayError("Paystack returned an invalid response.", code="gateway_invalid_response") from exc

    def initialize_transaction(
        self,
        *,
        email: str,
        amount_kobo: int,
        reference: str,
        callback_url: str,
        metadata: dict[str, Any],
    ) -> GatewayInitializeResult:
        payload = {
            "email": email,
            "amount": amount_kobo,
            "reference": reference,
            "callback_url": callback_url,
            "metadata": metadata,
        }
        response = self._request("POST", "/transaction/initialize", payload)
        if not response.get("status"):
            raise PaymentGatewayError(str(response.get("message") or "Paystack initialization failed."), code="gateway_initialize_failed")
        data = response.get("data") or {}
        authorization_url = data.get("authorization_url")
        access_code = data.get("access_code")
        if not authorization_url or not access_code:
            raise PaymentGatewayError("Paystack returned incomplete checkout data.", code="gateway_invalid_response")
        return GatewayInitializeResult(
            authorization_url=authorization_url,
            access_code=access_code,
            gateway_reference=data.get("reference") or reference,
            raw_status=str(response.get("message") or ""),
            metadata={"paystack_data": {k: data.get(k) for k in ("reference", "access_code")}},
        )

    def verify_transaction(self, reference: str) -> GatewayVerifyResult:
        response = self._request("GET", f"/transaction/verify/{reference}")
        if not response.get("status"):
            raise PaymentGatewayError(str(response.get("message") or "Paystack verification failed."), code="gateway_verify_failed")
        data = response.get("data") or {}
        amount = Decimal(str(data.get("amount") or 0)) / Decimal("100")
        return GatewayVerifyResult(
            status=str(data.get("status") or "").lower(),
            reference=str(data.get("reference") or reference),
            amount=amount,
            currency=str(data.get("currency") or "").upper(),
            gateway_reference=str(data.get("id")) if data.get("id") is not None else None,
            paid_at=data.get("paid_at"),
            raw_status=str(data.get("status") or ""),
            metadata={
                "paystack_id": data.get("id"),
                "channel": data.get("channel"),
                "fees": data.get("fees"),
                "gateway_response": data.get("gateway_response"),
                "transaction_metadata": data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
            },
        )

    def validate_webhook(self, raw_body: bytes, signature: str | None) -> GatewayWebhookEvent:
        if not signature:
            raise PaymentGatewayError("Missing Paystack signature.", code="invalid_signature")
        expected = hmac.new(self.secret_key.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise PaymentGatewayError("Invalid Paystack signature.", code="invalid_signature")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise PaymentGatewayError("Invalid webhook payload.", code="invalid_webhook_payload") from exc
        data = payload.get("data") or {}
        return GatewayWebhookEvent(
            event=str(payload.get("event") or ""),
            reference=data.get("reference"),
            raw=payload,
        )
