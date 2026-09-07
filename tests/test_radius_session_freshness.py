from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException, Request
from sqlalchemy import BigInteger, create_engine, func, select
from sqlalchemy.dialects.postgresql import INET, dialect as postgresql_dialect
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.core.platform_auth import require_platform_principal
from app.core.tenant import OrganizationContext
from app.database import Base
from app.models import (
    Customer,
    ExpiryDisconnectJob,
    ExpiryScanRun,
    NetworkAccessServer,
    Organization,
    Platform,
    RadAcct,
    RadCheck,
    RadiusRejectOwnership,
    ServicePlan,
    Subscription,
    User,
    Zone,
)
from app.routers import customer_portal, network, platform, radius_sessions
from app.services import expiry_worker
from app.services.expiry_policy import AccessReason
from app.services.radius_session_freshness import (
    fresh_active_session_conditions,
    is_fresh_active_session,
    session_freshness_cutoff,
)


NOW = datetime(2028, 2, 29, 12, 0, tzinfo=timezone.utc)


@compiles(INET, "sqlite")
def _compile_inet_for_isolated_sqlite(_type, _compiler, **_kwargs):
    """The deterministic consumer fixture stores PostgreSQL INET values as text."""

    return "TEXT"


@compiles(BigInteger, "sqlite")
def _compile_big_integer_for_isolated_sqlite(_type, _compiler, **_kwargs):
    return "INTEGER"


def session(**overrides):
    values = {
        "acctstarttime": NOW - timedelta(hours=1),
        "acctupdatetime": NOW - timedelta(minutes=1),
        "acctstoptime": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [
                (b"x-request-id", b"request-123"),
                (b"authorization", b"Bearer prohibited-token"),
                (b"cookie", b"session=prohibited-cookie"),
                (b"user-agent", b"prohibited-personal-agent"),
            ],
        }
    )


