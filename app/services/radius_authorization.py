from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import RadCheck, RadReply, RadiusRejectOwnership, ServicePlan, User
from app.services.expiry_policy import AccessDecision, evaluate_access


PASSWORD_ATTRIBUTE = "Cleartext-Password"
REJECT_ATTRIBUTE = "Auth-Type"
REJECT_VALUE = "Reject"
REJECT_OWNER = "radiusfiber_access_policy"
EXPIRATION_ATTRIBUTE = "Expiration"
MIKROTIK_RATE_LIMIT_ATTRIBUTE = "Mikrotik-Rate-Limit"


def _upsert_unique(db: Session, model, username: str, attribute: str, value: str, op: str = ":="):
    rows = (
        db.query(model)
        .filter(model.username == username, model.attribute == attribute)
        .order_by(model.id.asc())
        .all()
    )
    if rows:
        row = rows[0]
        row.op = op
        row.value = value
        for duplicate in rows[1:]:
            db.delete(duplicate)
        return row
    row = model(username=username, attribute=attribute, op=op, value=value)
    db.add(row)
    return row


def upsert_radcheck(db: Session, username: str, attribute: str, value: str, op: str = ":=") -> RadCheck:
    return _upsert_unique(db, RadCheck, username, attribute, value, op)


def upsert_radreply(db: Session, username: str, attribute: str, value: str, op: str = ":=") -> RadReply:
    return _upsert_unique(db, RadReply, username, attribute, value, op)


def remove_radcheck_attribute(db: Session, username: str, attribute: str) -> None:
    for row in db.query(RadCheck).filter(RadCheck.username == username, RadCheck.attribute == attribute).all():
        db.delete(row)


def remove_radreply_attribute(db: Session, username: str, attribute: str) -> None:
    for row in db.query(RadReply).filter(RadReply.username == username, RadReply.attribute == attribute).all():
        db.delete(row)


def expiration_radius_value(value: datetime) -> str:
    utc_value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return utc_value.strftime("%b %d %Y %H:%M:%S UTC")


def _owned_reject(db: Session, service: User) -> RadiusRejectOwnership | None:
    return (
        db.query(RadiusRejectOwnership)
        .filter(
            RadiusRejectOwnership.organization_id == service.organization_id,
            RadiusRejectOwnership.user_id == service.id,
        )
        .with_for_update()
        .one_or_none()
    )


def _ensure_owned_policy_reject(db: Session, service: User, reason_code: str) -> None:
    ownership = _owned_reject(db, service)
    if ownership is not None:
        row = db.query(RadCheck).filter(RadCheck.id == ownership.radcheck_id).one_or_none()
        if (
            row is not None
            and row.username == ownership.username == service.username
            and row.attribute == REJECT_ATTRIBUTE
        ):
            row.op = ":="
            row.value = REJECT_VALUE
            ownership.reason_code = reason_code
            return
        db.delete(ownership)
        db.flush()

    existing_reject = (
        db.query(RadCheck)
        .filter(
            RadCheck.username == service.username,
            RadCheck.attribute == REJECT_ATTRIBUTE,
            RadCheck.value == REJECT_VALUE,
        )
        .first()
    )
    if existing_reject is not None:
        # A pre-existing reject has unknown/manual ownership. Never claim it.
        return

    row = RadCheck(
        username=service.username,
        attribute=REJECT_ATTRIBUTE,
        op=":=",
        value=REJECT_VALUE,
    )
    db.add(row)
    db.flush()
    db.add(
        RadiusRejectOwnership(
            organization_id=service.organization_id,
            user_id=service.id,
            radcheck_id=row.id,
            username=service.username,
            owner=REJECT_OWNER,
            reason_code=reason_code,
        )
    )


def _remove_owned_policy_reject(db: Session, service: User) -> None:
    ownership = _owned_reject(db, service)
    if ownership is None:
        return
    row = db.query(RadCheck).filter(RadCheck.id == ownership.radcheck_id).one_or_none()
    if (
        row is not None
        and row.username == ownership.username == service.username
        and row.attribute == REJECT_ATTRIBUTE
    ):
        db.delete(row)
    db.delete(ownership)


def delete_radius_provisioning(db: Session, username: str) -> None:
    for row in db.query(RadCheck).filter(RadCheck.username == username).all():
        db.delete(row)
    for row in db.query(RadReply).filter(RadReply.username == username).all():
        db.delete(row)


def synchronize_radius_authorization(
    db: Session,
    service: User,
    plan: ServicePlan | None,
    *,
    now: datetime | None = None,
    customer_status: str = "active",
    organization_status: str = "active",
) -> AccessDecision:
    current = now or datetime.now(timezone.utc)
    decision = evaluate_access(
        service,
        plan,
        now=current,
        organization_id=service.organization_id,
        customer_status=customer_status,
        organization_status=organization_status,
    )

    if service.status in {"pending", "terminated"}:
        delete_radius_provisioning(db, service.username)
        # Materialize cascades before creating the replacement ownership row.
        db.flush()
        _ensure_owned_policy_reject(db, service, decision.reason.value)
        return decision

    if plan is not None:
        upsert_radcheck(db, service.username, PASSWORD_ATTRIBUTE, service.password)
        upsert_radreply(db, service.username, MIKROTIK_RATE_LIMIT_ATTRIBUTE, plan.rate_limit)
    if decision.expires_at is not None:
        upsert_radcheck(
            db,
            service.username,
            EXPIRATION_ATTRIBUTE,
            expiration_radius_value(decision.expires_at),
        )
    else:
        remove_radcheck_attribute(db, service.username, EXPIRATION_ATTRIBUTE)

    if decision.eligible:
        _remove_owned_policy_reject(db, service)
    else:
        _ensure_owned_policy_reject(db, service, decision.reason.value)
    return decision
