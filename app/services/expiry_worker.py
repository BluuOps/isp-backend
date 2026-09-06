from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import and_, func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    Customer,
    ExpiryDisconnectJob,
    ExpiryScanRun,
    NetworkAccessServer,
    Organization,
    RadAcct,
    ServicePlan,
    User,
)
from app.services.audit import record_audit
from app.services.disconnect_adapter import (
    DisconnectAdapter,
    DisconnectError,
    DisconnectRequest,
    DisconnectResult,
)
from app.services.expiry_policy import AccessReason, aware_utc, require_aware_utc
from app.services.radius_authorization import synchronize_radius_authorization
from app.services.radius_session_freshness import fresh_active_session_conditions


logger = logging.getLogger("radiusfiber.expiry_worker")
ADVISORY_LOCK_ID = 721_020_012
PENDING_STATUSES = ("pending", "retryable_failure")
FINAL_STATUSES = ("succeeded", "terminal_failure", "cancelled", "stale")


@dataclass(frozen=True)
class ScanSummary:
    correlation_id: str
    acquired_lock: bool
    dry_run: bool
    evaluated: int
    newly_expired: int
    active_sessions: int
    jobs_queued: int
    errors: int
    duration_ms: int


@dataclass(frozen=True)
class JobResult:
    job_id: int | None
    status: str
    detail: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _session_identity(session: RadAcct) -> str:
    return str(session.acctuniqueid or session.acctsessionid or session.radacctid)


def _try_worker_lock(db: Session) -> bool:
    dialect = getattr(getattr(db, "bind", None), "dialect", None)
    if dialect is not None and dialect.name != "postgresql":
        return True
    return bool(
        db.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
            {"lock_id": ADVISORY_LOCK_ID},
        ).scalar()
    )


def _latest_fresh_session(
    db: Session,
    username: str,
    *,
    test_now: datetime | None = None,
) -> RadAcct | None:
    return (
        db.query(RadAcct)
        .filter(
            RadAcct.username == username,
            *fresh_active_session_conditions(test_now),
        )
        .order_by(RadAcct.acctstarttime.desc(), RadAcct.radacctid.desc())
        .first()
    )


def _fresh_job_session(
    db: Session,
    *,
    radacct_id: int,
    username: str,
    test_now: datetime | None = None,
) -> RadAcct | None:
    return (
        db.query(RadAcct)
        .filter(
            RadAcct.radacctid == radacct_id,
            RadAcct.username == username,
            *fresh_active_session_conditions(test_now),
        )
        .first()
    )


