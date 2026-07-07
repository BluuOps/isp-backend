from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog

SENSITIVE_KEYS = {"password", "password_hash", "secret", "token", "temporary_password"}


def redact(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else item for key, item in value.items()}


def record_audit(
    db: Session,
    *,
    organization_id: int | None,
    action: str,
    target_type: str,
    target_id: str | None,
    actor: str = "platform-admin",
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    success: bool = True,
    ip_address: str | None = None,
) -> None:
    db.add(AuditLog(
        organization_id=organization_id, actor=actor, action=action,
        target_type=target_type, target_id=target_id,
        old_value=redact(old_value), new_value=redact(new_value),
        success=success, ip_address=ip_address,
    ))
