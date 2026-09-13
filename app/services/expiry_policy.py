from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol


class AccessReason(str, Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    MISSING_EXPIRATION = "MISSING_EXPIRATION"
    INVALID_PLAN = "INVALID_PLAN"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    LIFECYCLE_BLOCKED = "LIFECYCLE_BLOCKED"


class ServiceLike(Protocol):
    organization_id: int | None
    status: str | None
    expiration_date: datetime | None
    service_plan: str


class PlanLike(Protocol):
    organization_id: int | None
    status: str | None
    name: str


@dataclass(frozen=True)
class AccessDecision:
    eligible: bool
    reason: AccessReason
    expires_at: datetime | None


def aware_utc(value: datetime | None) -> datetime | None:
    """Normalize legacy naive values as UTC; all new values must be aware."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Authoritative time must be timezone-aware")
    return value.astimezone(timezone.utc)


def evaluate_access(
    service: ServiceLike,
    plan: PlanLike | None,
    *,
    now: datetime,
    organization_id: int | None = None,
    customer_status: str | None = "active",
    organization_status: str | None = "active",
) -> AccessDecision:
    current = require_aware_utc(now)
    expires_at = aware_utc(service.expiration_date)

    if organization_id is not None and service.organization_id != organization_id:
        return AccessDecision(False, AccessReason.TENANT_MISMATCH, expires_at)
    if plan is not None and plan.organization_id != service.organization_id:
        return AccessDecision(False, AccessReason.TENANT_MISMATCH, expires_at)

    lifecycle = (service.status or "").lower()
    if lifecycle == "terminated":
        return AccessDecision(False, AccessReason.TERMINATED, expires_at)
    if lifecycle == "suspended" or customer_status == "suspended":
        return AccessDecision(False, AccessReason.SUSPENDED, expires_at)
    if lifecycle != "active" or customer_status != "active" or organization_status != "active":
        return AccessDecision(False, AccessReason.LIFECYCLE_BLOCKED, expires_at)
    if plan is None or plan.status != "active" or plan.name != service.service_plan:
        return AccessDecision(False, AccessReason.INVALID_PLAN, expires_at)
    if expires_at is None:
        return AccessDecision(False, AccessReason.MISSING_EXPIRATION, None)
    if expires_at <= current:
        return AccessDecision(False, AccessReason.EXPIRED, expires_at)
    return AccessDecision(True, AccessReason.ACTIVE, expires_at)
