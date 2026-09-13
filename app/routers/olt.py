from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.core.authorization import AuthenticatedPrincipal, Permission, require_permission
from app.core.platform_auth import require_platform_admin
from app.core.principal import reject_customer_principal
from app.core.tenant import OrganizationContext, get_organization_context
from app.database import get_db
from app.models import AuditLog, OltOnu, OltServiceAssociation, Organization
from app.schemas.audit import AuditLogListResponse, AuditLogResponse
from app.schemas.olt import (
    OltAssociationListResponse,
    OltDeviceCreate,
    OltDeviceListResponse,
    OltDeviceResponse,
    OltDeviceUpdate,
    OltDisabledOperationResponse,
    OltInventoryListResponse,
    OltOverviewResponse,
    OltServiceAssociationCreate,
    OltServiceAssociationResponse,
)
from app.services.audit import record_audit
from app.services.olt_inventory import (
    bounded_pagination,
    create_association,
    create_device,
    device_or_404,
    device_response,
    inventory_page,
    list_devices,
    overview,
    update_device,
)


organization_router = APIRouter(
    prefix="/organization/olt/v1",
    tags=["OLT Inventory"],
    dependencies=[Depends(reject_customer_principal)],
)
platform_router = APIRouter(
    prefix="/platform/organizations/{organization_id}/olt/v1",
    tags=["Platform OLT Inventory"],
    dependencies=[Depends(require_platform_admin)],
)


def _inventory_dict(row: Any) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _organization_or_404(db: Session, organization_id: int) -> Organization:
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    return organization


