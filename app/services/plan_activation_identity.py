from __future__ import annotations

import hashlib
from datetime import datetime, timezone


def logical_plan_purchase_key(
    *,
    organization_id: int,
    customer_id: str,
    service_id: int,
    source_plan_name: str,
    selected_plan_id: int,
    source_expiration: datetime | None,
    billing_periods: int,
) -> str:
    if source_expiration is None:
        boundary = "no-expiration"
    else:
        if source_expiration.tzinfo is None:
            source_expiration = source_expiration.replace(tzinfo=timezone.utc)
        boundary = source_expiration.astimezone(timezone.utc).isoformat()
    value = "|".join(
        (
            str(organization_id),
            customer_id,
            str(service_id),
            source_plan_name,
            str(selected_plan_id),
            boundary,
            str(max(1, billing_periods)),
        )
    )
    return hashlib.md5(value.encode("utf-8"), usedforsecurity=False).hexdigest()
