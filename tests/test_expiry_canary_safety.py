from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    AuditLog,
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
from app.scripts import reject_reconciliation as reconciliation_cli
from app.routers.radius_sessions import RadiusDisconnectRequest, disconnect_session
from app.routers.users import activate_user
from app.core.tenant import OrganizationContext
from app.routers.users import sync_radius_username
from app.services.expiry_worker import scan_expired_services
from app.services.radius_authorization import (
    MANUAL_DISCONNECT_REASON,
    RejectOwnershipConflict,
    ensure_owned_reject,
    synchronize_radius_authorization,
)
from app.services.reject_reconciliation import reconcile_legacy_reject


NOW = datetime(2028, 2, 29, 12, 0, tzinfo=timezone.utc)


@compiles(INET, "sqlite")
def _compile_inet_for_sqlite(_type, _compiler, **_kw):
    return "TEXT"


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
    AuditLog.__table__,
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
        self.assertEqual(summary.matched, 1)
        self.assertFalse(summary.acquired_lock)
        self.assertTrue(summary.lock_skipped)
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

        with self.assertRaises(RejectOwnershipConflict):
            synchronize_radius_authorization(self.db, service, plan, now=NOW)
        self.assertEqual(self.db.query(RadiusRejectOwnership).count(), 0)

        service.expiration_date = NOW + timedelta(days=30)
        with self.assertRaises(RejectOwnershipConflict):
            synchronize_radius_authorization(self.db, service, plan, now=NOW)
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
        with self.assertRaises(RejectOwnershipConflict):
            synchronize_radius_authorization(self.db, service, plan, now=NOW)
        self.assertIsNotNone(self.db.query(RadCheck).filter(RadCheck.id == owned_id).first())
        self.assertIsNotNone(self.db.query(RadCheck).filter(RadCheck.id == manual_id).first())

    def test_manual_disconnect_and_activate_are_owned_idempotent_and_restore_authentication(self):
        service, _ = self._service_and_plan()
        service.expiration_date = NOW + timedelta(days=30)
        self.db.add(RadAcct(
            radacctid=1, acctsessionid="stopped", acctuniqueid="stopped-1",
            username=service.username, nasipaddress="192.0.2.1",
            calledstationid="core-radius-pilot", acctstarttime=NOW - timedelta(hours=1),
            acctupdatetime=NOW - timedelta(hours=1), acctstoptime=NOW - timedelta(minutes=30),
        ))
        self.db.commit()
        organization = OrganizationContext(1, "tenant-one", "Tenant One", "operator", (), ())
        disconnect_settings = SimpleNamespace(radius_coa_enabled=True, radius_disconnect_mode="real")
        with (
            patch("app.routers.radius_sessions.settings", disconnect_settings),
        ):
            first = disconnect_session(RadiusDisconnectRequest(username="TEST_ONU"), self.db, organization)
            second = disconnect_session(RadiusDisconnectRequest(username="TEST_ONU"), self.db, organization)
        self.assertIn("blocked", first.message)
        self.assertIn("blocked", second.message)
        ownerships = self.db.query(RadiusRejectOwnership).all()
        self.assertEqual([item.reason_code for item in ownerships], [MANUAL_DISCONNECT_REASON])
        self.assertEqual(self.db.query(RadCheck).filter(RadCheck.attribute == "Auth-Type").count(), 1)

        response = activate_user(11, self.db, organization)
        self.assertIn("restored", response.message)
        self.assertEqual(self.db.query(RadiusRejectOwnership).count(), 0)
        self.assertEqual(self.db.query(RadCheck).filter(RadCheck.attribute == "Auth-Type").count(), 0)
        repeated = activate_user(11, self.db, organization)
        self.assertIn("restored", repeated.message)

    def test_expiry_and_manual_rejects_remain_distinct_during_activation(self):
        service, plan = self._service_and_plan()
        service.expiration_date = datetime.now(timezone.utc) - timedelta(days=1)
        synchronize_radius_authorization(self.db, service, plan, now=NOW)
        ensure_owned_reject(self.db, service, MANUAL_DISCONNECT_REASON)
        service.status = "suspended"
        self.db.commit()
        organization = OrganizationContext(1, "tenant-one", "Tenant One", "operator", (), ())
        with self.assertRaises(HTTPException) as raised:
            activate_user(11, self.db, organization)
        self.assertEqual(raised.exception.status_code, 409)
        reasons = {row.reason_code for row in self.db.query(RadiusRejectOwnership).all()}
        self.assertEqual(reasons, {"EXPIRED"})
        self.assertEqual(self.db.query(RadCheck).filter(RadCheck.attribute == "Auth-Type").count(), 1)

    def test_unknown_reject_blocks_disconnect_and_activation_without_claim_or_removal(self):
        service, _ = self._service_and_plan()
        service.expiration_date = NOW + timedelta(days=30)
        unknown = RadCheck(username=service.username, attribute="Auth-Type", op=":=", value="Reject")
        self.db.add(unknown)
        self.db.commit()
        unknown_id = unknown.id
        organization = OrganizationContext(1, "tenant-one", "Tenant One", "operator", (), ())
        disconnect_settings = SimpleNamespace(radius_coa_enabled=True, radius_disconnect_mode="real")
        with (
            patch("app.routers.radius_sessions.settings", disconnect_settings),
            self.assertRaises(HTTPException) as disconnect_error,
        ):
            disconnect_session(RadiusDisconnectRequest(username="TEST_ONU"), self.db, organization)
        self.assertEqual(disconnect_error.exception.status_code, 409)
        with self.assertRaises(HTTPException) as activation_error:
            activate_user(11, self.db, organization)
        self.assertEqual(activation_error.exception.status_code, 409)
        self.assertEqual(self.db.query(RadiusRejectOwnership).count(), 0)
        self.assertIsNotNone(self.db.query(RadCheck).filter(RadCheck.id == unknown_id).one_or_none())

    def test_radius_and_ownership_writes_rollback_together(self):
        service, _ = self._service_and_plan()
        try:
            ensure_owned_reject(self.db, service, MANUAL_DISCONNECT_REASON)
            raise RuntimeError("simulated failure after ownership staging")
        except RuntimeError:
            self.db.rollback()
        self.assertEqual(self.db.query(RadCheck).filter(RadCheck.attribute == "Auth-Type").count(), 0)
        self.assertEqual(self.db.query(RadiusRejectOwnership).count(), 0)

    def test_legacy_reconciliation_is_exact_read_only_and_auditable_when_applied(self):
        service, _ = self._service_and_plan()
        legacy = RadCheck(username=service.username, attribute="Auth-Type", op=":=", value="Reject")
        self.db.add(legacy)
        self.db.commit()
        report = reconcile_legacy_reject(
            self.db, organization_id=1, user_id=11, username="TEST_ONU",
            radcheck_id=legacy.id,
        )
        self.assertEqual(report.ownership_state, "unowned_conflict")
        self.assertFalse(report.applied)
        self.assertEqual(self.db.query(AuditLog).count(), 0)
        adopted = reconcile_legacy_reject(
            self.db, organization_id=1, user_id=11, username="TEST_ONU",
            radcheck_id=legacy.id, action="adopt", reason_code=MANUAL_DISCONNECT_REASON,
            evidence_reference="CHANGE-OLT-PR14-001", operator_id="operator-001", apply=True,
        )
        self.db.commit()
        self.assertTrue(adopted.applied)
        self.assertEqual(self.db.query(RadiusRejectOwnership).one().radcheck_id, legacy.id)
        self.assertEqual(self.db.query(AuditLog).filter(
            AuditLog.action == "radius.reject_legacy_adopt"
        ).count(), 1)
        with self.assertRaises(RejectOwnershipConflict):
            reconcile_legacy_reject(
                self.db, organization_id=2, user_id=11, username="TEST_ONU",
                radcheck_id=legacy.id,
            )

        removed = reconcile_legacy_reject(
            self.db, organization_id=1, user_id=11, username="TEST_ONU",
            radcheck_id=legacy.id, action="remove",
            evidence_reference="CHANGE-OLT-PR14-002", operator_id="operator-001", apply=True,
        )
        self.db.commit()
        self.assertEqual(removed.ownership_state, "removed")
        self.assertIsNone(self.db.query(RadCheck).filter(RadCheck.id == legacy.id).one_or_none())
        self.assertEqual(self.db.query(RadiusRejectOwnership).count(), 0)
        self.assertEqual(self.db.query(AuditLog).filter(
            AuditLog.action == "radius.reject_legacy_remove"
        ).count(), 1)

    def test_lock_telemetry_distinguishes_dry_run_and_unavailable(self):
        with patch("app.services.expiry_worker._try_worker_lock") as worker_lock:
            dry = scan_expired_services(
                self.db, now=NOW, organization_id=1, user_id=11,
                username="TEST_ONU", dry_run=True,
            )
        worker_lock.assert_not_called()
        self.assertFalse(dry.acquired_lock)
        self.assertTrue(dry.lock_skipped)

        with patch("app.services.expiry_worker._try_worker_lock", return_value=False):
            unavailable = scan_expired_services(
                self.db, now=NOW, organization_id=1, user_id=11,
                username="TEST_ONU", dry_run=False,
            )
        self.assertFalse(unavailable.acquired_lock)
        self.assertFalse(unavailable.lock_skipped)
        self.assertEqual(unavailable.evaluated, 0)


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
    def test_legacy_report_cli_uses_read_only_transaction_and_rolls_back(self):
        db = MagicMock()
        db.get_bind.return_value.dialect.name = "postgresql"
        db.new = set()
        db.dirty = set()
        db.deleted = set()
        context = MagicMock()
        context.__enter__.return_value = db
        context.__exit__.return_value = False
        report = SimpleNamespace(as_dict=lambda: {
            "ownership_state": "unowned_conflict", "applied": False,
        })
        with (
            patch.object(reconciliation_cli, "SessionLocal", return_value=context),
            patch.object(reconciliation_cli, "reconcile_legacy_reject", return_value=report),
            patch.object(sys, "argv", [
                "reject_reconciliation", "--organization-id", "1", "--user-id", "11",
                "--username", "TEST_ONU", "--radcheck-id", "99",
            ]),
        ):
            self.assertEqual(reconciliation_cli.main(), 0)
        self.assertEqual(str(db.execute.call_args.args[0]), "SET TRANSACTION READ ONLY")
        db.rollback.assert_called_once_with()
        db.commit.assert_not_called()

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
            correlation_id="test", acquired_lock=False, lock_skipped=True, dry_run=True,
            matched=1, evaluated=1, changed=1, disconnected=0, newly_expired=1,
            active_sessions=0, jobs_queued=0, errors=0, duration_ms=1,
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
            with self.assertRaises(SystemExit) as raised:
                expiry_cli.main()
        self.assertEqual(raised.exception.code, expiry_cli.EXIT_SELECTOR_INVALID)

    def _run_cli(self, summary, *, dry_run=True):
        db = MagicMock()
        db.get_bind.return_value.dialect.name = "sqlite"
        db.new = set()
        db.dirty = set()
        db.deleted = set()
        context = MagicMock()
        context.__enter__.return_value = db
        context.__exit__.return_value = False
        argv = [
            "expiry_worker", "scan", "--organization-id", "1",
            "--user-id", "11", "--username", "TEST_ONU",
        ]
        if dry_run:
            argv.insert(2, "--dry-run")
        with (
            patch.object(expiry_cli, "SessionLocal", return_value=context),
            patch.object(expiry_cli, "scan_expired_services", return_value=summary),
            patch.object(
                expiry_cli,
                "settings",
                SimpleNamespace(
                    expiry_worker_enabled=not dry_run,
                    expiry_worker_dry_run=dry_run,
                ),
            ),
            patch.object(sys, "argv", argv),
        ):
            code = expiry_cli.main()
        return code, db

    def test_cli_exact_target_cardinality_and_error_exit_codes(self):
        base = dict(
            correlation_id="test", acquired_lock=False, lock_skipped=True,
            dry_run=True, matched=1, evaluated=1, changed=0, disconnected=0,
            newly_expired=1, active_sessions=0, jobs_queued=0, errors=0, duration_ms=1,
        )
        for matched, evaluated in ((0, 0), (2, 2)):
            summary = SimpleNamespace(**(base | {"matched": matched, "evaluated": evaluated}))
            code, db = self._run_cli(summary)
            self.assertEqual(code, expiry_cli.EXIT_TARGET_CARDINALITY)
            db.rollback.assert_called_once_with()
        summary = SimpleNamespace(**(base | {"errors": 1}))
        code, db = self._run_cli(summary)
        self.assertEqual(code, expiry_cli.EXIT_SCAN_ERRORS)
        db.rollback.assert_called_once_with()
        summary = SimpleNamespace(**(base | {"newly_expired": 0}))
        code, db = self._run_cli(summary)
        self.assertEqual(code, expiry_cli.EXIT_SCAN_ERRORS)
        db.rollback.assert_called_once_with()

    def test_cli_exact_dry_run_and_live_idempotent_success(self):
        base = dict(
            correlation_id="test", acquired_lock=False, lock_skipped=True,
            dry_run=True, matched=1, evaluated=1, changed=0, disconnected=0,
            newly_expired=1, active_sessions=0, jobs_queued=0, errors=0, duration_ms=1,
        )
        code, dry_db = self._run_cli(SimpleNamespace(**base))
        self.assertEqual(code, 0)
        dry_db.rollback.assert_called_once_with()
        live = base | {"acquired_lock": True, "lock_skipped": False, "dry_run": False, "changed": 1}
        code, live_db = self._run_cli(SimpleNamespace(**live), dry_run=False)
        self.assertEqual(code, 0)
        live_db.commit.assert_called_once_with()

    def test_cli_database_failure_is_nonzero_and_rolls_back_by_context(self):
        db = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = db
        context.__exit__.return_value = False
        with (
            patch.object(expiry_cli, "SessionLocal", return_value=context),
            patch.object(expiry_cli, "scan_expired_services", side_effect=DBAPIError("scan", {}, Exception())),
            patch.object(sys, "argv", [
                "expiry_worker", "scan", "--dry-run", "--organization-id", "1",
                "--user-id", "11", "--username", "TEST_ONU",
            ]),
        ):
            self.assertEqual(expiry_cli.main(), expiry_cli.EXIT_DATABASE_FAILURE)
        db.rollback.assert_called_once_with()


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

    def test_live_lock_telemetry_reports_acquired_on_success_and_failure(self):
        with patch("app.services.expiry_worker._try_worker_lock", return_value=True):
            acquired = scan_expired_services(
                self.db, now=NOW, organization_id=1, user_id=11,
                username="TEST_ONU", dry_run=False,
            )
        self.assertTrue(acquired.acquired_lock)
        self.assertFalse(acquired.lock_skipped)
        self.assertEqual(acquired.errors, 0)
        self.db.rollback()

        service, _ = self._service_and_plan()
        self.db.add(RadCheck(
            username=service.username, attribute="Auth-Type", op=":=", value="Reject"
        ))
        self.db.commit()
        with patch("app.services.expiry_worker._try_worker_lock", return_value=True):
            failed = scan_expired_services(
                self.db, now=NOW, organization_id=1, user_id=11,
                username="TEST_ONU", dry_run=False,
            )
        self.assertTrue(failed.acquired_lock)
        self.assertFalse(failed.lock_skipped)
        self.assertEqual(failed.errors, 1)


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
        self.assertIn("uq_radius_reject_ownership_user_tenant_reason", source)
        self.assertNotIn("DELETE FROM radcheck", source)

    def test_ci_requires_postgresql_migration_and_zero_skips(self):
        from pathlib import Path

        root = Path(__file__).parents[1]
        workflow = (root / ".github" / "workflows" / "pull-request-validation.yml").read_text()
        migration_runner = (root / "scripts" / "ci_postgresql_validation.py").read_text()
        no_skip_runner = (root / "scripts" / "run_tests_no_skips.py").read_text()
        self.assertIn("image: postgres:16", workflow)
        self.assertIn("RADIUSFIBER_DISPOSABLE_TEST_DATABASE_URL", workflow)
        self.assertIn("python -m scripts.ci_postgresql_validation", workflow)
        self.assertIn("python -m scripts.run_tests_no_skips", workflow)
        self.assertIn("command.downgrade(config, BASE_REVISION)", migration_runner)
        self.assertIn("command.check(config)", migration_runner)
        self.assertIn("if result.skipped", no_skip_runner)


if __name__ == "__main__":
    unittest.main()