def _device_list_response(db, organization_id, limit, offset, device_status, sort_by, sort_order):
    rows, total = list_devices(
        db,
        organization_id,
        limit=limit,
        offset=offset,
        status_filter=device_status,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return OltDeviceListResponse(items=[device_response(row) for row in rows], total=total, limit=limit, offset=offset)


def _inventory_response(db, organization_id, device_id, resource, limit, offset):
    device_or_404(db, organization_id, device_id)
    rows, total = inventory_page(db, organization_id, resource, device_id, limit, offset)
    return OltInventoryListResponse(items=[_inventory_dict(row) for row in rows], total=total, limit=limit, offset=offset)


@organization_router.get("/overview", response_model=OltOverviewResponse)
def organization_overview(
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
    _principal: AuthenticatedPrincipal = Depends(require_permission(Permission.OLT_INVENTORY_READ)),
):
    return overview(db, context.id)


@organization_router.get("/devices", response_model=OltDeviceListResponse)
def organization_devices(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    device_status: str | None = Query(default=None, alias="status"),
    sort_by: str = Query(default="name"),
    sort_order: str = Query(default="asc"),
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
    _principal: AuthenticatedPrincipal = Depends(require_permission(Permission.OLT_INVENTORY_READ)),
):
    return _device_list_response(db, context.id, limit, offset, device_status, sort_by, sort_order)


@organization_router.post("/devices", response_model=OltDeviceResponse, status_code=201)
def organization_create_device(
    payload: OltDeviceCreate,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
    principal: AuthenticatedPrincipal = Depends(require_permission(Permission.OLT_DEVICES_MANAGE)),
):
    return device_response(create_device(db, context.id, payload, principal))


@organization_router.get("/devices/{device_id}", response_model=OltDeviceResponse)
def organization_device(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
    principal: AuthenticatedPrincipal = Depends(require_permission(Permission.OLT_INVENTORY_READ)),
):
    return device_response(device_or_404(db, context.id, device_id, request=request, principal=principal))


@organization_router.patch("/devices/{device_id}", response_model=OltDeviceResponse)
def organization_update_device(
    device_id: int,
    payload: OltDeviceUpdate,
    request: Request,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
    principal: AuthenticatedPrincipal = Depends(require_permission(Permission.OLT_DEVICES_MANAGE)),
):
    device = device_or_404(db, context.id, device_id, request=request, principal=principal)
    return device_response(update_device(db, device, payload, principal))


@organization_router.get("/devices/{device_id}/system-info", response_model=OltDeviceResponse)
def organization_system_info(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
    principal: AuthenticatedPrincipal = Depends(require_permission(Permission.OLT_INVENTORY_READ)),
):
    return device_response(device_or_404(db, context.id, device_id, request=request, principal=principal))


def _add_organization_inventory_route(path: str, resource: str):
    def endpoint(
        device_id: int,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        context: OrganizationContext = Depends(get_organization_context),
        _principal: AuthenticatedPrincipal = Depends(require_permission(Permission.OLT_INVENTORY_READ)),
    ):
        return _inventory_response(db, context.id, device_id, resource, limit, offset)

    endpoint.__name__ = f"organization_{resource.replace('-', '_')}"
    organization_router.add_api_route(path, endpoint, methods=["GET"], response_model=OltInventoryListResponse)


for _resource in ("cards", "uplinks", "pon-ports", "onus", "poll-runs"):
    _add_organization_inventory_route(f"/devices/{{device_id}}/{_resource}", _resource)


@organization_router.get("/onus/{onu_id}")
def organization_onu(
    onu_id: int,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
    _principal: AuthenticatedPrincipal = Depends(require_permission(Permission.OLT_INVENTORY_READ)),
):
    onu = db.query(OltOnu).filter(OltOnu.id == onu_id, OltOnu.organization_id == context.id).first()
    if not onu:
        raise HTTPException(status_code=404, detail="ONU not found")
    return _inventory_dict(onu)


@organization_router.get("/associations", response_model=OltAssociationListResponse)
def organization_associations(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
    _principal: AuthenticatedPrincipal = Depends(require_permission(Permission.OLT_INVENTORY_READ)),
):
    bounded_pagination(limit, offset)
    query = db.query(OltServiceAssociation).filter(OltServiceAssociation.organization_id == context.id)
    total = query.count()
    return OltAssociationListResponse(items=query.order_by(OltServiceAssociation.id).offset(offset).limit(limit).all(), total=total, limit=limit, offset=offset)


@organization_router.post("/associations", response_model=OltServiceAssociationResponse, status_code=201)
def organization_create_association(
    payload: OltServiceAssociationCreate,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
    principal: AuthenticatedPrincipal = Depends(require_permission(Permission.OLT_ASSOCIATIONS_MANAGE)),
):
    return create_association(db, context.id, payload, principal)


@organization_router.delete("/associations/{association_id}", status_code=204)
def organization_remove_association(
    association_id: int,
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
    principal: AuthenticatedPrincipal = Depends(require_permission(Permission.OLT_ASSOCIATIONS_MANAGE)),
):
    association = db.query(OltServiceAssociation).filter(
        OltServiceAssociation.id == association_id,
        OltServiceAssociation.organization_id == context.id,
    ).first()
    if not association:
        raise HTTPException(status_code=404, detail="OLT association not found")
    association.status = "inactive"
    record_audit(
        db,
        organization_id=context.id,
        actor_type=principal.principal_type.value,
        actor_id=principal.subject_id,
        actor_label=principal.actor_label,
        action="olt.association.removed",
        target_type="olt_service_association",
        target_id=str(association.id),
        old_value={"status": "active"},
        new_value={"status": "inactive"},
    )
    db.commit()
    return Response(status_code=204)


@organization_router.get("/audit-events", response_model=AuditLogListResponse)
def organization_audit_events(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
    _principal: AuthenticatedPrincipal = Depends(require_permission(Permission.OLT_AUDIT_READ)),
):
    query = db.query(AuditLog).filter(AuditLog.organization_id == context.id, AuditLog.action.like("olt.%"))
    total = query.count()
    rows = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(offset).limit(limit).all()
    return AuditLogListResponse(items=[AuditLogResponse.model_validate(row) for row in rows], total=total, limit=limit, offset=offset)


def _disabled_operation():
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=OltDisabledOperationResponse().model_dump(),
    )


