from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from typing import Any, Literal

from fastapi import Header, HTTPException, status
from pydantic import BaseModel

from app.core.config import settings

PrincipalType = Literal["platform_admin", "organization_staff", "customer", "system"]

PLATFORM_PERMISSIONS = [
    "platform.dashboard.read",
    "platform.organizations.read",
    "platform.organizations.manage",
    "platform.subscriptions.read",
    "platform.subscriptions.manage",
    "platform.feature_flags.read",
    "platform.feature_flags.manage",
    "platform.health.read",
    "platform.organization_admins.manage",
]

ORGANIZATION_BRIDGE_PERMISSIONS = [
    "billing_access",
    "create_pppoe",
    "delete_customer",
    "disconnect_user",
    "radius_access",
    "settings_access",
    "view_customers",
]


class Principal(BaseModel):
    sub: str
    principal_type: PrincipalType
    organization_id: int | None = None
    organization_slug: str | None = None
    customer_id: str | None = None
    roles: list[str] = []
    permissions: list[str] = []
    email: str | None = None


def _auth_error(http_status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"error": code, "message": message})


def _require_jwt_secret() -> str:
    if not settings.jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="JWT signing secret is not configured",
        )
    return settings.jwt_secret


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _sign(payload: str, secret: str) -> str:
    return _b64url_encode(hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest())


def create_principal_token(payload: dict[str, Any], ttl_seconds: int | None = None) -> str:
    now = int(time.time())
    lifetime = settings.auth_access_token_ttl_seconds if ttl_seconds is None else ttl_seconds
    claims = {
        "token_type": "access",
        "iss": settings.auth_token_issuer,
        "aud": settings.auth_token_audience,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + lifetime,
        **payload,
    }
    encoded_payload = _b64url_encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{encoded_payload}.{_sign(encoded_payload, _require_jwt_secret())}"


def decode_principal_token(token: str) -> dict[str, Any]:
    try:
        encoded_payload, signature = token.split(".", 1)
    except ValueError as exc:
        raise _auth_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", "Invalid bearer token.") from exc

    expected = _sign(encoded_payload, _require_jwt_secret())
    if not secrets.compare_digest(signature, expected):
        raise _auth_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", "Invalid bearer token.")

    try:
        payload = json.loads(_b64url_decode(encoded_payload))
    except (ValueError, json.JSONDecodeError) as exc:
        raise _auth_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", "Invalid bearer token.") from exc

    required = {"sub", "principal_type", "token_type", "iat", "exp", "iss", "aud", "jti"}
    if not isinstance(payload, dict) or required - payload.keys():
        raise _auth_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", "Token claims are incomplete.")
    if not isinstance(payload["jti"], str) or not payload["jti"]:
        raise _auth_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", "Token claims are incomplete.")
    if payload["token_type"] != "access":
        raise _auth_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", "Invalid token type.")
    if payload["iss"] != settings.auth_token_issuer or payload["aud"] != settings.auth_token_audience:
        raise _auth_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", "Invalid token audience.")
    now = int(time.time())
    try:
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
    except (TypeError, ValueError) as exc:
        raise _auth_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", "Invalid token timestamps.") from exc
    if issued_at > now + 60 or expires_at <= now:
        raise _auth_error(status.HTTP_401_UNAUTHORIZED, "token_expired", "Bearer token has expired.")
    return payload


def bearer_payload(authorization: str | None) -> dict[str, Any] | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return decode_principal_token(authorization.split(" ", 1)[1])


def reject_customer_principal(authorization: str | None = Header(default=None)) -> None:
    payload = bearer_payload(authorization)
    if payload and payload.get("principal_type") == "customer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Customer portal tokens cannot access organization or platform APIs",
        )


def require_organization_staff_principal(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    payload = bearer_payload(authorization)
    if not payload:
        raise _auth_error(status.HTTP_401_UNAUTHORIZED, "missing_organization_staff_token", "Organization staff token is required.")
    if payload.get("principal_type") == "customer":
        raise _auth_error(status.HTTP_403_FORBIDDEN, "wrong_principal_type", "Customer portal tokens cannot access organization workspace APIs.")
    if payload.get("principal_type") != "organization_staff":
        raise _auth_error(status.HTTP_403_FORBIDDEN, "wrong_principal_type", "Organization staff access is required.")
    if not payload.get("organization_id") or not payload.get("organization_slug"):
        raise _auth_error(status.HTTP_401_UNAUTHORIZED, "invalid_organization_claims", "Organization staff token is missing required organization claims.")
    return payload
