from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from sqlalchemy.dialects import postgresql

from app.services.radius_session_freshness import (
    fresh_active_session_conditions,
    is_fresh_active_session,
    session_freshness_cutoff,
)


NOW = datetime(2028, 2, 29, 12, 0, tzinfo=timezone.utc)


def session(**overrides):
    values = {
        "acctstarttime": NOW - timedelta(hours=1),
        "acctupdatetime": NOW - timedelta(minutes=1),
        "acctstoptime": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RadiusSessionFreshnessTests(unittest.TestCase):
    def test_default_cutoff_is_fifteen_minutes(self):
        self.assertEqual(session_freshness_cutoff(NOW), NOW - timedelta(minutes=15))

    def test_recent_interim_update_is_online(self):
        self.assertTrue(is_fresh_active_session(session(), now=NOW))

    def test_exact_boundary_is_online(self):
        self.assertTrue(
            is_fresh_active_session(
                session(acctupdatetime=NOW - timedelta(minutes=15)), now=NOW
            )
        )

    def test_old_open_row_is_not_online(self):
        self.assertFalse(
            is_fresh_active_session(
                session(acctupdatetime=NOW - timedelta(minutes=15, microseconds=1)),
                now=NOW,
            )
        )

    def test_closed_row_is_not_online(self):
        self.assertFalse(is_fresh_active_session(session(acctstoptime=NOW), now=NOW))

    def test_start_time_is_used_before_first_interim_update(self):
        self.assertTrue(
            is_fresh_active_session(
                session(acctstarttime=NOW - timedelta(minutes=2), acctupdatetime=None),
                now=NOW,
            )
        )

    def test_missing_activity_timestamp_is_not_online(self):
        self.assertFalse(
            is_fresh_active_session(
                session(acctstarttime=None, acctupdatetime=None), now=NOW
            )
        )

    def test_naive_database_timestamp_is_treated_as_utc(self):
        self.assertTrue(
            is_fresh_active_session(
                session(acctupdatetime=(NOW - timedelta(minutes=1)).replace(tzinfo=None)),
                now=NOW,
            )
        )

    def test_naive_authoritative_clock_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            session_freshness_cutoff(NOW.replace(tzinfo=None))

    def test_sql_predicate_requires_open_and_recent_activity(self):
        sql = " AND ".join(
            str(
                expression.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
            for expression in fresh_active_session_conditions(NOW)
        )
        self.assertIn("radacct.acctstoptime IS NULL", sql)
        self.assertIn("coalesce(radacct.acctupdatetime, radacct.acctstarttime)", sql)
        self.assertIn("2028-02-29 11:45:00+00:00", sql)


class SessionFreshnessConsumerTests(unittest.TestCase):
    def test_all_online_consumers_use_the_canonical_predicate(self):
        root = Path(__file__).parents[1]
        expected = {
            "app/routers/platform.py",
            "app/routers/network.py",
            "app/routers/radius_sessions.py",
            "app/routers/customer_portal.py",
            "app/services/expiry_worker.py",
        }
        for relative in expected:
            source = (root / relative).read_text()
            self.assertIn("fresh_active_session_conditions", source, relative)

    def test_customer_portal_get_views_do_not_record_view_audits(self):
        source = (Path(__file__).parents[1] / "app/routers/customer_portal.py").read_text()
        for action in (
            "customer.portal.dashboard_viewed",
            "customer.service.viewed",
            "customer.subscription.viewed",
            "customer.payment.viewed",
            "customer.payment.view_denied",
            "customer.ticket.viewed",
            "customer.ticket.view_denied",
        ):
            self.assertNotIn(action, source)


if __name__ == "__main__":
    unittest.main()
