from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, Interval, bindparam, func

from app.core.config import settings
from app.models import RadAcct


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def session_freshness_cutoff(now: datetime | None = None) -> datetime:
    current = now or utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Session freshness clock must be timezone-aware")
    return current - timedelta(seconds=settings.radius_session_freshness_seconds)


def fresh_active_session_conditions(now: datetime | None = None):
    """Canonical predicates for an open session with recent accounting activity."""

    if now is None:
        freshness_interval = bindparam(
            "radius_session_freshness_interval",
            timedelta(seconds=settings.radius_session_freshness_seconds),
            type_=Interval(),
        )
        cutoff = func.current_timestamp() - freshness_interval
    else:
        cutoff = bindparam(
            "radius_session_freshness_cutoff",
            session_freshness_cutoff(now),
            type_=DateTime(timezone=True),
        )
    return (
        RadAcct.acctstoptime.is_(None),
        func.coalesce(RadAcct.acctupdatetime, RadAcct.acctstarttime) >= cutoff,
    )


def is_fresh_active_session(session: RadAcct, *, now: datetime | None = None) -> bool:
    if session.acctstoptime is not None:
        return False
    activity_at = session.acctupdatetime or session.acctstarttime
    if activity_at is None:
        return False
    if activity_at.tzinfo is None or activity_at.utcoffset() is None:
        activity_at = activity_at.replace(tzinfo=timezone.utc)
    return activity_at >= session_freshness_cutoff(now)
