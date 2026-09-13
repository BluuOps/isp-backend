from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.authorization import Permission, require_permission
from app.core.tenant import OrganizationContext, get_organization_context
from app.database import get_db
from app.models import NetworkAccessServer, RadAcct, User, Zone
from app.schemas.network import (
    NasCreate,
    NasResponse,
    NasStatusUpdate,
    NasUpdate,
    ZoneCreate,
    ZoneDetailResponse,
    ZoneResponse,
    ZoneUpdate,
)
from app.services.audit import record_audit
from app.services.radius_session_freshness import fresh_active_session_conditions


router = APIRouter(
    prefix="/organization",
    tags=["Organization Network Inventory"],
)


def _audit_actor(context: OrganizationContext) -> dict[str, str]:
    return {
        "actor": "organization-admin",
        "actor_id": context.principal_id,
        "actor_label": context.principal_id or "organization-admin",
    }


def _zone_or_404(zone_id: int, db: Session, context: OrganizationContext) -> Zone:
    zone = db.query(Zone).filter(Zone.id == zone_id, Zone.organization_id == context.id).first()
    if zone:
        return zone

    cross_tenant = db.query(Zone.id).filter(Zone.id == zone_id).first()
    if cross_tenant:
        record_audit(
            db,
            organization_id=context.id,
            action="zone.cross_tenant_access_rejected",
            target_type="zone",
            target_id=str(zone_id),
            success=False,
            **_audit_actor(context),
        )
        db.commit()
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Zone not found")


def _nas_or_404(nas_id: int, db: Session, context: OrganizationContext) -> NetworkAccessServer:
    nas = db.query(NetworkAccessServer).filter(
        NetworkAccessServer.id == nas_id,
        NetworkAccessServer.organization_id == context.id,
    ).first()
    if nas:
        return nas

    cross_tenant = db.query(NetworkAccessServer.id).filter(NetworkAccessServer.id == nas_id).first()
    if cross_tenant:
        record_audit(
            db,
            organization_id=context.id,
            action="nas.cross_tenant_access_rejected",
            target_type="network_access_server",
            target_id=str(nas_id),
            success=False,
            **_audit_actor(context),
        )
        db.commit()
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NAS metadata not found")


def _active_session_count(db: Session, organization_id: int, nas_ip_address: object) -> int:
    return (
        db.query(RadAcct)
        .join(User, User.username == RadAcct.username)
        .filter(
            RadAcct.nasipaddress == nas_ip_address,
            *fresh_active_session_conditions(),
            User.organization_id == organization_id,
        )
        .count()
    )


def _accounting_record_count(db: Session, organization_id: int, nas_ip_address: object) -> int:
    return (
        db.query(RadAcct)
        .join(User, User.username == RadAcct.username)
        .filter(
            RadAcct.nasipaddress == nas_ip_address,
            User.organization_id == organization_id,
        )
        .count()
    )


def _nas_response(nas: NetworkAccessServer, zone: Zone, db: Session) -> NasResponse:
    return NasResponse(
        id=nas.id,
        organization_id=nas.organization_id,
        zone_id=nas.zone_id,
        zone_name=zone.name,
        nas_ip_address=str(nas.nas_ip_address),
        display_name=nas.display_name,
        short_name=nas.short_name,
        device_type=nas.device_type,
        status=nas.status,
        description=nas.description,
        active_session_count=_active_session_count(db, nas.organization_id, nas.nas_ip_address),
        accounting_record_count=_accounting_record_count(db, nas.organization_id, nas.nas_ip_address),
        created_at=nas.created_at,
        updated_at=nas.updated_at,
    )


def _zone_response(zone: Zone, db: Session, *, detail: bool = False) -> ZoneResponse | ZoneDetailResponse:
    nas_devices = db.query(NetworkAccessServer).filter(
        NetworkAccessServer.organization_id == zone.organization_id,
        NetworkAccessServer.zone_id == zone.id,
    ).order_by(NetworkAccessServer.id).all()
    nas_responses = [_nas_response(nas, zone, db) for nas in nas_devices]
    data = {
        "id": zone.id,
        "organization_id": zone.organization_id,
        "name": zone.name,
        "status": zone.status,
        "nas_count": len(nas_devices),
        "user_count": db.query(User).filter(
            User.organization_id == zone.organization_id,
            User.zone == zone.name,
        ).count(),
        "active_session_count": sum(item.active_session_count for item in nas_responses),
        "created_at": zone.created_at,
    }
    if detail:
        return ZoneDetailResponse(**data, nas_devices=nas_responses)
    return ZoneResponse(**data)


