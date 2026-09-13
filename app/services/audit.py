from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog

SENSITIVE_KEY_MARKERS = {
    "authorization",
    "community",
    "cookie",
    "credential",
    "passphrase",
    "password",
    "private_key",
    "secret",
    "token",
}
ACTOR_TYPE_MAP = {
    "platform-admin": "platform_admin",
    "internal-admin": "organization_staff",
    "organization-admin": "organization_staff",
    "system": "system",
}


def _sensitive_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
    return any(marker in normalized for marker in SENSITIVE_KEY_MARKERS)


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _sensitive_key(key) else _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def redact(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return _redact_value(value)


def record_audit(
    db: Session,
    *,
    organization_id: int | None,
    action: str,
    target_type: str,
    target_id: str | None,
    actor: str = "platform-admin",
    actor_type: str | None = None,
    actor_id: str | None = None,
    actor_label: str | None = None,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    success: bool = True,
    ip_address: str | None = None,
) -> None:
    resolved_actor_type = actor_type or ACTOR_TYPE_MAP.get(actor, "system")
    db.add(AuditLog(
        organization_id=organization_id,
        actor_type=resolved_actor_type,
        actor_id=actor_id,
        actor_label=actor_label or actor,
        actor=actor,
        action=action,
        target_type=target_type, target_id=target_id,
        old_value=redact(old_value), new_value=redact(new_value),
        success=success, ip_address=ip_address,
    ))
