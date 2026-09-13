from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


class PaymentGatewayError(RuntimeError):
    def __init__(self, message: str, *, code: str = "gateway_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GatewayInitializeResult:
    authorization_url: str
    access_code: str | None
    gateway_reference: str
    raw_status: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class GatewayVerifyResult:
    status: str
    reference: str
    amount: Decimal
    currency: str
    gateway_reference: str | None
    paid_at: str | None = None
    raw_status: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class GatewayWebhookEvent:
    event: str
    reference: str | None
    raw: dict[str, Any]


class PaymentGateway(Protocol):
    gateway_name: str

    def initialize_transaction(
        self,
        *,
        email: str,
        amount_kobo: int,
        reference: str,
        callback_url: str,
        metadata: dict[str, Any],
    ) -> GatewayInitializeResult:
        ...

    def verify_transaction(self, reference: str) -> GatewayVerifyResult:
        ...

    def validate_webhook(self, raw_body: bytes, signature: str | None) -> GatewayWebhookEvent:
        ...
