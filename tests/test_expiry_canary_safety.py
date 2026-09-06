from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

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
    RadReply,
    RadiusRejectOwnership,
    ServicePlan,
    User,
)
from app.scripts import expiry_worker as expiry_cli
from app.routers.users import sync_radius_username
from app.services.expiry_worker import scan_expired_services
from app.services.radius_authorization import synchronize_radius_authorization


NOW = datetime(2028, 2, 29, 12, 0, tzinfo=timezone.utc)
CANARY_TABLES = [
    Platform.__table__,
    Organization.__table__,
    Customer.__table__,
    ServicePlan.__table__,
    User.__table__,
    RadCheck.__table__,
    RadReply.__table__,
    NetworkAccessServer.__table__,
    RadAcct.__table__,
    ExpiryScanRun.__table__,
    ExpiryDisconnectJob.__table__,
    RadiusRejectOwnership.__table__,
]


class ExpiryCanaryDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        cls.tables = CANARY_TABLES
        Base.metadata.create_all(cls.engine, tables=cls.tables)
        cls.Session = sessionmaker(bind=cls.engine, future=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.db = self.Session()
        for table in reversed(self.tables):
            self.db.execute(table.delete())
        self.db.add(Platform(id=1, name="RadiusFiber"))
        self.db.flush()
        self.db.add_all(
            [
                Organization(id=1, platform_id=1, name="Tenant One", slug="tenant-one"),
                Organization(id=2, platform_id=1, name="Tenant Two", slug="tenant-two"),
            ]
        )
        self.db.flush()
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
        self.db.flush()
        self.db.add_all(
            [
                Customer(
                    id="fixture-one", organization_id=1, tenant_id="tenant-one",
                    name="Fixture One", email="one@example.test", phone="100",
                    address="Test", latitude=0, longitude=0,
                ),
                Customer(
                    id="fixture-two", organization_id=2, tenant_id="tenant-two",
                    name="Fixture Two", email="two@example.test", phone="200",
                    address="Test", latitude=0, longitude=0,
                ),
            ]
        )
        self.db.flush()
        self.db.add_all(
            [
                User(
                    id=11, organization_id=1, username="TEST_ONU", password="test-hash",
                    customer_id="fixture-one", service_plan="Pilot", zone="Pilot",
                    status="active", expiration_date=NOW - timedelta(days=1),
                ),
                User(
                    id=12, organization_id=1, username="SAME_TENANT", password="test-hash",
                    customer_id="fixture-one", service_plan="Pilot", zone="Pilot",
                    status="active", expiration_date=NOW - timedelta(days=1),
                ),
                User(
                    id=21, organization_id=2, username="OTHER_TENANT", password="test-hash",
                    customer_id="fixture-two", service_plan="Pilot", zone="Pilot",
                    status="active", expiration_date=NOW - timedelta(days=1),
                ),
            ]
        )
        self.db.flush()
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def _counts(self):
        return tuple(
            self.db.query(model).count()
            for model in (
                RadCheck,
                RadReply,
                RadiusRejectOwnership,
                ExpiryScanRun,
                ExpiryDisconnectJob,
            )
        )

    def _service_and_plan(self):
        service = self.db.query(User).filter(User.id == 11, User.organization_id == 1).one()
        plan = self.db.query(ServicePlan).filter(
            ServicePlan.organization_id == 1,
            ServicePlan.name == "Pilot",
        ).one()
        return service, plan

    def test_exact_dry_run_is_non_mutating_and_selects_only_the_full_identity(self):
        before = self._counts()
        summary = scan_expired_services(
            self.db,
            now=NOW,
            organization_id=1,
            user_id=11,
            username="TEST_ONU",
            dry_run=True,
        )
        self.assertEqual(summary.evaluated, 1)
        self.assertEqual(summary.newly_expired, 1)
        self.assertEqual(summary.jobs_queued, 0)
        self.assertEqual(self._counts(), before)
        self.assertFalse(self.db.new)
        self.assertFalse(self.db.dirty)
        self.assertFalse(self.db.deleted)

    def test_target_mismatch_selects_nothing_and_never_crosses_tenants(self):
        mismatched = scan_expired_services(
            self.db, now=NOW, organization_id=1, user_id=11,
            username="OTHER_TENANT", dry_run=True,
        )
        cross_tenant = scan_expired_services(
            self.db, now=NOW, organization_id=2, user_id=11,
            username="TEST_ONU", dry_run=True,
        )
        self.assertEqual(mismatched.evaluated, 0)
        self.assertEqual(cross_tenant.evaluated, 0)

    def test_partial_or_unscoped_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "both user_id and username"):
            scan_expired_services(self.db, user_id=11, dry_run=True)
        with self.assertRaisesRegex(ValueError, "requires organization_id"):
            scan_expired_services(
                self.db, user_id=11, username="TEST_ONU", dry_run=True
            )

    def test_expiry_owned_reject_is_removed_after_renewal(self):
        service, plan = self._service_and_plan()
        synchronize_radius_authorization(self.db, service, plan, now=NOW)
        self.db.flush()
        ownership = self.db.query(RadiusRejectOwnership).one()
        owned_id = ownership.radcheck_id
        self.assertEqual(ownership.reason_code, "EXPIRED")
        self.assertEqual(
            self.db.query(RadCheck).filter(RadCheck.id == owned_id).one().value,
            "Reject",
        )

        service.expiration_date = NOW + timedelta(days=30)
        synchronize_radius_authorization(self.db, service, plan, now=NOW)
        self.db.flush()
        self.assertEqual(self.db.query(RadiusRejectOwnership).count(), 0)
        self.assertIsNone(self.db.query(RadCheck).filter(RadCheck.id == owned_id).first())

    def test_manual_reject_is_never_claimed_or_removed(self):
        service, plan = self._service_and_plan()
        manual = RadCheck(
            username="TEST_ONU", attribute="Auth-Type", op=":=", value="Reject"
        )
        self.db.add(manual)
        self.db.flush()
        manual_id = manual.id

        synchronize_radius_authorization(self.db, service, plan, now=NOW)
        self.db.flush()
        self.assertEqual(self.db.query(RadiusRejectOwnership).count(), 0)

        service.expiration_date = NOW + timedelta(days=30)
        synchronize_radius_authorization(self.db, service, plan, now=NOW)
        self.db.flush()
        self.assertIsNotNone(self.db.query(RadCheck).filter(RadCheck.id == manual_id).first())

    def test_renewal_removes_owned_row_but_preserves_later_manual_row(self):
        service, plan = self._service_and_plan()
        synchronize_radius_authorization(self.db, service, plan, now=NOW)
        self.db.flush()
        owned_id = self.db.query(RadiusRejectOwnership).one().radcheck_id
        manual = RadCheck(
            username="TEST_ONU", attribute="Auth-Type", op=":=", value="Reject"
        )
        self.db.add(manual)
        self.db.flush()
        manual_id = manual.id

        service.expiration_date = NOW + timedelta(days=30)
        synchronize_radius_authorization(self.db, service, plan, now=NOW)
        self.db.flush()
        self.assertIsNone(self.db.query(RadCheck).filter(RadCheck.id == owned_id).first())
        self.assertIsNotNone(self.db.query(RadCheck).filter(RadCheck.id == manual_id).first())

    def test_owned_reject_tracks_an_authorized_username_change(self):
        service, plan = self._service_and_plan()
        synchronize_radius_authorization(self.db, service, plan, now=NOW)
        self.db.flush()
        owned_id = self.db.query(RadiusRejectOwnership).one().radcheck_id

        sync_radius_username(self.db, "TEST_ONU", "TEST_ONU_RENAMED")
        service.username = "TEST_ONU_RENAMED"
        service.expiration_date = NOW + timedelta(days=30)
        self.db.flush()
        synchronize_radius_authorization(self.db, service, plan, now=NOW)
        self.db.flush()

        self.assertEqual(self.db.query(RadiusRejectOwnership).count(), 0)
        self.assertIsNone(self.db.query(RadCheck).filter(RadCheck.id == owned_id).first())