organization_router.add_api_route(
    "/devices/{device_id}/connection-tests", _disabled_operation, methods=["POST"],
    dependencies=[Depends(require_permission(Permission.OLT_CONNECTIONS_TEST))],
)
organization_router.add_api_route(
    "/devices/{device_id}/refresh-requests", _disabled_operation, methods=["POST"],
    dependencies=[Depends(require_permission(Permission.OLT_POLL_REQUEST))],
)


@platform_router.get("/overview", response_model=OltOverviewResponse)
def platform_overview(organization_id: int, db: Session = Depends(get_db)):
    _organization_or_404(db, organization_id)
    return overview(db, organization_id)


@platform_router.get("/devices", response_model=OltDeviceListResponse)
def platform_devices(
    organization_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    device_status: str | None = Query(default=None, alias="status"),
    sort_by: str = Query(default="name"),
    sort_order: str = Query(default="asc"),
    db: Session = Depends(get_db),
):
    _organization_or_404(db, organization_id)
    return _device_list_response(db, organization_id, limit, offset, device_status, sort_by, sort_order)


@platform_router.post("/devices", response_model=OltDeviceResponse, status_code=201)
def platform_create_device(
    organization_id: int,
    payload: OltDeviceCreate,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_platform_admin),
):
    _organization_or_404(db, organization_id)
    return device_response(create_device(db, organization_id, payload, principal))


@platform_router.get("/devices/{device_id}", response_model=OltDeviceResponse)
def platform_device(organization_id: int, device_id: int, db: Session = Depends(get_db)):
    _organization_or_404(db, organization_id)
    return device_response(device_or_404(db, organization_id, device_id))


@platform_router.patch("/devices/{device_id}", response_model=OltDeviceResponse)
def platform_update_device(
    organization_id: int,
    device_id: int,
    payload: OltDeviceUpdate,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_platform_admin),
):
    _organization_or_404(db, organization_id)
    return device_response(update_device(db, device_or_404(db, organization_id, device_id), payload, principal))


@platform_router.get("/devices/{device_id}/system-info", response_model=OltDeviceResponse)
def platform_system_info(organization_id: int, device_id: int, db: Session = Depends(get_db)):
    _organization_or_404(db, organization_id)
    return device_response(device_or_404(db, organization_id, device_id))


def _add_platform_inventory_route(path: str, resource: str):
    def endpoint(
        organization_id: int,
        device_id: int,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
    ):
        _organization_or_404(db, organization_id)
        return _inventory_response(db, organization_id, device_id, resource, limit, offset)

    endpoint.__name__ = f"platform_{resource.replace('-', '_')}"
    platform_router.add_api_route(path, endpoint, methods=["GET"], response_model=OltInventoryListResponse)


for _platform_resource in ("cards", "uplinks", "pon-ports", "onus", "poll-runs"):
    _add_platform_inventory_route(f"/devices/{{device_id}}/{_platform_resource}", _platform_resource)


@platform_router.get("/onus/{onu_id}")
def platform_onu(organization_id: int, onu_id: int, db: Session = Depends(get_db)):
    _organization_or_404(db, organization_id)
    onu = db.query(OltOnu).filter(OltOnu.id == onu_id, OltOnu.organization_id == organization_id).first()
    if not onu:
        raise HTTPException(status_code=404, detail="ONU not found")
    return _inventory_dict(onu)


@platform_router.get("/associations", response_model=OltAssociationListResponse)
def platform_associations(
    organization_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    _organization_or_404(db, organization_id)
    query = db.query(OltServiceAssociation).filter(OltServiceAssociation.organization_id == organization_id)
    total = query.count()
    return OltAssociationListResponse(items=query.order_by(OltServiceAssociation.id).offset(offset).limit(limit).all(), total=total, limit=limit, offset=offset)


@platform_router.get("/audit-events", response_model=AuditLogListResponse)
def platform_audit_events(
    organization_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    _organization_or_404(db, organization_id)
    query = db.query(AuditLog).filter(AuditLog.organization_id == organization_id, AuditLog.action.like("olt.%"))
    total = query.count()
    rows = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(offset).limit(limit).all()
    return AuditLogListResponse(items=[AuditLogResponse.model_validate(row) for row in rows], total=total, limit=limit, offset=offset)
