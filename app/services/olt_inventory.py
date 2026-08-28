from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import asc, desc, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.authorization import AuthenticatedPrincipal
from app.core.config import settings
from app.models import (
    AuditLog,
    Customer,
    OltCard,
    OltCredentialReference,
    OltDevice,
    OltOnu,
    OltPollRun,
    OltPonPort,
    OltServiceAssociation,
    OltUplink,
    User,
)
from app.schemas.olt import FreshnessMetadata, OltDeviceCreate, OltDeviceResponse, OltDeviceUpdate
from app.services.audit import record_audit
from app.services.security_events import emit_olt_access_denial


DEVICE_SORTS = {"id": OltDevice.id, "name": OltDevice.name, "status": OltDevice.status, "created_at": OltDevice.created_at}
INVENTORY_MODELS = {
    "cards": OltCard,
    "uplinks": OltUplink,
    "pon-ports": OltPonPort,
    "onus": OltOnu,
    "poll-runs": OltPollRun,
}
STALE_CACHE_STATUSES = frozenset({"empty", "stale", "unavailable"})


def bounded_pagination(limit: int, offset: int) -> tuple[int, int]:
    if limit < 1 or limit > 200 or offset < 0:
        raise HTTPException(status_code=422, detail="Invalid pagination")
    return limit, offset


def device_or_404(
    db: Session,
    organization_id: int,
    device_id: int,
    *,
    request: Request | None = None,
    principal: AuthenticatedPrincipal | None = None,
) -> OltDevice:
    device = db.query(OltDevice).filter(OltDevice.id == device_id, OltDevice.organization_id == organization_id).first()
    if device:
        return device
    if request is not None and principal is not None:
        emit_olt_access_denial(
            request,
            principal_type=principal.principal_type.value,
            subject_id=principal.subject_id,
            organization_id=organization_id,
            resource_type="olt_device",
            resource_id=device_id,
            route_template="/olt/v1/devices/{device_id}",
        )
    raise HTTPException(status_code=404, detail="OLT device not found")


def device_response(device: OltDevice) -> OltDeviceResponse:
    now = datetime.now(timezone.utc)
    observed_at = device.observed_at
    age_seconds = int((now - observed_at).total_seconds()) if observed_at else None
    is_stale = device.cache_status in STALE_CACHE_STATUSES
    return OltDeviceResponse(
        **{column.name: getattr(device, column.name) for column in OltDevice.__table__.columns},
        freshness=FreshnessMetadata(
            observed_at=observed_at,
            last_success_at=device.last_poll_success_at,
            age_seconds=age_seconds,
            is_stale=is_stale,
            source_status=device.cache_status,
        ),
    )


def list_devices(
    db: Session,
    organization_id: int,
    *,
    limit: int,
    offset: int,
    status_filter: str | None,
    sort_by: str,
    sort_order: str,
) -> tuple[list[OltDevice], int]:
    limit, offset = bounded_pagination(limit, offset)
    if sort_by not in DEVICE_SORTS or sort_order not in {"asc", "desc"}:
        raise HTTPException(status_code=422, detail="Invalid OLT device sort")
    query = db.query(OltDevice).filter(OltDevice.organization_id == organization_id)
    if status_filter is not None:
        if status_filter not in {"active", "disabled"}:
            raise HTTPException(status_code=422, detail="Invalid OLT device status filter")
        query = query.filter(OltDevice.status == status_filter)
    total = query.count()
    order = asc(DEVICE_SORTS[sort_by]) if sort_order == "asc" else desc(DEVICE_SORTS[sort_by])
    return query.order_by(order, OltDevice.id).offset(offset).limit(limit).all(), total


def create_device(
    db: Session,
    organization_id: int,
    payload: OltDeviceCreate,
    principal: AuthenticatedPrincipal,
) -> OltDevice:
    if payload.credential_reference_id is not None:
        reference = db.query(OltCredentialReference).filter(
            OltCredentialReference.id == payload.credential_reference_id,
            OltCredentialReference.organization_id == organization_id,
        ).first()
        if not reference:
            raise HTTPException(status_code=404, detail="Credential reference not found")
    device = OltDevice(organization_id=organization_id, **payload.model_dump())
    db.add(device)
    try:
        db.flush()
        record_audit(
            db,
            organization_id=organization_id,
            actor_type=principal.principal_type.value,
            actor_id=principal.subject_id,
            actor_label=principal.actor_label,
            action="olt.device.created",
            target_type="olt_device",
            target_id=str(device.id),
            new_value={"name": device.name, "management_address": device.management_address, "credential_reference_id": device.credential_reference_id},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="OLT device metadata conflicts with an existing device") from exc
    db.refresh(device)
    return device


