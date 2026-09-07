from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import RadCheck, RadReply, RadiusRejectOwnership, ServicePlan, User
from app.services.expiry_policy import AccessDecision, AccessReason, evaluate_access


PASSWORD_ATTRIBUTE = "Cleartext-Password"
REJECT_ATTRIBUTE = "Auth-Type"
REJECT_VALUE = "Reject"
REJECT_OWNER = "radiusfiber_access_policy"
MANUAL_DISCONNECT_REASON = "MANUAL_DISCONNECT"
EXPIRATION_ATTRIBUTE = "Expiration"
MIKROTIK_RATE_LIMIT_ATTRIBUTE = "Mikrotik-Rate-Limit"
POLICY_REJECT_REASONS = frozenset(
    reason.value for reason in AccessReason if reason is not AccessReason.ACTIVE
)


class RejectOwnershipConflict(RuntimeError):
    def __init__(self, message: str, *, radcheck_ids: tuple[int, ...] = ()) -> None:
        super().__init__(message)
        self.radcheck_ids = radcheck_ids


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


def _owned_rejects(
    db: Session,
    service: User,
    *,
    lock_rows: bool = True,
) -> list[RadiusRejectOwnership]:
    query = (
        db.query(RadiusRejectOwnership)
        .filter(
            RadiusRejectOwnership.organization_id == service.organization_id,
            RadiusRejectOwnership.user_id == service.id,
        )
        .order_by(RadiusRejectOwnership.id.asc())
    )
    if lock_rows:
        query = query.with_for_update()
    return query.all()


def _reject_rows(db: Session, service: User, *, lock_rows: bool = True) -> list[RadCheck]:
    query = (
        db.query(RadCheck)
        .filter(
            RadCheck.username == service.username,
            RadCheck.attribute == REJECT_ATTRIBUTE,
            RadCheck.value == REJECT_VALUE,
        )
        .order_by(RadCheck.id.asc())
    )
    if lock_rows:
        query = query.with_for_update()
    return query.all()


def _validated_owned_rejects(
    db: Session,
    service: User,
    *,
    lock_rows: bool = True,
) -> tuple[list[RadiusRejectOwnership], list[RadCheck]]:
    ownerships = _owned_rejects(db, service, lock_rows=lock_rows)
    rows = _reject_rows(db, service, lock_rows=lock_rows)
    rows_by_id = {row.id: row for row in rows}
    for ownership in ownerships:
        row = rows_by_id.get(ownership.radcheck_id)
        if (
            row is None
            or row.username != ownership.username
            or ownership.username != service.username
            or row.op != ":="
        ):
            raise RejectOwnershipConflict(
                "RADIUS reject ownership metadata does not match its effective reject row",
                radcheck_ids=(ownership.radcheck_id,),
            )
    owned_ids = {ownership.radcheck_id for ownership in ownerships}
    unowned_ids = tuple(row.id for row in rows if row.id not in owned_ids)
    if unowned_ids:
        raise RejectOwnershipConflict(
            "An effective RADIUS reject has unknown ownership and requires reconciliation",
            radcheck_ids=unowned_ids,
        )
    return ownerships, rows


def ensure_owned_reject(db: Session, service: User, reason_code: str) -> RadiusRejectOwnership:
    ownerships, rows = _validated_owned_rejects(db, service)
    for ownership in ownerships:
        if ownership.reason_code == reason_code:
            row = next(row for row in rows if row.id == ownership.radcheck_id)
            row.op = ":="
            row.value = REJECT_VALUE
            return ownership

    row = RadCheck(
        username=service.username,
        attribute=REJECT_ATTRIBUTE,
        op=":=",
        value=REJECT_VALUE,
    )
    db.add(row)
    db.flush()
    ownership = RadiusRejectOwnership(
        organization_id=service.organization_id,
        user_id=service.id,
        radcheck_id=row.id,
        username=service.username,
        owner=REJECT_OWNER,
        reason_code=reason_code,
    )
    db.add(ownership)
    return ownership


def remove_owned_rejects(
    db: Session,
    service: User,
    *,
    reason_codes: set[str] | frozenset[str],
) -> int:
    ownerships, rows = _validated_owned_rejects(db, service)
    rows_by_id = {row.id: row for row in rows}
    removed = 0
    for ownership in ownerships:
        if ownership.reason_code not in reason_codes:
            continue
        db.delete(rows_by_id[ownership.radcheck_id])
        db.delete(ownership)
        removed += 1
    return removed


def effective_reject_ids(db: Session, service: User) -> tuple[int, ...]:
    return tuple(row.id for row in _reject_rows(db, service, lock_rows=False))


def owned_reject_reasons(
    db: Session,
    service: User,
    *,
    lock_rows: bool = False,
) -> frozenset[str]:
    ownerships, _ = _validated_owned_rejects(db, service, lock_rows=lock_rows)
    return frozenset(ownership.reason_code for ownership in ownerships)


def delete_radius_provisioning(db: Session, username: str) -> None:
    for row in db.query(RadCheck).filter(
        RadCheck.username == username,
        RadCheck.attribute != REJECT_ATTRIBUTE,
    ).all():
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
        remove_owned_rejects(
            db,
            service,
            reason_codes=POLICY_REJECT_REASONS - {decision.reason.value},
        )
        ensure_owned_reject(db, service, decision.reason.value)
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
        remove_owned_rejects(db, service, reason_codes=POLICY_REJECT_REASONS)
    else:
        remove_owned_rejects(
            db,
            service,
            reason_codes=POLICY_REJECT_REASONS - {decision.reason.value},
        )
        ensure_owned_reject(db, service, decision.reason.value)
    return decision
