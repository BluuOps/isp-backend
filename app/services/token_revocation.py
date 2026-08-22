from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.auth_token_revocation import AuthTokenRevocation
from app.core.config import settings


def _jti_hash(jti: str) -> str:
    return hashlib.sha256(jti.encode("utf-8")).hexdigest()


def _required_claim(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token claims are incomplete")
    return value


def token_fingerprint(payload: dict[str, Any]) -> str:
    return _jti_hash(_required_claim(payload, "jti"))[-12:]


def ensure_token_not_revoked(db: Session, payload: dict[str, Any]) -> None:
    digest = _jti_hash(_required_claim(payload, "jti"))
    try:
        revoked = (
            db.query(AuthTokenRevocation)
            .filter(AuthTokenRevocation.jti_hash == digest)
            .first()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication revocation state is unavailable",
        ) from exc
    if revoked:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked")


def cleanup_expired_revocations(db: Session, *, batch_size: int | None = None) -> int:
    """Remove one deterministic bounded batch using PostgreSQL's clock.

    Callers may invoke this repeatedly to drain a backlog. The composite
    ``(expires_at, id)`` index supports the expiry predicate and stable order.
    """
    limit = settings.auth_token_revocation_cleanup_batch_size if batch_size is None else batch_size
    if limit < 1 or limit > 1000:
        raise ValueError("Revocation cleanup batch size must be between 1 and 1000")
    expired_ids = list(
        db.scalars(
            select(AuthTokenRevocation.id)
            .where(AuthTokenRevocation.expires_at <= func.current_timestamp())
            .order_by(AuthTokenRevocation.expires_at, AuthTokenRevocation.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    if not expired_ids:
        return 0
    result = db.execute(
        delete(AuthTokenRevocation).where(AuthTokenRevocation.id.in_(expired_ids))
    )
    return int(result.rowcount or 0)


def _opportunistic_cleanup(db: Session) -> int:
    """Isolate cleanup failure so it cannot discard the requested revocation."""
    try:
        with db.begin_nested():
            return cleanup_expired_revocations(db)
    except SQLAlchemyError:
        return 0


def revoke_token(
    db: Session,
    payload: dict[str, Any],
    *,
    reason: str = "logout",
) -> bool:
    _opportunistic_cleanup(db)
    jti = _required_claim(payload, "jti")
    subject_id = _required_claim(payload, "sub")
    principal_type = _required_claim(payload, "principal_type")
    try:
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token expiration") from exc

    digest = _jti_hash(jti)
    if db.query(AuthTokenRevocation).filter(AuthTokenRevocation.jti_hash == digest).first():
        return False
    organization_id = payload.get("organization_id")
    try:
        normalized_organization_id = int(organization_id) if organization_id is not None else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid organization claim") from exc
    try:
        with db.begin_nested():
            db.add(
                AuthTokenRevocation(
                    jti_hash=digest,
                    principal_type=principal_type,
                    subject_id=subject_id,
                    organization_id=normalized_organization_id,
                    expires_at=expires_at,
                    reason=reason,
                )
            )
            db.flush()
    except IntegrityError:
        # A concurrent logout may have inserted the same hash. The outer
        # transaction remains usable because the conflict is savepoint-scoped.
        return False
    return True