def scan_expired_services(
    db: Session,
    *,
    now: datetime | None = None,
    organization_id: int | None = None,
    user_id: int | None = None,
    username: str | None = None,
    batch_size: int | None = None,
    dry_run: bool | None = None,
    acquire_lock: bool = True,
) -> ScanSummary:
    target_parts = (user_id is not None, username is not None)
    if any(target_parts) and not all(target_parts):
        raise ValueError("targeted scan requires both user_id and username")
    if all(target_parts) and organization_id is None:
        raise ValueError("targeted scan requires organization_id")
    started_clock = time.monotonic()
    current = require_aware_utc(now or utc_now())
    limit = max(1, min(1000, batch_size or settings.expiry_scan_batch_size))
    is_dry_run = settings.expiry_worker_dry_run if dry_run is None else dry_run
    correlation_id = uuid.uuid4().hex
    acquired = not acquire_lock or is_dry_run or _try_worker_lock(db)
    if not acquired:
        return ScanSummary(correlation_id, False, is_dry_run, 0, 0, 0, 0, 0, 0)

    scan_run = None
    if not is_dry_run:
        scan_run = ExpiryScanRun(
            correlation_id=correlation_id,
            organization_id=organization_id,
            component="expiry-scan-worker",
            status="running",
            dry_run=False,
            started_at=current,
        )
        db.add(scan_run)

    query = (
        db.query(User, Customer, Organization, ServicePlan)
        .join(Customer, and_(Customer.id == User.customer_id, Customer.organization_id == User.organization_id))
        .join(Organization, Organization.id == User.organization_id)
        .outerjoin(
            ServicePlan,
            and_(ServicePlan.name == User.service_plan, ServicePlan.organization_id == User.organization_id),
        )
        .filter(
            User.status == "active",
            or_(User.expiration_date.is_(None), User.expiration_date <= current),
        )
        .order_by(User.expiration_date.asc(), User.id.asc())
    )
    if organization_id is not None:
        query = query.filter(User.organization_id == organization_id)
    if user_id is not None and username is not None:
        query = query.filter(User.id == user_id, User.username == username)
    query = query.limit(limit)

    evaluated = newly_expired = active_sessions = jobs_queued = errors = 0
    for service, customer, organization, plan in query.all():
        evaluated += 1
        try:
            if not is_dry_run:
                decision = synchronize_radius_authorization(
                    db,
                    service,
                    plan,
                    now=current,
                    customer_status=customer.account_status,
                    organization_status=organization.status,
                )
            else:
                from app.services.expiry_policy import evaluate_access
                decision = evaluate_access(
                    service,
                    plan,
                    now=current,
                    organization_id=organization.id,
                    customer_status=customer.account_status,
                    organization_status=organization.status,
                )
            if decision.reason != AccessReason.EXPIRED:
                continue
            newly_expired += 1
            active_session = _latest_fresh_session(db, service.username, test_now=now)
            if not active_session:
                if not is_dry_run:
                    record_audit(
                        db,
                        organization_id=organization.id,
                        actor="system",
                        actor_type="system",
                        actor_id="expiry-scan-worker",
                        actor_label="expiry-scan-worker",
                        action="expiry.authorization_blocked",
                        target_type="user",
                        target_id=str(service.id),
                        new_value={
                            "customer_id": customer.id,
                            "service_id": service.id,
                            "expiration_date": decision.expires_at.isoformat(),
                            "correlation_id": correlation_id,
                            "reason_code": decision.reason.value,
                            "active_session": False,
                        },
                    )
                continue
            active_sessions += 1
            identity = _session_identity(active_session)
            expiration = aware_utc(service.expiration_date)
            existing = db.query(ExpiryDisconnectJob).filter(
                ExpiryDisconnectJob.organization_id == organization.id,
                ExpiryDisconnectJob.user_id == service.id,
                ExpiryDisconnectJob.session_identity == identity,
                ExpiryDisconnectJob.requested_expiration_at == expiration,
            ).first()
            if existing or is_dry_run:
                continue
            nas = db.query(NetworkAccessServer).filter(
                NetworkAccessServer.organization_id == organization.id,
                NetworkAccessServer.nas_ip_address == active_session.nasipaddress,
                NetworkAccessServer.status == "active",
            ).first()
            job = ExpiryDisconnectJob(
                organization_id=organization.id,
                customer_id=customer.id,
                user_id=service.id,
                session_radacct_id=active_session.radacctid,
                session_identity=identity,
                nas_id=nas.id if nas else None,
                reason_code=AccessReason.EXPIRED.value,
                requested_expiration_at=expiration,
                status="pending",
                attempt_count=0,
                max_attempts=settings.expiry_disconnect_max_attempts,
                next_attempt_at=current,
                correlation_id=uuid.uuid4().hex,
            )
            try:
                with db.begin_nested():
                    db.add(job)
                    db.flush()
                jobs_queued += 1
                record_audit(
                    db,
                    organization_id=organization.id,
                    actor="system",
                    actor_type="system",
                    actor_id="expiry-scan-worker",
                    actor_label="expiry-scan-worker",
                    action="expiry.disconnect_queued",
                    target_type="expiry_disconnect_job",
                    target_id=str(job.id),
                    new_value={
                        "customer_id": customer.id,
                        "service_id": service.id,
                        "session_identity": identity,
                        "nas_id": job.nas_id,
                        "expiration_date": expiration.isoformat(),
                        "correlation_id": job.correlation_id,
                        "reason_code": job.reason_code,
                        "result": "pending",
                    },
                )
            except IntegrityError:
                logger.info("Duplicate expiry disconnect job suppressed", extra={"organization_id": organization.id, "service_id": service.id})
        except Exception as exc:
            errors += 1
            logger.exception("Expiry scan item failed", extra={"organization_id": service.organization_id, "service_id": service.id})
            if not is_dry_run:
                record_audit(
                    db,
                    organization_id=service.organization_id,
                    actor="system",
                    actor_type="system",
                    actor_id="expiry-scan-worker",
                    actor_label="expiry-scan-worker",
                    action="expiry.scan_item_failed",
                    target_type="user",
                    target_id=str(service.id),
                    success=False,
                    new_value={"correlation_id": correlation_id, "reason_code": type(exc).__name__},
                )

    duration_ms = max(0, int((time.monotonic() - started_clock) * 1000))
    if scan_run is not None:
        scan_run.status = "completed" if errors == 0 else "completed_with_errors"
        scan_run.evaluated_count = evaluated
        scan_run.newly_expired_count = newly_expired
        scan_run.active_session_count = active_sessions
        scan_run.jobs_queued_count = jobs_queued
        scan_run.error_count = errors
        scan_run.completed_at = utc_now()
        scan_run.duration_ms = duration_ms
        record_audit(
            db,
            organization_id=organization_id,
            actor="system",
            actor_type="system",
            actor_id="expiry-scan-worker",
            actor_label="expiry-scan-worker",
            action="expiry.scan_completed",
            target_type="expiry_scan_run",
            target_id=correlation_id,
            success=errors == 0,
            new_value={
                "correlation_id": correlation_id,
                "dry_run": False,
                "evaluated": evaluated,
                "newly_expired": newly_expired,
                "active_sessions": active_sessions,
                "jobs_queued": jobs_queued,
                "errors": errors,
                "duration_ms": duration_ms,
            },
        )
    return ScanSummary(
        correlation_id, True, is_dry_run, evaluated, newly_expired,
        active_sessions, jobs_queued, errors, duration_ms,
    )