@router.get(
    "/zones",
    response_model=list[ZoneResponse],
    dependencies=[Depends(require_permission(Permission.NETWORK_ZONES_READ))],
)
def list_zones(
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
) -> list[ZoneResponse]:
    zones = db.query(Zone).filter(Zone.organization_id == context.id).order_by(Zone.name).all()
    return [_zone_response(zone, db) for zone in zones]


@router.get(
    "/zones/{zone_id}",
    response_model=ZoneDetailResponse,
    dependencies=[Depends(require_permission(Permission.NETWORK_ZONES_READ))],
)
def get_zone(
    zone_id: int,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
) -> ZoneDetailResponse:
    return _zone_response(_zone_or_404(zone_id, db, context), db, detail=True)


@router.post(
    "/zones",
    response_model=ZoneDetailResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.NETWORK_ZONES_CREATE))],
)
def create_zone(
    payload: ZoneCreate,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
) -> ZoneDetailResponse:
    zone = Zone(organization_id=context.id, **payload.model_dump())
    try:
        db.add(zone)
        db.flush()
        record_audit(
            db,
            organization_id=context.id,
            action="zone.created",
            target_type="zone",
            target_id=str(zone.id),
            new_value={"name": zone.name, "status": zone.status},
            **_audit_actor(context),
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Zone name already exists") from exc
    db.refresh(zone)
    return _zone_response(zone, db, detail=True)


@router.put(
    "/zones/{zone_id}",
    response_model=ZoneDetailResponse,
    dependencies=[Depends(require_permission(Permission.NETWORK_ZONES_UPDATE))],
)
def update_zone(
    zone_id: int,
    payload: ZoneUpdate,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
) -> ZoneDetailResponse:
    zone = _zone_or_404(zone_id, db, context)
    changes = payload.model_dump(exclude_unset=True)
    old = {key: getattr(zone, key) for key in changes}
    old_name = zone.name
    for key, value in changes.items():
        setattr(zone, key, value)
    if "name" in changes and changes["name"] != old_name:
        db.query(User).filter(User.organization_id == context.id, User.zone == old_name).update(
            {User.zone: changes["name"]}, synchronize_session=False
        )
    try:
        record_audit(
            db,
            organization_id=context.id,
            action="zone.status_changed" if set(changes) == {"status"} else "zone.updated",
            target_type="zone",
            target_id=str(zone.id),
            old_value=old,
            new_value=changes,
            **_audit_actor(context),
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Zone update conflicts with an existing zone") from exc
    db.refresh(zone)
    return _zone_response(zone, db, detail=True)


@router.delete(
    "/zones/{zone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission(Permission.NETWORK_ZONES_DELETE))],
)
def delete_zone(
    zone_id: int,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
) -> Response:
    zone = _zone_or_404(zone_id, db, context)
    linked_nas = db.query(NetworkAccessServer.id).filter(
        NetworkAccessServer.organization_id == context.id,
        NetworkAccessServer.zone_id == zone.id,
    ).first()
    linked_user = db.query(User.id).filter(User.organization_id == context.id, User.zone == zone.name).first()
    if linked_nas or linked_user:
        record_audit(
            db,
            organization_id=context.id,
            action="zone.delete_rejected",
            target_type="zone",
            target_id=str(zone.id),
            old_value={"name": zone.name, "status": zone.status},
            success=False,
            **_audit_actor(context),
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Zone has linked NAS devices or PPPoE users")
    record_audit(
        db,
        organization_id=context.id,
        action="zone.deleted",
        target_type="zone",
        target_id=str(zone.id),
        old_value={"name": zone.name, "status": zone.status},
        **_audit_actor(context),
    )
    db.delete(zone)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/nas",
    response_model=list[NasResponse],
    dependencies=[Depends(require_permission(Permission.NETWORK_NAS_READ))],
)
def list_nas(
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
) -> list[NasResponse]:
    rows = (
        db.query(NetworkAccessServer, Zone)
        .join(Zone, Zone.id == NetworkAccessServer.zone_id)
        .filter(
            NetworkAccessServer.organization_id == context.id,
            Zone.organization_id == context.id,
        )
        .order_by(NetworkAccessServer.display_name)
        .all()
    )
    return [_nas_response(nas, zone, db) for nas, zone in rows]


@router.get(
    "/nas/{nas_id}",
    response_model=NasResponse,
    dependencies=[Depends(require_permission(Permission.NETWORK_NAS_READ))],
)
def get_nas(
    nas_id: int,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
) -> NasResponse:
    nas = _nas_or_404(nas_id, db, context)
    zone = _zone_or_404(nas.zone_id, db, context)
    return _nas_response(nas, zone, db)


@router.post(
    "/nas",
    response_model=NasResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.NETWORK_NAS_CREATE))],
)
def create_nas(
    payload: NasCreate,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
) -> NasResponse:
    zone = _zone_or_404(payload.zone_id, db, context)
    nas = NetworkAccessServer(organization_id=context.id, **payload.model_dump())
    try:
        db.add(nas)
        db.flush()
        record_audit(
            db,
            organization_id=context.id,
            action="nas.created",
            target_type="network_access_server",
            target_id=str(nas.id),
            new_value={
                "zone_id": nas.zone_id,
                "nas_ip_address": str(nas.nas_ip_address),
                "display_name": nas.display_name,
                "short_name": nas.short_name,
                "device_type": nas.device_type,
                "status": nas.status,
            },
            **_audit_actor(context),
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="NAS IP already exists for this organization") from exc
    db.refresh(nas)
    return _nas_response(nas, zone, db)


