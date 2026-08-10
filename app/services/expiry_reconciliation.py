from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import ExpiryDisconnectJob, User
from app.services.audit import record_audit


def cancel_stale_disconnect_jobs(
    db: Session,
    service: User,
    *,
    now: datetime,
    correlation_id: str,
    actor_type: str,
    actor_id: str,
    actor_label: str,
) -> int:
    jobs = db.query(ExpiryDisconnectJob).filter(
        ExpiryDisconnectJob.organization_id == service.organization_id,
        ExpiryDisconnectJob.user_id == service.id,
        ExpiryDisconnectJob.status.in_(("pending", "retryable_failure")),
    ).with_for_update().all()
    for job in jobs:
        job.status = "cancelled"
        job.completed_at = now
        job.last_error = None
        record_audit(
            db,
            organization_id=service.organization_id,
            actor=actor_label,
            actor_type=actor_type,
            actor_id=actor_id,
            actor_label=actor_label,
            action="expiry.disconnect_cancelled_after_renewal",
            target_type="expiry_disconnect_job",
            target_id=str(job.id),
            new_value={
                "customer_id": service.customer_id,
                "service_id": service.id,
                "session_identity": job.session_identity,
                "correlation_id": correlation_id,
                "job_correlation_id": job.correlation_id,
                "reason_code": "RENEWED_BEFORE_DISCONNECT",
                "result": "cancelled",
            },
        )
    return len(jobs)
