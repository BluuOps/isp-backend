from __future__ import annotations

import re
from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.organization import Organization

TENANT_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True)
class TenantHostContext:
    organization: Organization
    hostname: str
    tenant_label: str
    source: str


def _tenant_error(code: str, message: str, http_status: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"error": code, "message": message})


def _host_without_port(raw_host: str | None) -> str:
    value = (raw_host or "").strip().lower().rstrip(".")
    if not value:
        raise _tenant_error("missing_tenant_host", "Host header is required.")
    if "," in value:
        raise _tenant_error("malformed_tenant_host", "Host header must contain exactly one hostname.")
    if any(char.isspace() for char in value) or "/" in value or "\\" in value:
        raise _tenant_error("malformed_tenant_host", "Host header is malformed.")
    if value.startswith("["):
        if "]" not in value:
            raise _tenant_error("malformed_tenant_host", "Host header is malformed.")
        return value.split("]", 1)[0].lstrip("[")
    return value.rsplit(":", 1)[0] if ":" in value else value


def request_hostname(request: Request) -> str:
    client_host = request.client.host if request.client else None
    forwarded = request.headers.get("x-forwarded-host")
    if settings.trust_forwarded_host and forwarded and client_host in settings.trusted_proxy_ips:
        return _host_without_port(forwarded)
    return _host_without_port(request.headers.get("host"))


def _tenant_label_from_hostname(hostname: str) -> str | None:
    for domain in settings.tenant_allowed_domains:
        suffix = f".{domain}"
        if hostname == domain:
            raise _tenant_error("tenant_required", "Tenant subdomain is required for this portal.")
        if hostname.endswith(suffix):
            label = hostname[: -len(suffix)]
            if "." in label or not label:
                raise _tenant_error("malformed_tenant_host", "Tenant hostname must contain exactly one tenant subdomain.")
            return label
    return None


def _resolve_organization(db: Session, tenant_label: str) -> Organization:
    if tenant_label in settings.tenant_reserved_subdomains:
        raise _tenant_error("reserved_tenant_subdomain", "This subdomain is reserved by RadiusFiber.")
    if not TENANT_LABEL_RE.fullmatch(tenant_label):
        raise _tenant_error("malformed_tenant_subdomain", "Tenant subdomain is malformed.")

    normalized = tenant_label.replace("-", "")
    matches = (
        db.query(Organization)
        .filter(func.replace(func.lower(Organization.slug), "-", "") == normalized)
        .limit(2)
        .all()
    )
    if not matches:
        raise _tenant_error("tenant_not_found", "Organization was not found.", status.HTTP_404_NOT_FOUND)
    if len(matches) > 1:
        raise _tenant_error("ambiguous_tenant", "Organization hostname is ambiguous.")

    organization = matches[0]
    if organization.status != "active":
        raise _tenant_error("tenant_inactive", "Organization is not active.", status.HTTP_403_FORBIDDEN)
    return organization


def resolve_tenant_from_request(
    request: Request,
    db: Session,
    *,
    allow_staging_fallback: bool | None = None,
) -> TenantHostContext:
    hostname = request_hostname(request)
    tenant_label = _tenant_label_from_hostname(hostname)

    if tenant_label is None:
        fallback_enabled = settings.allow_staging_tenant_fallback if allow_staging_fallback is None else allow_staging_fallback
        if not fallback_enabled:
            raise _tenant_error("unapproved_tenant_host", "Host is not an approved RadiusFiber tenant domain.")
        tenant_label = settings.staging_organization_slug
        source = "staging_fallback"
    else:
        source = "hostname"

    organization = _resolve_organization(db, tenant_label)
    return TenantHostContext(
        organization=organization,
        hostname=hostname,
        tenant_label=tenant_label,
        source=source,
    )
