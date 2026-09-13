from __future__ import annotations

from dataclasses import asdict, dataclass
import re

from sqlalchemy.orm import Session

from app.models import RadCheck, RadiusRejectOwnership, User
from app.services.audit import record_audit
from app.services.radius_authorization import (
    MANUAL_DISCONNECT_REASON,
    POLICY_REJECT_REASONS,
    REJECT_ATTRIBUTE,
    REJECT_OWNER,
    REJECT_VALUE,
    RejectOwnershipConflict,
)


ALLOWED_ADOPTION_REASONS = POLICY_REJECT_REASONS | {MANUAL_DISCONNECT_REASON}
EVIDENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{7,127}$")


@dataclass(frozen=True)
class RejectReconciliationReport:
    organization_id: int
    user_id: int
    username: str
    radcheck_id: int
    ownership_state: str
    reason_code: str | None
    action: str
    applied: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _exact_service_and_reject(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    username: str,
    radcheck_id: int,
    lock_rows: bool,
) -> tuple[User, RadCheck, RadiusRejectOwnership | None]:
    service_query = db.query(User).filter(
        User.organization_id == organization_id,
        User.id == user_id,
        User.username == username,
    )
    reject_query = db.query(RadCheck).filter(
        RadCheck.id == radcheck_id,
        RadCheck.username == username,
        RadCheck.attribute == REJECT_ATTRIBUTE,
        RadCheck.value == REJECT_VALUE,
    )
    if lock_rows:
        service_query = service_query.with_for_update()
        reject_query = reject_query.with_for_update()
    service = service_query.one_or_none()
    reject = reject_query.one_or_none()
    if service is None or reject is None:
        raise RejectOwnershipConflict(
            "The exact tenant, service, username, and reject row did not match",
            radcheck_ids=(radcheck_id,),
        )
    ownership_query = (
        db.query(RadiusRejectOwnership)
        .filter(RadiusRejectOwnership.radcheck_id == radcheck_id)
    )
    if lock_rows:
        ownership_query = ownership_query.with_for_update()
    ownership = ownership_query.one_or_none()
    if ownership is not None and (
        ownership.organization_id != organization_id
        or ownership.user_id != user_id
        or ownership.username != username
    ):
        raise RejectOwnershipConflict(
            "The reject ownership record does not match the exact tenant service",
            radcheck_ids=(radcheck_id,),
        )
    return service, reject, ownership


def reconcile_legacy_reject(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    username: str,
    radcheck_id: int,
    action: str = "report",
    reason_code: str = MANUAL_DISCONNECT_REASON,
    evidence_reference: str | None = None,
    operator_id: str | None = None,
    apply: bool = False,
) -> RejectReconciliationReport:
    if action not in {"report", "adopt", "remove"}:
        raise ValueError("action must be report, adopt, or remove")
    if reason_code not in ALLOWED_ADOPTION_REASONS:
        raise ValueError("reason_code is not an application-managed reject reason")
    if apply and action == "report":
        raise ValueError("report is always read-only")
    if apply and (
        evidence_reference is None
        or not EVIDENCE_PATTERN.fullmatch(evidence_reference)
        or operator_id is None
        or not EVIDENCE_PATTERN.fullmatch(operator_id)
    ):
        raise ValueError("an auditable evidence reference and operator ID are required")

    _, reject, ownership = _exact_service_and_reject(
        db,
        organization_id=organization_id,
        user_id=user_id,
        username=username,
        radcheck_id=radcheck_id,
        lock_rows=apply,
    )
    state = "owned" if ownership is not None else "unowned_conflict"
    existing_reason = ownership.reason_code if ownership is not None else None
    if not apply or action == "report":
        return RejectReconciliationReport(
            organization_id, user_id, username, radcheck_id,
            state, existing_reason, action, False,
        )

    if action == "adopt":
        if ownership is None:
            duplicate_reason = db.query(RadiusRejectOwnership).filter(
                RadiusRejectOwnership.organization_id == organization_id,
                RadiusRejectOwnership.user_id == user_id,
                RadiusRejectOwnership.reason_code == reason_code,
            ).with_for_update().one_or_none()
            if duplicate_reason is not None:
                raise RejectOwnershipConflict(
                    "The service already owns a different reject row for this reason",
                    radcheck_ids=(duplicate_reason.radcheck_id, radcheck_id),
                )
            db.add(RadiusRejectOwnership(
                organization_id=organization_id,
                user_id=user_id,
                radcheck_id=reject.id,
                username=username,
                owner=REJECT_OWNER,
                reason_code=reason_code,
            ))
        elif ownership.reason_code != reason_code:
            raise RejectOwnershipConflict(
                "The reject is already owned for a different reason",
                radcheck_ids=(radcheck_id,),
            )
        resulting_state = "owned"
        resulting_reason = reason_code
    else:
        if ownership is not None:
            db.delete(ownership)
            db.flush()
        db.delete(reject)
        resulting_state = "removed"
        resulting_reason = existing_reason

    record_audit(
        db,
        organization_id=organization_id,
        actor="system",
        actor_type="system",
        actor_id=operator_id,
        actor_label="reject-reconciliation-operator",
        action=f"radius.reject_legacy_{action}",
        target_type="radcheck",
        target_id=str(radcheck_id),
        old_value={"ownership_state": state, "reason_code": existing_reason},
        new_value={
            "ownership_state": resulting_state,
            "reason_code": resulting_reason,
            "evidence_reference": evidence_reference,
        },
    )
    return RejectReconciliationReport(
        organization_id, user_id, username, radcheck_id,
        resulting_state, resulting_reason, action, True,
    )
