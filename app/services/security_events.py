from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import uuid
from typing import Any

from fastapi import Request


logger = logging.getLogger("radiusfiber.security")
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9._:-]{1,120}\Z")


def _safe_identifier(value: object) -> str:
    candidate = str(value).strip()
    if _SAFE_IDENTIFIER.fullmatch(candidate):
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8", errors="replace")).hexdigest()[:24]
    return f"sha256:{digest}"


def _request_correlation_id(request: Request) -> str:
    supplied = request.headers.get("x-correlation-id") or request.headers.get("x-request-id")
    return _safe_identifier(supplied) if supplied else uuid.uuid4().hex


def emit_customer_object_denial(
    request: Request,
    *,
    event_name: str,
    customer_id: object,
    organization_id: int,
    resource_type: str,
    resource_id: object,
    route_template: str,
) -> None:
    """Emit one bounded security event without changing request or database state."""

    event: dict[str, Any] = {
        "event_version": 1,
        "event_name": _safe_identifier(event_name),
        "correlation_id": _request_correlation_id(request),
        "principal_type": "customer",
        "subject_id": _safe_identifier(customer_id),
        "organization_id": int(organization_id),
        "resource_type": _safe_identifier(resource_type),
        "resource_id": _safe_identifier(resource_id),
        "denial_reason": "not_owned_or_not_found",
        "route": route_template,
        "method": "GET",
        "status_code": 404,
    }
    try:
        logger.warning(
            json.dumps(event, separators=(",", ":"), sort_keys=True),
            extra={"security_event": event},
        )
    except Exception:
        # Keep failure observable without recursively calling a failed handler or exposing metadata.
        try:
            sys.stderr.write("radiusfiber_security_event_emission_failed\n")
        except Exception:
            pass


def emit_olt_access_denial(
    request: Request,
    *,
    principal_type: str,
    subject_id: object,
    organization_id: int,
    resource_type: str,
    resource_id: object,
    route_template: str,
) -> None:
    """Emit a bounded OLT denial event without writing to the database."""

    event: dict[str, Any] = {
        "event_version": 1,
        "event_name": "olt.cross_tenant_access_rejected",
        "correlation_id": _request_correlation_id(request),
        "principal_type": _safe_identifier(principal_type),
        "subject_id": _safe_identifier(subject_id),
        "organization_id": int(organization_id),
        "resource_type": _safe_identifier(resource_type),
        "resource_id": _safe_identifier(resource_id),
        "denial_reason": "not_owned_or_not_found",
        "route": route_template,
        "method": request.method,
        "status_code": 404,
    }
    try:
        logger.warning(json.dumps(event, separators=(",", ":"), sort_keys=True), extra={"security_event": event})
    except Exception:
        try:
            sys.stderr.write("radiusfiber_security_event_emission_failed\n")
        except Exception:
            pass