def _sanitize_error(exc: Exception) -> str:
    return type(exc).__name__[:120]


def process_next_disconnect_job(
    session_factory: Callable[[], Session],
    adapter: DisconnectAdapter,
    *,
    now: datetime | None = None,
    jitter: Callable[[float, float], float] = random.uniform,
) -> JobResult:
    current = require_aware_utc(now or utc_now())
    job_id: int | None = None
    request: DisconnectRequest | None = None

    with session_factory() as db:
        processing_cutoff = current - timedelta(
            seconds=settings.expiry_disconnect_processing_timeout_seconds
        )
        abandoned_jobs = (
            db.query(ExpiryDisconnectJob)
            .filter(
                ExpiryDisconnectJob.status == "processing",
                ExpiryDisconnectJob.processing_started_at.is_not(None),
                ExpiryDisconnectJob.processing_started_at <= processing_cutoff,
            )
            .order_by(ExpiryDisconnectJob.processing_started_at.asc(), ExpiryDisconnectJob.id.asc())
            .with_for_update(skip_locked=True)
            .limit(settings.expiry_scan_batch_size)
            .all()
        )
        for abandoned in abandoned_jobs:
            if abandoned.attempt_count >= abandoned.max_attempts:
                abandoned.status = "terminal_failure"
                abandoned.completed_at = current
                action = "expiry.disconnect_terminal_failure"
            else:
                abandoned.status = "retryable_failure"
                abandoned.next_attempt_at = current
                action = "expiry.disconnect_processing_lease_recovered"
            abandoned.processing_started_at = None
            abandoned.last_error = "processing_lease_expired"
            record_audit(
                db,
                organization_id=abandoned.organization_id,
                actor="system",
                actor_type="system",
                actor_id="expiry-disconnect-worker",
                actor_label="expiry-disconnect-worker",
                action=action,
                target_type="expiry_disconnect_job",
                target_id=str(abandoned.id),
                success=False,
                new_value={
                    "customer_id": abandoned.customer_id,
                    "service_id": abandoned.user_id,
                    "session_identity": abandoned.session_identity,
                    "correlation_id": abandoned.correlation_id,
                    "attempt": abandoned.attempt_count,
                    "reason_code": "PROCESSING_LEASE_EXPIRED",
                    "result": abandoned.status,
                },
            )
        if abandoned_jobs:
            db.flush()

        job = (
            db.query(ExpiryDisconnectJob)
            .filter(
                ExpiryDisconnectJob.status.in_(PENDING_STATUSES),
                ExpiryDisconnectJob.next_attempt_at <= current,
                ExpiryDisconnectJob.attempt_count < ExpiryDisconnectJob.max_attempts,
            )
            .order_by(ExpiryDisconnectJob.next_attempt_at.asc(), ExpiryDisconnectJob.id.asc())
            .with_for_update(skip_locked=True)
            .first()
        )
        if not job:
            return JobResult(None, "empty", "no_due_job")
        job_id = job.id
        service = db.query(User).filter(
            User.id == job.user_id,
            User.organization_id == job.organization_id,
            User.customer_id == job.customer_id,
        ).first()
        session = _fresh_job_session(
            db,
            radacct_id=job.session_radacct_id,
            username=service.username if service else "",
            test_now=now,
        )
        requested_expiration = aware_utc(job.requested_expiration_at)
        current_expiration = aware_utc(service.expiration_date) if service else None
        if (
            not service
            or service.status != "active"
            or current_expiration != requested_expiration
            or current_expiration is None
            or current_expiration > current
            or not session
            or _session_identity(session) != job.session_identity
        ):
            job.status = "stale"
            job.completed_at = current
            job.last_error = None
            record_audit(
                db,
                organization_id=job.organization_id,
                actor="system",
                actor_type="system",
                actor_id="expiry-disconnect-worker",
                actor_label="expiry-disconnect-worker",
                action="expiry.disconnect_cancelled_stale",
                target_type="expiry_disconnect_job",
                target_id=str(job.id),
                new_value={
                    "customer_id": job.customer_id,
                    "service_id": job.user_id,
                    "session_identity": job.session_identity,
                    "correlation_id": job.correlation_id,
                    "reason_code": "STALE_AFTER_RENEWAL_OR_SESSION_CHANGE",
                    "result": "stale",
                },
            )
            db.commit()
            return JobResult(job.id, job.status, "stale_after_renewal_or_session_change")
        nas = db.query(NetworkAccessServer).filter(
            NetworkAccessServer.id == job.nas_id,
            NetworkAccessServer.organization_id == job.organization_id,
            NetworkAccessServer.status == "active",
        ).first()
        if not nas:
            job.status = "terminal_failure"
            job.completed_at = current
            job.last_error = "nas_mapping_unavailable"
            record_audit(
                db,
                organization_id=job.organization_id,
                actor="system",
                actor_type="system",
                actor_id="expiry-disconnect-worker",
                actor_label="expiry-disconnect-worker",
                action="expiry.disconnect_terminal_failure",
                target_type="expiry_disconnect_job",
                target_id=str(job.id),
                success=False,
                new_value={
                    "customer_id": job.customer_id,
                    "service_id": job.user_id,
                    "session_identity": job.session_identity,
                    "correlation_id": job.correlation_id,
                    "attempt": job.attempt_count,
                    "reason_code": "NAS_MAPPING_UNAVAILABLE",
                    "result": job.status,
                },
            )
            db.commit()
            return JobResult(job.id, job.status, job.last_error)
        job.status = "processing"
        job.processing_started_at = current
        job.attempt_count += 1
        request = DisconnectRequest(
            username=service.username,
            session_identity=job.session_identity,
            accounting_session_id=str(session.acctsessionid),
            nas_ip_address=str(nas.nas_ip_address),
            framed_ip_address=str(session.framedipaddress) if session.framedipaddress else None,
        )
        record_audit(
            db,
            organization_id=job.organization_id,
            actor="system",
            actor_type="system",
            actor_id="expiry-disconnect-worker",
            actor_label="expiry-disconnect-worker",
            action="expiry.disconnect_started",
            target_type="expiry_disconnect_job",
            target_id=str(job.id),
            new_value={
                "customer_id": job.customer_id,
                "service_id": job.user_id,
                "session_identity": job.session_identity,
                "nas_id": job.nas_id,
                "correlation_id": job.correlation_id,
                "attempt": job.attempt_count,
                "reason_code": job.reason_code,
            },
        )
        db.commit()

    # No database session or transaction is open while network-capable code runs.
    try:
        adapter_result = adapter.disconnect(request)
        if not adapter_result.acknowledged:
            raise RetryableDisconnectError("disconnect_not_acknowledged")
        outcome: DisconnectResult | Exception = adapter_result
    except Exception as exc:
        outcome = exc

    with session_factory() as db:
        job = db.query(ExpiryDisconnectJob).filter(ExpiryDisconnectJob.id == job_id).with_for_update().one()
        if job.status != "processing":
            return JobResult(job.id, job.status, "job_state_changed_before_result_recording")
        if isinstance(outcome, Exception):
            retryable = isinstance(outcome, DisconnectError) and outcome.retryable
            if retryable and job.attempt_count < job.max_attempts:
                job.status = "retryable_failure"
                delay = settings.expiry_disconnect_backoff_seconds * (2 ** (job.attempt_count - 1))
                job.next_attempt_at = current + timedelta(seconds=int(delay + jitter(0, max(1, delay * 0.2))))
            else:
                job.status = "terminal_failure"
                job.completed_at = current
            job.last_error = _sanitize_error(outcome)
            action = "expiry.disconnect_retry_scheduled" if job.status == "retryable_failure" else "expiry.disconnect_terminal_failure"
            success = False
            detail = job.last_error
        else:
            job.status = "succeeded"
            job.completed_at = current
            job.last_error = None
            action = "expiry.disconnect_succeeded"
            success = True
            detail = outcome.detail
        record_audit(
            db,
            organization_id=job.organization_id,
            actor="system",
            actor_type="system",
            actor_id="expiry-disconnect-worker",
            actor_label="expiry-disconnect-worker",
            action=action,
            target_type="expiry_disconnect_job",
            target_id=str(job.id),
            success=success,
            new_value={
                "customer_id": job.customer_id,
                "service_id": job.user_id,
                "session_identity": job.session_identity,
                "nas_id": job.nas_id,
                "correlation_id": job.correlation_id,
                "attempt": job.attempt_count,
                "reason_code": job.reason_code,
                "result": job.status,
            },
        )
        db.commit()
        return JobResult(job.id, job.status, detail)


# Imported late to keep the adapter protocol definitions small and testable.
from app.services.disconnect_adapter import RetryableDisconnectError  # noqa: E402


def expiry_metrics(db: Session) -> dict[str, object]:
    last_scan = db.query(ExpiryScanRun).filter(
        ExpiryScanRun.status.in_(("completed", "completed_with_errors"))
    ).order_by(ExpiryScanRun.completed_at.desc()).first()
    statuses = dict(
        db.query(ExpiryDisconnectJob.status, func.count(ExpiryDisconnectJob.id))
        .group_by(ExpiryDisconnectJob.status)
        .all()
    )
    return {
        "last_successful_scan_at": last_scan.completed_at if last_scan and last_scan.error_count == 0 else None,
        "last_scan_duration_ms": last_scan.duration_ms if last_scan else None,
        "last_scan_evaluated": last_scan.evaluated_count if last_scan else 0,
        "last_scan_newly_expired": last_scan.newly_expired_count if last_scan else 0,
        "last_scan_active_sessions": last_scan.active_session_count if last_scan else 0,
        "last_scan_jobs_queued": last_scan.jobs_queued_count if last_scan else 0,
        "job_status_counts": statuses,
    }