@router.put(
    "/nas/{nas_id}",
    response_model=NasResponse,
    dependencies=[Depends(require_permission(Permission.NETWORK_NAS_UPDATE))],
)
def update_nas(
    nas_id: int,
    payload: NasUpdate,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
) -> NasResponse:
    nas = _nas_or_404(nas_id, db, context)
    changes = payload.model_dump(exclude_unset=True)
    if "zone_id" in changes:
        zone = _zone_or_404(changes["zone_id"], db, context)
    else:
        zone = _zone_or_404(nas.zone_id, db, context)
    old = {key: str(getattr(nas, key)) if key == "nas_ip_address" else getattr(nas, key) for key in changes}
    for key, value in changes.items():
        setattr(nas, key, value)
    action = "nas.zone_reassigned" if set(changes) == {"zone_id"} else "nas.updated"
    if set(changes) == {"status"}:
        action = "nas.status_changed"
    try:
        record_audit(
            db,
            organization_id=context.id,
            action=action,
            target_type="network_access_server",
            target_id=str(nas.id),
            old_value=old,
            new_value=changes,
            **_audit_actor(context),
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="NAS update conflicts with existing metadata") from exc
    db.refresh(nas)
    return _nas_response(nas, zone, db)


@router.patch(
    "/nas/{nas_id}/status",
    response_model=NasResponse,
    dependencies=[Depends(require_permission(Permission.NETWORK_NAS_UPDATE))],
)
def update_nas_status(
    nas_id: int,
    payload: NasStatusUpdate,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
) -> NasResponse:
    return update_nas(nas_id, NasUpdate(status=payload.status), db, context)


@router.delete(
    "/nas/{nas_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission(Permission.NETWORK_NAS_DELETE))],
)
def delete_nas(
    nas_id: int,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
) -> Response:
    nas = _nas_or_404(nas_id, db, context)
    accounting_count = _accounting_record_count(db, context.id, nas.nas_ip_address)
    if accounting_count:
        record_audit(
            db,
            organization_id=context.id,
            action="nas.delete_rejected",
            target_type="network_access_server",
            target_id=str(nas.id),
            old_value={"nas_ip_address": str(nas.nas_ip_address), "accounting_record_count": accounting_count},
            success=False,
            **_audit_actor(context),
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="NAS metadata has linked accounting records")
    record_audit(
        db,
        organization_id=context.id,
        action="nas.deleted",
        target_type="network_access_server",
        target_id=str(nas.id),
        old_value={"nas_ip_address": str(nas.nas_ip_address), "display_name": nas.display_name},
        **_audit_actor(context),
    )
    db.delete(nas)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
