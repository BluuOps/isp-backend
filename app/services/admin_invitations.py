from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from app.core.authorization import AuthenticatedPrincipal
from app.core.config import settings
from app.models import (
    Organization,
    OrganizationAdminInvitation,
    OrganizationAdminInvitationRateLimit,
    OrganizationStaff,
)
from app.services.audit import record_audit
from app.services.security import hash_password

INVITATION_PURPOSES = frozenset({"bootstrap", "invite", "recovery"})
GENERIC_INVALID_INVITATION = "Invitation is invalid or unavailable"
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass(frozen=True)
class CreatedInvitation:
    invitation: OrganizationAdminInvitation
    token: str


def database_now(db: Session) -> datetime:
    value = db.execute(select(func.current_timestamp())).scalar_one()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 255 or not EMAIL_PATTERN.fullmatch(email):
        raise HTTPException(status_code=422, detail="A valid email is required")
    return email


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _rate_key(value: str) -> str:
    if not settings.jwt_secret:
        raise HTTPException(status_code=503, detail="Authentication is not configured")
    return hmac.new(
        settings.jwt_secret.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def consume_rate_limit(
    db: Session,
    *,
    scope: str,
    identity: str,
    limit: int,
    window_seconds: int = 600,
) -> None:
    now = database_now(db)
    window_epoch = int(now.timestamp()) // window_seconds * window_seconds
    window_started = datetime.fromtimestamp(window_epoch, tz=timezone.utc)
    key_hash = _rate_key(identity)
    db.query(OrganizationAdminInvitationRateLimit).filter(
        OrganizationAdminInvitationRateLimit.expires_at <= now
    ).delete(synchronize_session=False)
    if db.get_bind().dialect.name == "postgresql":
        statement = (
            postgresql_insert(OrganizationAdminInvitationRateLimit)
            .values(
                scope=scope,
                key_hash=key_hash,
                window_started_at=window_started,
                expires_at=window_started + timedelta(seconds=window_seconds * 2),
                attempt_count=1,
            )
            .on_conflict_do_update(
                index_elements=["scope", "key_hash", "window_started_at"],
                set_={
                    "attempt_count": OrganizationAdminInvitationRateLimit.attempt_count + 1,
                },
                where=OrganizationAdminInvitationRateLimit.attempt_count < limit,
            )
            .returning(OrganizationAdminInvitationRateLimit.attempt_count)
        )
        count = db.execute(statement).scalar_one_or_none()
        if count is None:
            raise HTTPException(status_code=429, detail="Too many requests")
        return
    row = (
        db.query(OrganizationAdminInvitationRateLimit)
        .filter(
            OrganizationAdminInvitationRateLimit.scope == scope,
            OrganizationAdminInvitationRateLimit.key_hash == key_hash,
            OrganizationAdminInvitationRateLimit.window_started_at == window_started,
        )
        .with_for_update()
        .first()
    )
    if row is None:
        row = OrganizationAdminInvitationRateLimit(
            scope=scope,
            key_hash=key_hash,
            window_started_at=window_started,
            expires_at=window_started + timedelta(seconds=window_seconds * 2),
            attempt_count=1,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError as exc:
            raise HTTPException(status_code=429, detail="Too many requests") from exc
        return
    if row.attempt_count >= limit:
        raise HTTPException(status_code=429, detail="Too many requests")
    row.attempt_count += 1
    db.flush()


def _active_invitation_query(
    db: Session,
    *,
    organization_id: int,
    purpose: str,
    now: datetime,
    email: str | None = None,
):
    query = db.query(OrganizationAdminInvitation).filter(
        OrganizationAdminInvitation.organization_id == organization_id,
        OrganizationAdminInvitation.purpose == purpose,
        OrganizationAdminInvitation.used_at.is_(None),
        OrganizationAdminInvitation.revoked_at.is_(None),
        OrganizationAdminInvitation.expires_at > now,
    )
    if email is not None:
        query = query.filter(OrganizationAdminInvitation.email == email)
    return query


def create_admin_invitation(
    db: Session,
    *,
    organization_id: int,
    email: str,
    purpose: str,
    reason: str,
    principal: AuthenticatedPrincipal,
) -> CreatedInvitation:
    if purpose not in INVITATION_PURPOSES:
        raise HTTPException(status_code=422, detail="Invalid invitation purpose")
    normalized_email = normalize_email(email)
    normalized_reason = reason.strip()
    if len(normalized_reason) < 8 or len(normalized_reason) > 500:
        raise HTTPException(status_code=422, detail="A specific reason is required")

    organization = (
        db.query(Organization)
        .filter(Organization.id == organization_id)
        .with_for_update()
        .first()
    )
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    now = database_now(db)
    consume_rate_limit(
        db,
        scope="creation",
        identity=f"{principal.subject_id}:{organization_id}",
        limit=5,
    )
    record_audit(
        db,
        organization_id=organization_id,
        actor=principal.actor_label,
        actor_type="platform_admin",
        actor_id=principal.subject_id,
        actor_label=principal.actor_label,
        action="admin_invitation.requested",
        target_type="organization_admin_invitation",
        target_id=None,
        new_value={
            "purpose": purpose,
            "reason": normalized_reason,
            "correlation_id": principal.correlation_id,
            "outcome": "requested",
        },
    )

    admins = db.query(OrganizationStaff).filter(
        OrganizationStaff.organization_id == organization_id,
        OrganizationStaff.role == "Organization Admin",
        OrganizationStaff.status == "active",
        OrganizationStaff.is_uat_fixture.is_(False),
    )
    active_admin_count = admins.count()
    existing_staff = db.query(OrganizationStaff).filter(
        OrganizationStaff.organization_id == organization_id,
        func.lower(OrganizationStaff.email) == normalized_email,
    )

    rejection: str | None = None
    if purpose == "bootstrap":
        if organization.status not in {"active", "trial"}:
            rejection = "organization_state"
        elif active_admin_count != 0:
            rejection = "active_admin_exists"
        elif _active_invitation_query(
            db, organization_id=organization_id, purpose=purpose, now=now
        ).first():
            rejection = "active_bootstrap_exists"
    elif purpose == "invite":
        if organization.status != "active":
            rejection = "organization_state"
        elif existing_staff.first() is not None:
            rejection = "staff_email_exists"
        elif _active_invitation_query(
            db,
            organization_id=organization_id,
            purpose=purpose,
            email=normalized_email,
            now=now,
        ).first():
            rejection = "active_invitation_exists"
    else:
        if organization.status != "active":
            rejection = "organization_state"
        elif admins.filter(func.lower(OrganizationStaff.email) == normalized_email).count() != 1:
            rejection = "exact_active_admin_not_found"
        elif _active_invitation_query(
            db,
            organization_id=organization_id,
            purpose=purpose,
            email=normalized_email,
            now=now,
        ).first():
            rejection = "active_invitation_exists"

    if rejection:
        record_audit(
            db,
            organization_id=organization_id,
            actor=principal.actor_label,
            actor_type="platform_admin",
            actor_id=principal.subject_id,
            actor_label=principal.actor_label,
            action="admin_invitation.creation_rejected",
            target_type="organization_admin_invitation",
            target_id=None,
            success=False,
            new_value={"purpose": purpose, "reason": normalized_reason, "outcome": rejection},
        )
        db.commit()
        raise HTTPException(status_code=409, detail="Invitation cannot be created")

    token = secrets.token_urlsafe(32)
    invitation = OrganizationAdminInvitation(
        organization_id=organization_id,
        email=normalized_email,
        token_hash=token_digest(token),
        purpose=purpose,
        expires_at=now + timedelta(seconds=settings.admin_invitation_ttl_seconds),
        max_attempts=settings.admin_invitation_max_attempts,
        created_by_principal_type="platform_admin",
        created_by_principal_id=principal.subject_id,
        reason=normalized_reason,
        correlation_id=uuid.uuid4().hex,
    )
    db.add(invitation)
    db.flush()
    record_audit(
        db,
        organization_id=organization_id,
        actor=principal.actor_label,
        actor_type="platform_admin",
        actor_id=principal.subject_id,
        actor_label=principal.actor_label,
        action="admin_invitation.created",
        target_type="organization_admin_invitation",
        target_id=str(invitation.id),
        new_value={
            "purpose": purpose,
            "reason": normalized_reason,
            "correlation_id": invitation.correlation_id,
            "outcome": "created",
        },
    )
    return CreatedInvitation(invitation=invitation, token=token)


def _record_acceptance_rejection(
    db: Session,
    invitation: OrganizationAdminInvitation | None,
    *,
    outcome: str,
) -> None:
    if invitation is not None and invitation.used_at is None:
        invitation.attempt_count = min(invitation.max_attempts, invitation.attempt_count + 1)
        if invitation.attempt_count >= invitation.max_attempts and invitation.revoked_at is None:
            invitation.revoked_at = database_now(db)
    action = {
        "expired": "admin_invitation.expired",
        "bootstrap_conflict": "admin_invitation.concurrency_conflict",
        "invite_conflict": "admin_invitation.concurrency_conflict",
        "recovery_conflict": "admin_invitation.concurrency_conflict",
    }.get(outcome, "admin_invitation.acceptance_rejected")
    record_audit(
        db,
        organization_id=invitation.organization_id if invitation else None,
        actor="invitation-acceptance",
        actor_type="system",
        actor_id=None,
        action=action,
        target_type="organization_admin_invitation",
        target_id=str(invitation.id) if invitation else None,
        success=False,
        new_value={
            "purpose": invitation.purpose if invitation else "unknown",
            "correlation_id": invitation.correlation_id if invitation else uuid.uuid4().hex,
            "outcome": outcome,
        },
    )
    db.commit()
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=GENERIC_INVALID_INVITATION)


def validate_new_password(password: str) -> None:
    if len(password) < 12 or len(password) > 128:
        raise HTTPException(status_code=422, detail="Password does not meet policy")
    lowered = password.casefold()
    if lowered in {"password1234", "administrator", "radiusfiber"} or len(set(password)) < 6:
        raise HTTPException(status_code=422, detail="Password does not meet policy")


def accept_admin_invitation(
    db: Session,
    *,
    token: str,
    new_password: str,
    rate_identity: str,
) -> tuple[OrganizationAdminInvitation, OrganizationStaff]:
    consume_rate_limit(db, scope="acceptance", identity=rate_identity, limit=20)
    try:
        validate_new_password(new_password)
    except HTTPException:
        db.commit()
        raise
    digest = token_digest(token)
    invitation = (
        db.query(OrganizationAdminInvitation)
        .filter(OrganizationAdminInvitation.token_hash == digest)
        .with_for_update()
        .first()
    )
    if invitation is None:
        _record_acceptance_rejection(db, None, outcome="invalid")

    record_audit(
        db,
        organization_id=invitation.organization_id,
        actor="invitation-acceptance",
        actor_type="system",
        action="admin_invitation.acceptance_attempted",
        target_type="organization_admin_invitation",
        target_id=str(invitation.id),
        new_value={
            "purpose": invitation.purpose,
            "correlation_id": invitation.correlation_id,
            "outcome": "attempted",
        },
    )

    organization = (
        db.query(Organization)
        .filter(Organization.id == invitation.organization_id)
        .with_for_update()
        .first()
    )
    now = database_now(db)
    organization_state_valid = organization is not None and (
        (invitation.purpose == "bootstrap" and organization.status in {"active", "trial"})
        or (invitation.purpose in {"invite", "recovery"} and organization.status == "active")
    )
    if (
        not organization_state_valid
        or invitation.used_at is not None
        or invitation.revoked_at is not None
        or as_utc(invitation.expires_at) <= now
        or invitation.attempt_count >= invitation.max_attempts
    ):
        outcome = "expired" if as_utc(invitation.expires_at) <= now else "invalid"
        _record_acceptance_rejection(db, invitation, outcome=outcome)

    staff: OrganizationStaff
    if invitation.purpose == "bootstrap":
        active_admin = db.query(OrganizationStaff).filter(
            OrganizationStaff.organization_id == invitation.organization_id,
            OrganizationStaff.role == "Organization Admin",
            OrganizationStaff.status == "active",
            OrganizationStaff.is_uat_fixture.is_(False),
        ).first()
        if active_admin:
            _record_acceptance_rejection(db, invitation, outcome="bootstrap_conflict")
        staff = OrganizationStaff(
            organization_id=invitation.organization_id,
            name="Organization Administrator",
            email=invitation.email,
            password_hash=hash_password(new_password),
            role="Organization Admin",
            status="active",
            is_temporary_password=False,
            credential_version=1,
        )
        action = "administrator.bootstrapped"
        db.add(staff)
        db.flush()
    elif invitation.purpose == "invite":
        existing = db.query(OrganizationStaff).filter(
            OrganizationStaff.organization_id == invitation.organization_id,
            func.lower(OrganizationStaff.email) == invitation.email,
        ).first()
        if existing:
            _record_acceptance_rejection(db, invitation, outcome="invite_conflict")
        staff = OrganizationStaff(
            organization_id=invitation.organization_id,
            name="Organization Administrator",
            email=invitation.email,
            password_hash=hash_password(new_password),
            role="Organization Admin",
            status="active",
            is_temporary_password=False,
            credential_version=1,
        )
        action = "administrator.invited"
        db.add(staff)
        db.flush()
    elif invitation.purpose == "recovery":
        staff_rows = (
            db.query(OrganizationStaff)
            .filter(
                OrganizationStaff.organization_id == invitation.organization_id,
                func.lower(OrganizationStaff.email) == invitation.email,
                OrganizationStaff.role == "Organization Admin",
                OrganizationStaff.status == "active",
                OrganizationStaff.is_uat_fixture.is_(False),
            )
            .with_for_update()
            .all()
        )
        if len(staff_rows) != 1:
            _record_acceptance_rejection(db, invitation, outcome="recovery_conflict")
        staff = staff_rows[0]
        staff.password_hash = hash_password(new_password)
        staff.is_temporary_password = False
        staff.credential_version += 1
        staff.credentials_revoked_at = now
        action = "administrator.recovered"
        record_audit(
            db,
            organization_id=invitation.organization_id,
            actor="invitation-acceptance",
            actor_type="system",
            action="administrator.sessions_revoked",
            target_type="organization_staff",
            target_id=str(staff.id),
            new_value={
                "purpose": "recovery",
                "correlation_id": invitation.correlation_id,
                "outcome": "credential_version_incremented",
            },
        )
    else:
        _record_acceptance_rejection(db, invitation, outcome="invalid_purpose")

    invitation.used_at = now
    record_audit(
        db,
        organization_id=invitation.organization_id,
        actor="invitation-acceptance",
        actor_type="system",
        action=action,
        target_type="organization_staff",
        target_id=str(staff.id),
        new_value={
            "purpose": invitation.purpose,
            "invitation_id": invitation.id,
            "correlation_id": invitation.correlation_id,
            "reason": invitation.reason,
            "outcome": "accepted",
        },
    )
    record_audit(
        db,
        organization_id=invitation.organization_id,
        actor="invitation-acceptance",
        actor_type="system",
        action="admin_invitation.accepted",
        target_type="organization_admin_invitation",
        target_id=str(invitation.id),
        new_value={
            "purpose": invitation.purpose,
            "correlation_id": invitation.correlation_id,
            "target_staff_id": staff.id,
            "outcome": "accepted",
        },
    )
    db.flush()
    return invitation, staff


def revoke_admin_invitation(
    db: Session,
    *,
    invitation: OrganizationAdminInvitation,
    principal: AuthenticatedPrincipal,
) -> bool:
    if invitation.used_at is not None:
        raise HTTPException(status_code=409, detail="Used invitations cannot be revoked")
    if invitation.revoked_at is not None:
        return False
    invitation.revoked_at = database_now(db)
    record_audit(
        db,
        organization_id=invitation.organization_id,
        actor=principal.actor_label,
        actor_type="platform_admin",
        actor_id=principal.subject_id,
        actor_label=principal.actor_label,
        action="admin_invitation.revoked",
        target_type="organization_admin_invitation",
        target_id=str(invitation.id),
        new_value={
            "purpose": invitation.purpose,
            "correlation_id": invitation.correlation_id,
            "reason": invitation.reason,
            "outcome": "revoked",
        },
    )
    return True