class RadiusSessionFreshnessTests(unittest.TestCase):
    def test_default_cutoff_is_fifteen_minutes(self):
        self.assertEqual(session_freshness_cutoff(NOW), NOW - timedelta(minutes=15))

    def test_recent_interim_update_is_online(self):
        self.assertTrue(is_fresh_active_session(session(), now=NOW))

    def test_exact_boundary_is_online(self):
        self.assertTrue(
            is_fresh_active_session(
                session(acctupdatetime=NOW - timedelta(seconds=900)), now=NOW
            )
        )

    def test_just_inside_and_outside_boundary(self):
        self.assertTrue(
            is_fresh_active_session(
                session(acctupdatetime=NOW - timedelta(seconds=899, microseconds=999999)),
                now=NOW,
            )
        )
        self.assertFalse(
            is_fresh_active_session(
                session(acctupdatetime=NOW - timedelta(seconds=900, microseconds=1)),
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
        self.assertFalse(
            is_fresh_active_session(
                session(acctstarttime=NOW - timedelta(minutes=16), acctupdatetime=None),
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

    def test_production_sql_uses_postgresql_clock_and_bound_interval(self):
        statement = select(func.count()).select_from(RadAcct).where(
            *fresh_active_session_conditions()
        )
        compiled = statement.compile(dialect=postgresql_dialect())
        sql = str(compiled)
        self.assertIn("radacct.acctstoptime IS NULL", sql)
        self.assertIn("coalesce(radacct.acctupdatetime, radacct.acctstarttime)", sql)
        self.assertIn("CURRENT_TIMESTAMP - %(radius_session_freshness_interval)s", sql)
        self.assertEqual(
            compiled.params["radius_session_freshness_interval"],
            timedelta(seconds=900),
        )

    def test_explicit_test_clock_uses_bound_cutoff_not_database_clock(self):
        statement = select(func.count()).select_from(RadAcct).where(
            *fresh_active_session_conditions(NOW)
        )
        compiled = statement.compile(dialect=postgresql_dialect())
        self.assertNotIn("CURRENT_TIMESTAMP", str(compiled))
        self.assertEqual(
            compiled.params["radius_session_freshness_cutoff"],
            NOW - timedelta(seconds=900),
        )


class FreshnessConsumerDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(
            cls.engine,
            tables=[
                Platform.__table__,
                Organization.__table__,
                Subscription.__table__,
                Customer.__table__,
                User.__table__,
                ServicePlan.__table__,
                Zone.__table__,
                NetworkAccessServer.__table__,
                RadAcct.__table__,
                RadCheck.__table__,
                ExpiryScanRun.__table__,
                ExpiryDisconnectJob.__table__,
                RadiusRejectOwnership.__table__,
            ],
        )
        cls.Session = sessionmaker(bind=cls.engine, future=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.db = self.Session()
        for table in reversed(
            [
                Platform.__table__,
                Organization.__table__,
                Subscription.__table__,
                Customer.__table__,
                User.__table__,
                ServicePlan.__table__,
                Zone.__table__,
                NetworkAccessServer.__table__,
                RadAcct.__table__,
                RadCheck.__table__,
                ExpiryScanRun.__table__,
                ExpiryDisconnectJob.__table__,
                RadiusRejectOwnership.__table__,
            ]
        ):
            self.db.execute(table.delete())
        self._seed_tenants_and_sessions()
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def _seed_tenants_and_sessions(self):
        self.db.add(Platform(id=1, name="RadiusFiber"))
        self.db.add_all(
            [
                Organization(id=1, platform_id=1, name="Tenant One", slug="tenant-one"),
                Organization(id=2, platform_id=1, name="Tenant Two", slug="tenant-two"),
            ]
        )
        self.db.add_all(
            [
                ServicePlan(
                    id=1, organization_id=1, name="Pilot", rate_limit="10M/10M",
                    duration_days=30, status="active",
                ),
                ServicePlan(
                    id=2, organization_id=2, name="Pilot", rate_limit="10M/10M",
                    duration_days=30, status="active",
                ),
            ]
        )
        self.db.add_all(
            [
                Customer(
                    id="customer-one", organization_id=1, tenant_id="tenant-one",
                    name="Customer One", email="one@example.test", phone="100",
                    address="Test", latitude=0, longitude=0,
                ),
                Customer(
                    id="customer-two", organization_id=2, tenant_id="tenant-two",
                    name="Customer Two", email="two@example.test", phone="200",
                    address="Test", latitude=0, longitude=0,
                ),
            ]
        )
        self.db.add_all(
            [
                User(
                    id=11, organization_id=1, username="TEST_ONU", password="test-hash",
                    customer_id="customer-one", service_plan="Pilot", zone="Pilot", status="active",
                ),
                User(
                    id=12, organization_id=2, username="OTHER_TENANT", password="test-hash",
                    customer_id="customer-two", service_plan="Pilot", zone="Pilot", status="active",
                ),
            ]
        )
        self.db.add_all(
            [
                Zone(id=1, organization_id=1, name="Shared Name"),
                Zone(id=2, organization_id=2, name="Shared Name"),
                NetworkAccessServer(
                    id=1, organization_id=1, zone_id=1, nas_ip_address="192.0.2.1",
                    display_name="Tenant One NAS", status="active",
                ),
                NetworkAccessServer(
                    id=2, organization_id=2, zone_id=2, nas_ip_address="192.0.2.1",
                    display_name="Tenant Two NAS", status="active",
                ),
            ]
        )
        rows = [
            self._radacct(
                row_id=index,
                username="TEST_ONU",
                updated_at=NOW - timedelta(minutes=16 + index),
            )
            for index in range(1, 23)
        ]
        rows.extend(
            [
                self._radacct(23, "TEST_ONU", NOW - timedelta(minutes=1)),
                self._radacct(
                    24,
                    "TEST_ONU",
                    NOW - timedelta(seconds=30),
                    stopped_at=NOW - timedelta(seconds=10),
                ),
                self._radacct(25, "OTHER_TENANT", NOW - timedelta(minutes=1)),
            ]
        )
        self.db.add_all(rows)

    @staticmethod
    def _radacct(
        row_id: int,
        username: str,
        updated_at: datetime | None,
        *,
        started_at: datetime | None = None,
        stopped_at: datetime | None = None,
    ) -> RadAcct:
        return RadAcct(
            radacctid=row_id,
            acctsessionid=f"session-{row_id}",
            acctuniqueid=f"unique-{row_id}",
            username=username,
            nasipaddress="192.0.2.1",
            calledstationid="core-radius-pilot",
            acctstarttime=started_at or NOW - timedelta(hours=1),
            acctupdatetime=updated_at,
            acctstoptime=stopped_at,
        )

    @staticmethod
    def _fixed_conditions():
        return fresh_active_session_conditions(NOW)

    def test_twenty_two_stale_plus_one_fresh_query_returns_one(self):
        count = (
            self.db.query(RadAcct)
            .filter(RadAcct.username == "TEST_ONU", *self._fixed_conditions())
            .count()
        )
        self.assertEqual(count, 1)

    def test_null_update_fallback_and_exact_boundary_query_behavior(self):
        missing = self._radacct(35, "MISSING", None)
        missing.acctstarttime = None
        self.db.add_all(
            [
                self._radacct(30, "FRESH_START", None, started_at=NOW - timedelta(minutes=2)),
                self._radacct(31, "STALE_START", None, started_at=NOW - timedelta(minutes=16)),
                self._radacct(32, "BOUNDARY", NOW - timedelta(seconds=900)),
                self._radacct(33, "INSIDE", NOW - timedelta(seconds=899, microseconds=999999)),
                self._radacct(34, "OUTSIDE", NOW - timedelta(seconds=900, microseconds=1)),
                missing,
            ]
        )
        self.db.flush()
        qualifying = {
            row.username
            for row in self.db.query(RadAcct).filter(*self._fixed_conditions()).all()
        }
        self.assertTrue({"FRESH_START", "BOUNDARY", "INSIDE"}.issubset(qualifying))
        self.assertTrue({"STALE_START", "OUTSIDE", "MISSING"}.isdisjoint(qualifying))

    def test_platform_total_is_global_but_guard_requires_platform_authority(self):
        with patch(
            "app.core.platform_auth.settings",
            SimpleNamespace(platform_admin_api_key="platform-test-key"),
        ):
            with self.assertRaises(HTTPException) as denied:
                require_platform_principal(authorization=None, x_platform_admin_key=None)
            self.assertEqual(denied.exception.status_code, 401)
            principal = require_platform_principal(
                authorization=None,
                x_platform_admin_key="platform-test-key",
            )
            self.assertTrue(principal.platform_authority)
        with patch.object(platform, "fresh_active_session_conditions", self._fixed_conditions):
            result = platform.dashboard(self.db)
        self.assertEqual(result["online_sessions"], 2)

    def test_organization_listing_is_tenant_scoped_and_filters_before_limit(self):
        self.db.add_all(
            [
                self._radacct(
                    1000 + index,
                    "TEST_ONU",
                    NOW - timedelta(minutes=16),
                    started_at=NOW + timedelta(seconds=index),
                )
                for index in range(501)
            ]
        )
        self.db.flush()
        context = OrganizationContext(
            id=1, slug="tenant-one", name="Tenant One", principal_id="staff-1",
            roles=("Organization Admin",), permissions=("radius.sessions.read",),
        )
        with patch.object(
            radius_sessions,
            "fresh_active_session_conditions",
            self._fixed_conditions,
        ):
            rows = radius_sessions.list_active_sessions(self.db, context)
        self.assertEqual([row.username for row in rows], ["TEST_ONU"])

    def test_shared_nas_ip_counts_are_tenant_scoped(self):
        with patch.object(network, "fresh_active_session_conditions", self._fixed_conditions):
            tenant_one = network._active_session_count(self.db, 1, "192.0.2.1")
            tenant_two = network._active_session_count(self.db, 2, "192.0.2.1")
        self.assertEqual(tenant_one, 1)
        self.assertEqual(tenant_two, 1)

    def test_customer_online_state_ignores_other_tenant_and_stale_rows(self):
        with patch.object(
            customer_portal,
            "fresh_active_session_conditions",
            self._fixed_conditions,
        ):
            self.assertTrue(customer_portal._online(self.db, "TEST_ONU"))
            self.db.query(RadAcct).filter(RadAcct.radacctid == 23).update(
                {RadAcct.acctupdatetime: NOW - timedelta(minutes=16)}
            )
            self.db.flush()
            self.assertFalse(customer_portal._online(self.db, "TEST_ONU"))
            self.assertTrue(customer_portal._online(self.db, "OTHER_TENANT"))

    def test_expiry_selection_and_job_revalidation_require_same_fresh_session(self):
        fresh = expiry_worker._latest_fresh_session(self.db, "TEST_ONU", test_now=NOW)
        self.assertEqual(fresh.radacctid, 23)
        self.assertIsNone(expiry_worker._latest_fresh_session(self.db, "UNKNOWN", test_now=NOW))
        self.assertEqual(
            expiry_worker._latest_fresh_session(self.db, "OTHER_TENANT", test_now=NOW).radacctid,
            25,
        )
        self.assertEqual(
            expiry_worker._fresh_job_session(
                self.db, radacct_id=23, username="TEST_ONU", test_now=NOW
            ).radacctid,
            23,
        )
        self.assertIsNone(
            expiry_worker._fresh_job_session(
                self.db, radacct_id=23, username="OTHER_TENANT", test_now=NOW
            )
        )
        self.db.query(RadAcct).filter(RadAcct.radacctid == 23).update(
            {RadAcct.acctstoptime: NOW}
        )
        self.db.flush()
        self.assertIsNone(
            expiry_worker._fresh_job_session(
                self.db, radacct_id=23, username="TEST_ONU", test_now=NOW
            )
        )

    def test_stale_open_session_cannot_generate_disconnect_job(self):
        self.db.query(User).filter(User.id == 11).update(
            {User.expiration_date: NOW - timedelta(days=1)}
        )
        self.db.add(
            User(
                id=13, organization_id=1, username="STALE_SERVICE", password="test-hash",
                customer_id="customer-one", service_plan="Pilot", zone="Pilot", status="active",
                expiration_date=NOW - timedelta(days=1),
            )
        )
        self.db.add(
            self._radacct(26, "STALE_SERVICE", NOW - timedelta(minutes=16))
        )
        self.db.query(User).filter(User.id == 12).update(
            {User.expiration_date: NOW - timedelta(days=1)}
        )
        self.db.flush()
        expired = SimpleNamespace(reason=AccessReason.EXPIRED, expires_at=NOW - timedelta(days=1))
        with (
            patch("app.services.expiry_worker.synchronize_radius_authorization", return_value=expired),
            patch("app.services.expiry_worker.record_audit"),
        ):
            summary = expiry_worker.scan_expired_services(
                self.db,
                now=NOW,
                organization_id=1,
                batch_size=10,
                dry_run=False,
                acquire_lock=False,
            )
        jobs = self.db.query(ExpiryDisconnectJob).all()
        self.assertEqual(summary.active_sessions, 1)
        self.assertEqual(summary.jobs_queued, 1)
        self.assertEqual([(job.organization_id, job.user_id) for job in jobs], [(1, 11)])

    def test_worker_defaults_remain_disabled_and_dry_run(self):
        self.assertFalse(expiry_worker.settings.expiry_worker_enabled)
        self.assertTrue(expiry_worker.settings.expiry_worker_dry_run)


class CustomerPortalSecurityEventTests(unittest.TestCase):
    def setUp(self):
        self.context = SimpleNamespace(
            customer=SimpleNamespace(id="customer-one"),
            organization=SimpleNamespace(id=1),
        )

    def _assert_denial(self, handler, resource_id: int, path: str, expected_event: str):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        with self.assertLogs("radiusfiber.security", level="WARNING") as captured:
            with self.assertRaises(HTTPException) as denied:
                handler(resource_id, _request(path), db, self.context)
        self.assertEqual(denied.exception.status_code, 404)
        db.commit.assert_not_called()
        db.flush.assert_not_called()
        db.add.assert_not_called()
        self.assertEqual(len(captured.records), 1)
        event = json.loads(captured.records[0].getMessage())
        self.assertEqual(event["event_name"], expected_event)
        self.assertEqual(event["correlation_id"], "request-123")
        self.assertEqual(event["principal_type"], "customer")
        self.assertEqual(event["subject_id"], "customer-one")
        self.assertEqual(event["organization_id"], 1)
        self.assertEqual(event["resource_id"], str(resource_id))
        self.assertEqual(event["method"], "GET")
        self.assertEqual(event["status_code"], 404)
        serialized = json.dumps(event)
        for prohibited in (
            "prohibited-token",
            "prohibited-cookie",
            "prohibited-personal-agent",
            "authorization",
            "cookie",
        ):
            self.assertNotIn(prohibited, serialized)

    def test_payment_denial_is_logged_once_without_database_write(self):
        self._assert_denial(
            customer_portal.get_payment,
            4041,
            "/customer-portal/payments/4041",
            "customer.payment.view_denied",
        )

    def test_ticket_denial_is_logged_once_without_database_write(self):
        self._assert_denial(
            customer_portal.get_ticket,
            4042,
            "/customer-portal/tickets/4042",
            "customer.ticket.view_denied",
        )

    def test_logging_failure_preserves_safe_404_and_uses_constant_fallback(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        fallback = io.StringIO()
        with (
            patch("app.services.security_events.logger.warning", side_effect=RuntimeError("failed")),
            patch("app.services.security_events.sys.stderr", fallback),
            self.assertRaises(HTTPException) as denied,
        ):
            customer_portal.get_payment(
                4043,
                _request("/customer-portal/payments/4043"),
                db,
                self.context,
            )
        self.assertEqual(denied.exception.status_code, 404)
        self.assertEqual(fallback.getvalue(), "radiusfiber_security_event_emission_failed\n")
        db.commit.assert_not_called()
        db.flush.assert_not_called()
        db.add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