class ExpiryCanaryCliTests(unittest.TestCase):
    def test_postgresql_dry_run_sets_read_only_and_rolls_back(self):
        db = MagicMock()
        db.get_bind.return_value.dialect.name = "postgresql"
        db.new = set()
        db.dirty = set()
        db.deleted = set()
        context = MagicMock()
        context.__enter__.return_value = db
        context.__exit__.return_value = False
        summary = SimpleNamespace(
            correlation_id="test", acquired_lock=True, dry_run=True, evaluated=1,
            newly_expired=1, active_sessions=0, jobs_queued=0, errors=0, duration_ms=1,
        )
        with (
            patch.object(expiry_cli, "SessionLocal", return_value=context),
            patch.object(expiry_cli, "scan_expired_services", return_value=summary) as scan,
            patch.object(sys, "argv", [
                "expiry_worker", "scan", "--dry-run", "--organization-id", "1",
                "--user-id", "11", "--username", "TEST_ONU",
            ]),
        ):
            self.assertEqual(expiry_cli.main(), 0)
        self.assertEqual(str(db.execute.call_args.args[0]), "SET TRANSACTION READ ONLY")
        scan.assert_called_once_with(
            db, organization_id=1, user_id=11, username="TEST_ONU", dry_run=True
        )
        db.rollback.assert_called_once_with()
        db.commit.assert_not_called()

    def test_cli_refuses_incomplete_target(self):
        with patch.object(
            sys,
            "argv",
            ["expiry_worker", "scan", "--dry-run", "--organization-id", "1", "--user-id", "11"],
        ):
            with self.assertRaisesRegex(SystemExit, "both --user-id and --username"):
                expiry_cli.main()