def update_device(
    db: Session,
    device: OltDevice,
    payload: OltDeviceUpdate,
    principal: AuthenticatedPrincipal,
) -> OltDevice:
    changes = payload.model_dump(exclude_unset=True)
    if "credential_reference_id" in changes and changes["credential_reference_id"] is not None:
        reference = db.query(OltCredentialReference).filter(
            OltCredentialReference.id == changes["credential_reference_id"],
            OltCredentialReference.organization_id == device.organization_id,
        ).first()
        if not reference:
            raise HTTPException(status_code=404, detail="Credential reference not found")
    old = {key: getattr(device, key) for key in changes}
    for key, value in changes.items():
        setattr(device, key, value)
    try:
        record_audit(
            db,
            organization_id=device.organization_id,
            actor_type=principal.principal_type.value,
            actor_id=principal.subject_id,
            actor_label=principal.actor_label,
            action="olt.device.updated",
            target_type="olt_device",
            target_id=str(device.id),
            old_value=old,
            new_value=changes,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="OLT device metadata conflicts with an existing device") from exc
    db.refresh(device)
    return device


def inventory_page(db: Session, organization_id: int, resource: str, device_id: int, limit: int, offset: int):
    limit, offset = bounded_pagination(limit, offset)
    model = INVENTORY_MODELS.get(resource)
    if model is None:
        raise HTTPException(status_code=422, detail="Invalid OLT inventory resource")
    query = db.query(model).filter(model.organization_id == organization_id, model.olt_device_id == device_id)
    total = query.count()
    return query.order_by(model.id).offset(offset).limit(limit).all(), total


def overview(db: Session, organization_id: int) -> dict[str, object]:
    device_count = db.query(func.count(OltDevice.id)).filter(OltDevice.organization_id == organization_id).scalar() or 0
    fresh_count = db.query(func.count(OltDevice.id)).filter(OltDevice.organization_id == organization_id, OltDevice.cache_status == "fresh").scalar() or 0
    stale_count = db.query(func.count(OltDevice.id)).filter(OltDevice.organization_id == organization_id, OltDevice.cache_status.in_(STALE_CACHE_STATUSES)).scalar() or 0
    onu_count = db.query(func.count(OltOnu.id)).filter(OltOnu.organization_id == organization_id).scalar() or 0
    association_count = db.query(func.count(OltServiceAssociation.id)).filter(OltServiceAssociation.organization_id == organization_id, OltServiceAssociation.status == "active").scalar() or 0
    return {
        "organization_id": organization_id,
        "device_count": device_count,
        "fresh_device_count": fresh_count,
        "stale_device_count": stale_count,
        "onu_count": onu_count,
        "active_association_count": association_count,
        "operational_integration_enabled": settings.olt_integration_enabled,
    }


def create_association(db: Session, organization_id: int, payload, principal: AuthenticatedPrincipal) -> OltServiceAssociation:
    onu = db.query(OltOnu).filter(OltOnu.id == payload.onu_id, OltOnu.organization_id == organization_id).first()
    customer = db.query(Customer).filter(Customer.id == payload.customer_id, Customer.organization_id == organization_id).first()
    user = db.query(User).filter(User.id == payload.user_id, User.organization_id == organization_id).first()
    if not onu or not customer or not user:
        raise HTTPException(status_code=404, detail="OLT association target not found")
    if user.customer_id != customer.id:
        raise HTTPException(status_code=409, detail="PPPoE service is not linked to the selected customer")
    association = OltServiceAssociation(
        organization_id=organization_id,
        verified_by=principal.subject_id,
        verified_at=datetime.now(timezone.utc),
        **payload.model_dump(),
    )
    db.add(association)
    try:
        db.flush()
        record_audit(
            db,
            organization_id=organization_id,
            actor_type=principal.principal_type.value,
            actor_id=principal.subject_id,
            actor_label=principal.actor_label,
            action="olt.association.created",
            target_type="olt_service_association",
            target_id=str(association.id),
            new_value={"onu_id": association.onu_id, "customer_id": association.customer_id, "user_id": association.user_id},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="An active OLT association already exists") from exc
    db.refresh(association)
    return association