POSTGRES_URL = os.getenv("RADIUSFIBER_DISPOSABLE_TEST_DATABASE_URL")


@unittest.skipUnless(POSTGRES_URL, "disposable PostgreSQL URL is not configured")
class ExpiryCanaryPostgresTests(ExpiryCanaryDatabaseTests):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(POSTGRES_URL, future=True)
        cls.tables = CANARY_TABLES
        cls.Session = sessionmaker(bind=cls.engine, future=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def test_database_read_only_transaction_enforces_non_mutation(self):
        self.db.execute(text("SET TRANSACTION READ ONLY"))
        before = self._counts()
        summary = scan_expired_services(
            self.db,
            now=NOW,
            organization_id=1,
            user_id=11,
            username="TEST_ONU",
            dry_run=True,
        )
        self.assertEqual(summary.evaluated, 1)
        self.assertEqual(self._counts(), before)
        self.db.add(
            ExpiryScanRun(
                correlation_id="must-not-write",
                component="test",
                status="running",
                dry_run=True,
            )
        )
        with self.assertRaises(DBAPIError):
            self.db.flush()
        self.db.rollback()


class ExpiryOwnershipMigrationTests(unittest.TestCase):
    def test_migration_is_single_head_and_fail_closed_on_downgrade(self):
        from pathlib import Path

        source = (
            Path(__file__).parents[1]
            / "alembic"
            / "versions"
            / "0015_expiry_reject_ownership.py"
        ).read_text()
        self.assertIn('revision = "0015_expiry_reject_ownership"', source)
        self.assertIn('down_revision = "0014_olt_inventory_foundation"', source)
        self.assertIn("fk_radius_reject_ownership_user_tenant", source)
        self.assertIn("uq_radius_reject_ownership_radcheck", source)
        self.assertNotIn("DELETE FROM radcheck", source)


if __name__ == "__main__":
    unittest.main()
