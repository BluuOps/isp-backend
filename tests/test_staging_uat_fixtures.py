from __future__ import annotations

import os
import threading
import unittest
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, HTTPException, Response
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.core.authorization import (
    READ_ONLY_PERMISSIONS,
    UAT_FIXTURE_PERMISSIONS,
    AuthenticatedPrincipal,
    Permission,
    PrincipalType,
    role_permissions,
    create_access_token,
    decode_access_token,
    get_authenticated_principal,
    require_permission,
)
from app.core import authorization
from app.core.config import settings
from app.core import principal as principal_core
from app.core import tenant_host
from app import main as app_main
from app.models import AuditLog, AuthTokenRevocation, Organization, OrganizationStaff, Platform
from app.routers import auth
from app.routers import staging_uat
from app.routers.staging_uat import CreateFixtureRequest
from app.services import staging_uat_fixtures as fixtures


class StagingUatFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        for table in (
            Platform.__table__,
            Organization.__table__,
            OrganizationStaff.__table__,
            AuditLog.__table__,
            AuthTokenRevocation.__table__,
        ):
            table.create(cls.engine, checkfirst=True)
        cls.Session = sessionmaker(bind=cls.engine)

    def setUp(self):
        self.db = self.Session()
        for model in (AuthTokenRevocation, AuditLog, OrganizationStaff, Organization, Platform):
            self.db.query(model).delete()
        self.db.add(Platform(id=1, name="RadiusFiber"))
        self.db.add(
            Organization(
                id=1,
                platform_id=1,
                name="Smart Fiber",
                slug="smart-fiber",
                status="active",
            )
        )
        self.db.add(
            Organization(
                id=2,
                platform_id=1,
                name="Other ISP",
                slug="other-isp",
                status="active",
            )
        )
        self.db.commit()
        self.principal = AuthenticatedPrincipal(
            subject_id="platform-admin",
            principal_type=PrincipalType.PLATFORM_ADMIN,
            authentication_method="bearer",
            active=True,
            platform_authority=True,
            organization_id=None,
            organization_slug=None,
            organization_role=None,
            effective_permissions=frozenset(),
            actor_label="platform-admin@example.test",
        )
        self.enabled_settings = SimpleNamespace(
            deployment_environment="staging",
            staging_uat_fixtures_enabled=True,
            database_url="postgresql://redacted@localhost/isp_db_stage",
        )

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def create_fixture(self, ttl_minutes=60):
        with patch.object(fixtures, "settings", self.enabled_settings):
            result = fixtures.create_read_only_fixture(
                self.db,
                ttl_minutes=ttl_minutes,
                principal=self.principal,
                correlation_id="request-123",
            )
            self.db.commit()
            return result

    def test_production_configuration_rejects_enabled_capability(self):
        configured = replace(
            settings,
            deployment_environment="production",
            staging_uat_fixtures_enabled=True,
        )
        with self.assertRaisesRegex(RuntimeError, "only be enabled"):
            configured.validate_staging_uat_fixture_configuration()

    def test_staging_enabled_with_wrong_database_rejects_startup(self):
        configured = replace(
            settings,
            deployment_environment="staging",
            staging_uat_fixtures_enabled=True,
            database_url="postgresql://redacted@localhost/not_isp_db_stage",
        )
        with self.assertRaisesRegex(RuntimeError, "requires database isp_db_stage"):
            configured.validate_staging_uat_fixture_configuration()

    def test_route_registration_matrix_is_fail_closed(self):
        disabled = replace(
            settings,
            deployment_environment="staging",
            staging_uat_fixtures_enabled=False,
            database_url="postgresql://redacted@localhost/isp_db_stage",
        )
        disabled_app = FastAPI()
        self.assertFalse(app_main.register_staging_uat_routes(disabled_app, disabled))
        self.assertNotIn("/internal/staging/uat-fixtures/read-only", disabled_app.openapi()["paths"])
        self.assertNotIn(
            "/internal/staging/uat-fixtures/read-only", app_main.app.openapi()["paths"]
        )

        enabled = replace(disabled, staging_uat_fixtures_enabled=True)
        enabled_app = FastAPI()
        self.assertTrue(app_main.register_staging_uat_routes(enabled_app, enabled))
        self.assertIn("/internal/staging/uat-fixtures/read-only", enabled_app.openapi()["paths"])

    def test_production_invocation_is_hidden(self):
        unsafe = SimpleNamespace(
            deployment_environment="production",
            staging_uat_fixtures_enabled=True,
            database_url="postgresql://redacted@localhost/isp_db_stage",
        )
        with patch.object(fixtures, "settings", unsafe), self.assertRaises(HTTPException) as raised:
            fixtures.require_staging_fixture_capability()
        self.assertEqual(raised.exception.status_code, 404)

    def test_disabled_flag_is_hidden(self):
        disabled = SimpleNamespace(
            deployment_environment="staging",
            staging_uat_fixtures_enabled=False,
            database_url="postgresql://redacted@localhost/isp_db_stage",
        )
        with patch.object(fixtures, "settings", disabled), self.assertRaises(HTTPException) as raised:
            fixtures.require_staging_fixture_capability()
        self.assertEqual(raised.exception.status_code, 404)

    def test_non_staging_database_is_hidden_even_when_flag_enabled(self):
        production_db = SimpleNamespace(
            deployment_environment="staging",
            staging_uat_fixtures_enabled=True,
            database_url="postgresql://redacted@localhost/isp_db",
        )
        with patch.object(fixtures, "settings", production_db), self.assertRaises(HTTPException) as raised:
            fixtures.require_staging_fixture_capability()
        self.assertEqual(raised.exception.status_code, 404)

    def test_customer_or_organization_principal_cannot_create_fixture(self):
        for principal_type, platform_authority in (
            (PrincipalType.CUSTOMER, False),
            (PrincipalType.ORGANIZATION_STAFF, False),
            (PrincipalType.PLATFORM_ADMIN, False),
        ):
            untrusted = replace(
                self.principal,
                principal_type=principal_type,
                platform_authority=platform_authority,
            )
            with patch.object(fixtures, "settings", self.enabled_settings), self.assertRaises(HTTPException) as raised:
                fixtures.create_read_only_fixture(
                    self.db,
                    ttl_minutes=60,
                    principal=untrusted,
                    correlation_id="request-denied",
                )
            self.assertEqual(raised.exception.status_code, 403)

    def test_request_rejects_arbitrary_role_and_organization(self):
        with self.assertRaises(ValidationError):
            CreateFixtureRequest.model_validate(
                {"ttl_minutes": 60, "role": "Organization Admin", "organization_id": 2}
            )

    def test_database_metadata_requires_read_only_role_and_expiry(self):
        constraint_sql = " ".join(
            str(constraint.sqltext)
            for constraint in OrganizationStaff.__table__.constraints
            if getattr(constraint, "name", None) == "ck_organization_staff_uat_read_only_expiring"
        )
        self.assertIn("role = 'Read Only'", constraint_sql)
        self.assertIn("uat_fixture_id IS NOT NULL", constraint_sql)
        self.assertIn("uat_expires_at IS NOT NULL", constraint_sql)

    def test_creation_is_smart_fiber_read_only_and_securely_hashed(self):
        result = self.create_fixture()
        staff = self.db.query(OrganizationStaff).filter_by(uat_fixture_id=result.fixture_id).one()
        self.assertEqual(staff.organization_id, 1)
        self.assertEqual(staff.role, "Read Only")
        self.assertEqual(staff.email, result.email)
        self.assertNotEqual(staff.password_hash, result.temporary_password)
        self.assertTrue(staff.is_uat_fixture)
        self.assertIsNotNone(staff.uat_expires_at)
        self.assertEqual(self.db.query(OrganizationStaff).filter_by(organization_id=2).count(), 0)

    def test_credential_response_is_non_cacheable(self):
        response = Response()
        with patch.object(fixtures, "settings", self.enabled_settings):
            returned = staging_uat.create_fixture(
                CreateFixtureRequest(),
                response,
                x_request_id="response-safety",
                db=self.db,
                principal=self.principal,
            )
        self.assertEqual(returned.organization_slug, "smart-fiber")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")

    def test_only_one_active_fixture_may_exist(self):
        self.create_fixture()
        with patch.object(fixtures, "settings", self.enabled_settings), self.assertRaises(HTTPException) as raised:
            fixtures.create_read_only_fixture(
                self.db,
                ttl_minutes=60,
                principal=self.principal,
                correlation_id="request-456",
            )
        self.assertEqual(raised.exception.status_code, 409)

    def test_inactive_smart_fiber_organization_is_rejected(self):
        organization = self.db.query(Organization).filter_by(slug="smart-fiber").one()
        organization.status = "inactive"
        self.db.commit()
        with patch.object(fixtures, "settings", self.enabled_settings), self.assertRaises(HTTPException) as raised:
            fixtures.create_read_only_fixture(
                self.db,
                ttl_minutes=60,
                principal=self.principal,
                correlation_id="request-inactive",
            )
        self.assertEqual(raised.exception.status_code, 409)

    def test_ttl_is_mandatory_and_bounded(self):
        self.assertEqual(CreateFixtureRequest().ttl_minutes, 60)
        for value in (4, 121):
            with self.assertRaises(ValidationError):
                CreateFixtureRequest(ttl_minutes=value)
        with patch.object(fixtures, "settings", self.enabled_settings), self.assertRaises(HTTPException):
            fixtures.create_read_only_fixture(
                self.db,
                ttl_minutes=999,
                principal=self.principal,
                correlation_id="request-invalid",
            )

    def test_creation_audit_is_complete_and_contains_no_secret(self):
        result = self.create_fixture()
        audit = self.db.query(AuditLog).filter_by(action="uat.read_only_fixture.created").one()
        self.assertEqual(audit.organization_id, 1)
        self.assertEqual(audit.actor_type, "platform_admin")
        self.assertEqual(audit.new_value["role"], "Read Only")
        self.assertEqual(audit.new_value["organization_slug"], "smart-fiber")
        self.assertEqual(audit.new_value["correlation_id"], "request-123")
        self.assertIn("created_at", audit.new_value)
        self.assertIn("expires_at", audit.new_value)
        self.assertNotIn(result.temporary_password, str(audit.new_value))

    def test_revoke_is_immediate_and_idempotent(self):
        result = self.create_fixture()
        with patch.object(fixtures, "settings", self.enabled_settings):
            first = fixtures.revoke_read_only_fixture(
                self.db,
                fixture_id=result.fixture_id,
                principal=self.principal,
                correlation_id="revoke-1",
            )
            self.db.commit()
            second = fixtures.revoke_read_only_fixture(
                self.db,
                fixture_id=result.fixture_id,
                principal=self.principal,
                correlation_id="revoke-2",
            )
        staff = self.db.query(OrganizationStaff).filter_by(uat_fixture_id=result.fixture_id).one()
        self.assertEqual(first, "revoked")
        self.assertEqual(second, "already_revoked")
        self.assertEqual(staff.status, "inactive")
        self.assertIsNotNone(staff.uat_revoked_at)
        self.assertEqual(self.db.query(AuditLog).filter_by(action="uat.read_only_fixture.revoked").count(), 1)

    def test_expired_fixture_cannot_authenticate(self):
        result = self.create_fixture()
        staff = self.db.query(OrganizationStaff).filter_by(uat_fixture_id=result.fixture_id).one()
        staff.uat_expires_at = fixtures._database_now(self.db) - fixtures.timedelta(seconds=1)
        self.db.commit()
        with self.assertRaises(HTTPException) as raised:
            auth._active_staff(self.db, 1, result.email)
        self.assertEqual(raised.exception.status_code, 401)
        audit = self.db.query(AuditLog).filter_by(
            action="uat.read_only_fixture.authentication_rejected"
        ).one()
        self.assertFalse(audit.success)
        self.assertEqual(audit.new_value["outcome"], "inactive_or_expired")

    def _tenant_request(self) -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/auth/login",
                "headers": [(b"host", b"smart-fiber.radiusfiber.com")],
                "client": ("127.0.0.1", 12345),
                "server": ("smart-fiber.radiusfiber.com", 443),
                "scheme": "https",
                "query_string": b"",
            }
        )

    def _auth_settings(self):
        return replace(
            settings,
            jwt_secret="fixture-test-secret",
            tenant_allowed_domains=("radiusfiber.com",),
            internal_admin_email=None,
            internal_admin_password=None,
            auth_access_token_ttl_seconds=3600,
        )

    def test_login_me_and_principal_use_exact_fixture_permissions_and_expiry(self):
        result = self.create_fixture(ttl_minutes=5)
        staff = self.db.query(OrganizationStaff).filter_by(uat_fixture_id=result.fixture_id).one()
        configured = self._auth_settings()
        database_epoch = int(fixtures._database_now(self.db).timestamp())
        with (
            patch.object(auth, "settings", configured),
            patch.object(authorization, "settings", configured),
            patch.object(principal_core, "settings", configured),
            patch.object(tenant_host, "settings", configured),
            patch.object(auth.time, "time", return_value=database_epoch),
        ):
            login_response = auth.login(
                auth.LoginRequest(email=result.email, password=result.temporary_password),
                self._tenant_request(),
                self.db,
            )
            claims = decode_access_token(login_response.token, configured.jwt_secret)
            principal = get_authenticated_principal(
                authorization=f"Bearer {login_response.token}", db=self.db
            )
            me_response = auth.me(
                authorization=f"Bearer {login_response.token}", db=self.db
            )

        expected_flags = auth._permissions(UAT_FIXTURE_PERMISSIONS)
        self.assertEqual(login_response.user.permissions, expected_flags)
        self.assertEqual(me_response.user.permissions, expected_flags)
        self.assertEqual(principal.effective_permissions, UAT_FIXTURE_PERMISSIONS)
        self.assertLessEqual(claims["exp"], int(staff.uat_expires_at.timestamp()))
        self.assertEqual(
            self.db.query(AuditLog)
            .filter_by(action="uat.read_only_fixture.authentication_succeeded")
            .count(),
            1,
        )

    def test_invalid_password_fixture_authentication_is_audited(self):
        result = self.create_fixture()
        configured = self._auth_settings()
        with (
            patch.object(auth, "settings", configured),
            patch.object(tenant_host, "settings", configured),
            self.assertRaises(HTTPException) as raised,
        ):
            auth.login(
                auth.LoginRequest(email=result.email, password="incorrect-password"),
                self._tenant_request(),
                self.db,
            )
        self.assertEqual(raised.exception.status_code, 401)
        audit = self.db.query(AuditLog).filter_by(
            action="uat.read_only_fixture.authentication_rejected"
        ).one()
        self.assertFalse(audit.success)
        self.assertEqual(audit.new_value["outcome"], "invalid_password")
        self.assertNotIn("incorrect-password", str(audit.new_value))

    def test_inactive_fixture_authentication_is_audited(self):
        result = self.create_fixture()
        staff = self.db.query(OrganizationStaff).filter_by(uat_fixture_id=result.fixture_id).one()
        staff.status = "inactive"
        self.db.commit()
        with self.assertRaises(HTTPException):
            auth._active_staff(self.db, 1, result.email)
        audit = self.db.query(AuditLog).filter_by(
            action="uat.read_only_fixture.authentication_rejected"
        ).one()
        self.assertEqual(audit.new_value["outcome"], "inactive_or_expired")

    def _fixture_token(self, staff: OrganizationStaff, *, expires_in: int = 3600) -> str:
        now = int(time.time())
        return create_access_token(
            {
                "sub": f"staff:{staff.id}",
                "principal_type": "organization_staff",
                "token_type": "access",
                "staff_id": staff.id,
                "organization_id": staff.organization_id,
                "organization_slug": "smart-fiber",
                "tenant_id": "smart-fiber",
                "auth_method": "password",
                "iat": now,
                "exp": now + expires_in,
                "iss": settings.auth_token_issuer,
                "aud": settings.auth_token_audience,
                "jti": f"fixture-token-{staff.id}-{now}",
            },
            "fixture-test-secret",
        )

    def test_revocation_invalidates_an_already_issued_token(self):
        result = self.create_fixture()
        staff = self.db.query(OrganizationStaff).filter_by(uat_fixture_id=result.fixture_id).one()
        token = self._fixture_token(staff)
        auth_settings = replace(settings, jwt_secret="fixture-test-secret")
        with patch.object(authorization, "settings", auth_settings):
            self.assertEqual(
                get_authenticated_principal(authorization=f"Bearer {token}", db=self.db).subject_id,
                f"staff:{staff.id}",
            )
            with patch.object(fixtures, "settings", self.enabled_settings):
                fixtures.revoke_read_only_fixture(
                    self.db,
                    fixture_id=result.fixture_id,
                    principal=self.principal,
                    correlation_id="revoke-active-token",
                )
                self.db.commit()
            with self.assertRaises(HTTPException) as raised:
                get_authenticated_principal(authorization=f"Bearer {token}", db=self.db)
        self.assertEqual(raised.exception.status_code, 401)

    def test_server_side_fixture_expiry_invalidates_unexpired_jwt(self):
        result = self.create_fixture()
        staff = self.db.query(OrganizationStaff).filter_by(uat_fixture_id=result.fixture_id).one()
        token = self._fixture_token(staff)
        staff.uat_expires_at = fixtures._database_now(self.db) - fixtures.timedelta(seconds=1)
        self.db.commit()
        auth_settings = replace(settings, jwt_secret="fixture-test-secret")
        with patch.object(authorization, "settings", auth_settings), self.assertRaises(HTTPException) as raised:
            get_authenticated_principal(authorization=f"Bearer {token}", db=self.db)
        self.assertEqual(raised.exception.status_code, 401)

    def test_cleanup_is_idempotent_and_retains_sanitized_audit(self):
        result = self.create_fixture()
        with patch.object(fixtures, "settings", self.enabled_settings):
            first = fixtures.cleanup_read_only_fixture(
                self.db,
                fixture_id=result.fixture_id,
                principal=self.principal,
                correlation_id="cleanup-1",
            )
            self.db.commit()
            second = fixtures.cleanup_read_only_fixture(
                self.db,
                fixture_id=result.fixture_id,
                principal=self.principal,
                correlation_id="cleanup-2",
            )
        self.assertEqual(first, "removed")
        self.assertEqual(second, "already_absent")
        self.assertEqual(self.db.query(OrganizationStaff).filter_by(uat_fixture_id=result.fixture_id).count(), 0)
        self.assertEqual(self.db.query(AuditLog).filter_by(action="uat.read_only_fixture.cleaned").count(), 1)

    def test_read_only_permission_matrix_has_no_mutations(self):
        expected_reads = {
            Permission.OLT_INVENTORY_READ,
            Permission.OLT_TELEMETRY_READ,
            Permission.OLT_ALARMS_READ,
        }
        forbidden = {
            Permission.OLT_DEVICES_MANAGE,
            Permission.OLT_CONNECTIONS_TEST,
            Permission.OLT_POLL_REQUEST,
            Permission.OLT_ASSOCIATIONS_MANAGE,
            Permission.CUSTOMERS_CREATE,
            Permission.CUSTOMERS_UPDATE,
            Permission.CUSTOMERS_DELETE,
            Permission.SUBSCRIBERS_CREATE,
            Permission.SUBSCRIBERS_UPDATE,
            Permission.SUBSCRIBERS_DELETE,
            Permission.SUBSCRIBERS_SUSPEND,
            Permission.SUBSCRIBERS_RECONNECT,
            Permission.SUBSCRIBERS_RECHARGE,
            Permission.RADIUS_SESSIONS_DISCONNECT,
            Permission.BILLING_ACCOUNTS_CREATE,
            Permission.BILLING_ACCOUNTS_UPDATE,
            Permission.BILLING_ACCOUNTS_DELETE,
            Permission.PAYMENTS_CREATE,
            Permission.PAYMENTS_UPDATE,
            Permission.PAYMENTS_VERIFY,
            Permission.PAYMENTS_PLAN_ACTIVATE,
            Permission.NETWORK_NAS_CREATE,
            Permission.NETWORK_NAS_UPDATE,
            Permission.NETWORK_NAS_DELETE,
            Permission.NETWORK_ZONES_CREATE,
            Permission.NETWORK_ZONES_UPDATE,
            Permission.NETWORK_ZONES_DELETE,
        }
        self.assertEqual(role_permissions("Read Only"), READ_ONLY_PERMISSIONS)
        self.assertTrue(expected_reads <= READ_ONLY_PERMISSIONS)
        self.assertTrue(forbidden.isdisjoint(READ_ONLY_PERMISSIONS))

    def test_read_only_principal_can_read_olt_but_cannot_write(self):
        principal = AuthenticatedPrincipal(
            subject_id="staff:42",
            principal_type=PrincipalType.ORGANIZATION_STAFF,
            authentication_method="password",
            active=True,
            platform_authority=False,
            organization_id=1,
            organization_slug="smart-fiber",
            organization_role="Read Only",
            effective_permissions=READ_ONLY_PERMISSIONS,
            actor_label="uat-read-only@example.test",
        )
        self.assertIs(require_permission(Permission.OLT_INVENTORY_READ)(principal), principal)
        for permission in (
            Permission.OLT_DEVICES_MANAGE,
            Permission.OLT_CONNECTIONS_TEST,
            Permission.OLT_POLL_REQUEST,
            Permission.BILLING_ACCOUNTS_CREATE,
            Permission.SUBSCRIBERS_UPDATE,
            Permission.RADIUS_SESSIONS_DISCONNECT,
        ):
            with self.assertRaises(HTTPException) as raised:
                require_permission(permission)(principal)
            self.assertEqual(raised.exception.status_code, 403)


POSTGRES_URL = os.getenv("RADIUSFIBER_DISPOSABLE_TEST_DATABASE_URL")


@unittest.skipUnless(POSTGRES_URL, "disposable PostgreSQL URL is not configured")
class StagingUatFixturePostgresTests(unittest.TestCase):
    LOCK_KEY = 160016

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(POSTGRES_URL, future=True)
        cls.Session = sessionmaker(bind=cls.engine, future=True)
        cls.lock_connection = cls.engine.connect()
        cls.lock_connection.execute(
            text("SELECT pg_advisory_lock(:lock_key)"), {"lock_key": cls.LOCK_KEY}
        )
        cls.created_platform_id = None
        cls.created_organization_id = None
        cls.all_correlation_ids: set[str] = set()
        cls.all_fixture_ids: set[str] = set()
        cls.all_staff_ids: set[int] = set()
        try:
            with cls.Session.begin() as db:
                platform = db.query(Platform).order_by(Platform.id).first()
                if platform is None:
                    platform_id = db.execute(
                        text("SELECT COALESCE(MAX(id), 0) + 1 FROM platform")
                    ).scalar_one()
                    platform = Platform(
                        id=platform_id,
                        name="UAT fixture concurrency validation",
                    )
                    db.add(platform)
                    db.flush()
                    cls.created_platform_id = platform.id

                organization = (
                    db.query(Organization)
                    .filter_by(slug="smart-fiber")
                    .order_by(Organization.id)
                    .one_or_none()
                )
                if organization is not None and organization.status != "active":
                    raise RuntimeError(
                        "Existing smart-fiber organization is unsuitable for UAT fixture tests"
                    )
                if organization is None:
                    organization_id = db.execute(
                        text("SELECT COALESCE(MAX(id), 0) + 1 FROM organizations")
                    ).scalar_one()
                    organization = Organization(
                        id=organization_id,
                        platform_id=platform.id,
                        name="Smart Fiber",
                        slug="smart-fiber",
                        status="active",
                    )
                    db.add(organization)
                    db.flush()
                    cls.created_organization_id = organization.id
                cls.organization_id = organization.id

                existing_fixture_count = db.query(OrganizationStaff).filter_by(
                    organization_id=organization.id,
                    is_uat_fixture=True,
                ).count()
                if existing_fixture_count:
                    raise RuntimeError(
                        "Pre-existing Smart Fiber UAT fixture prevents isolated PostgreSQL tests"
                    )
        except Exception:
            cls._release_lock_and_engine()
            raise
        cls.principal = AuthenticatedPrincipal(
            subject_id="platform-admin",
            principal_type=PrincipalType.PLATFORM_ADMIN,
            authentication_method="bearer",
            active=True,
            platform_authority=True,
            organization_id=None,
            organization_slug=None,
            organization_role=None,
            effective_permissions=frozenset(),
            actor_label="platform-admin@example.test",
        )
        cls.enabled_settings = SimpleNamespace(
            deployment_environment="staging",
            staging_uat_fixtures_enabled=True,
            database_url="postgresql://redacted@localhost/isp_db_stage",
        )

    @classmethod
    def tearDownClass(cls):
        try:
            with cls.Session.begin() as db:
                if cls.all_correlation_ids:
                    audit_ids = list(
                        db.execute(
                            text(
                                "SELECT id FROM audit_logs "
                                "WHERE organization_id = :organization_id "
                                "AND new_value ->> 'correlation_id' = ANY(:correlation_ids)"
                            ),
                            {
                                "organization_id": cls.organization_id,
                                "correlation_ids": list(cls.all_correlation_ids),
                            },
                        ).scalars()
                    )
                    if audit_ids:
                        db.query(AuditLog).filter(AuditLog.id.in_(audit_ids)).delete(
                            synchronize_session=False
                        )
                if cls.all_fixture_ids:
                    db.query(OrganizationStaff).filter(
                        OrganizationStaff.organization_id == cls.organization_id,
                        OrganizationStaff.uat_fixture_id.in_(cls.all_fixture_ids),
                    ).delete(synchronize_session=False)
                if cls.all_staff_ids:
                    db.query(OrganizationStaff).filter(
                        OrganizationStaff.organization_id == cls.organization_id,
                        OrganizationStaff.id.in_(cls.all_staff_ids),
                    ).delete(synchronize_session=False)
                if cls.created_organization_id is not None:
                    db.query(Organization).filter_by(
                        id=cls.created_organization_id,
                        slug="smart-fiber",
                    ).delete(synchronize_session=False)
                if cls.created_platform_id is not None:
                    db.query(Platform).filter_by(id=cls.created_platform_id).delete(
                        synchronize_session=False
                    )
        finally:
            cls._release_lock_and_engine()

    @classmethod
    def _release_lock_and_engine(cls):
        connection = getattr(cls, "lock_connection", None)
        if connection is not None:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": cls.LOCK_KEY},
                )
            finally:
                connection.close()
                cls.lock_connection = None
        cls.engine.dispose()

    def setUp(self):
        self.correlation_ids: set[str] = set()
        self.fixture_ids: set[str] = set()
        self.staff_ids: set[int] = set()
        with self.Session() as db:
            self.protected_before = self._protected_counts(db)
            self.ordinary_staff_before = self._ordinary_staff_snapshot(db)

    def tearDown(self):
        config = Config("alembic.ini")
        with self.engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        if revision != "0016_staging_uat_fixtures":
            command.upgrade(config, "0016_staging_uat_fixtures")

        with self.Session.begin() as db:
            audit_ids = []
            if self.correlation_ids:
                audit_ids = list(
                    db.execute(
                        text(
                            "SELECT id FROM audit_logs "
                            "WHERE organization_id = :organization_id "
                            "AND new_value ->> 'correlation_id' = ANY(:correlation_ids)"
                        ),
                        {
                            "organization_id": self.organization_id,
                            "correlation_ids": list(self.correlation_ids),
                        },
                    ).scalars()
                )
            if audit_ids:
                db.query(AuditLog).filter(AuditLog.id.in_(audit_ids)).delete(
                    synchronize_session=False
                )
            if self.fixture_ids:
                db.query(OrganizationStaff).filter(
                    OrganizationStaff.organization_id == self.organization_id,
                    OrganizationStaff.uat_fixture_id.in_(self.fixture_ids),
                ).delete(synchronize_session=False)
            if self.staff_ids:
                db.query(OrganizationStaff).filter(
                    OrganizationStaff.organization_id == self.organization_id,
                    OrganizationStaff.id.in_(self.staff_ids),
                ).delete(synchronize_session=False)

        with self.Session() as db:
            self.assertEqual(self._protected_counts(db), self.protected_before)
            self.assertEqual(self._ordinary_staff_snapshot(db), self.ordinary_staff_before)

    @staticmethod
    def _protected_counts(db):
        return tuple(
            db.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one()
            for table_name in ("customers", "billing_accounts", "radcheck")
        )

    def _ordinary_staff_snapshot(self, db):
        return tuple(
            db.execute(
                text(
                    "SELECT id, organization_id, email, role, status, password_hash "
                    "FROM organization_staff WHERE organization_id = :organization_id "
                    "AND NOT is_uat_fixture ORDER BY id"
                ),
                {"organization_id": self.organization_id},
            ).all()
        )

    def _correlation_id(self, purpose: str) -> str:
        correlation_id = f"pr16-{purpose}-{uuid.uuid4().hex}"
        self.correlation_ids.add(correlation_id)
        self.all_correlation_ids.add(correlation_id)
        return correlation_id

    def test_concurrent_creation_serializes_on_target_organization(self):
        barrier = threading.Barrier(2)
        outcomes: list[object] = []
        outcome_lock = threading.Lock()
        correlations = [
            self._correlation_id("concurrent-a"),
            self._correlation_id("concurrent-b"),
        ]

        def create(correlation_id: str) -> None:
            with self.Session() as db:
                barrier.wait()
                try:
                    created = fixtures.create_read_only_fixture(
                        db,
                        ttl_minutes=60,
                        principal=self.principal,
                        correlation_id=correlation_id,
                    )
                    created_staff_id = db.query(OrganizationStaff.id).filter_by(
                        uat_fixture_id=created.fixture_id
                    ).scalar()
                    db.commit()
                    outcome: object = (created.fixture_id, created_staff_id)
                except HTTPException as exc:
                    db.rollback()
                    outcome = exc.status_code
                with outcome_lock:
                    outcomes.append(outcome)
                    if isinstance(outcome, tuple):
                        fixture_id, staff_id = outcome
                        self.fixture_ids.add(fixture_id)
                        self.all_fixture_ids.add(fixture_id)
                        self.staff_ids.add(staff_id)
                        self.all_staff_ids.add(staff_id)

        threads = [
            threading.Thread(target=create, args=(correlation_id,))
            for correlation_id in correlations
        ]
        with patch.object(fixtures, "settings", self.enabled_settings):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len([item for item in outcomes if isinstance(item, tuple)]), 1)
        self.assertEqual(outcomes.count(409), 1)
        with self.Session() as db:
            count = db.query(OrganizationStaff).filter(
                OrganizationStaff.organization_id == self.organization_id,
                OrganizationStaff.is_uat_fixture.is_(True),
                OrganizationStaff.status == "active",
            ).count()
        self.assertEqual(count, 1)

    def test_downgrade_refuses_fixture_and_preserves_ordinary_staff(self):
        with self.Session() as db:
            ordinary = OrganizationStaff(
                organization_id=self.organization_id,
                name="Ordinary Read Only",
                email=f"ordinary-{time.time_ns()}@example.test",
                password_hash="not-a-real-credential",
                role="Read Only",
                status="active",
                is_uat_fixture=False,
            )
            db.add(ordinary)
            db.flush()
            self.staff_ids.add(ordinary.id)
            self.all_staff_ids.add(ordinary.id)
            create_correlation_id = self._correlation_id("downgrade-create")
            with patch.object(fixtures, "settings", self.enabled_settings):
                created = fixtures.create_read_only_fixture(
                    db,
                    ttl_minutes=60,
                    principal=self.principal,
                    correlation_id=create_correlation_id,
                )
            db.commit()
            ordinary_id = ordinary.id
            self.fixture_ids.add(created.fixture_id)
            self.all_fixture_ids.add(created.fixture_id)
            fixture_staff_id = db.query(OrganizationStaff.id).filter_by(
                uat_fixture_id=created.fixture_id
            ).scalar()
            self.staff_ids.add(fixture_staff_id)
            self.all_staff_ids.add(fixture_staff_id)

        config = Config("alembic.ini")
        with self.assertRaisesRegex(RuntimeError, "UAT fixture rows remain"):
            command.downgrade(config, "0015_expiry_reject_ownership")
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(text("SELECT version_num FROM alembic_version")),
                "0016_staging_uat_fixtures",
            )
            fixture_metadata = connection.execute(
                text(
                    "SELECT is_uat_fixture, uat_fixture_id, uat_expires_at "
                    "FROM organization_staff WHERE uat_fixture_id = :fixture_id"
                ),
                {"fixture_id": created.fixture_id},
            ).one()
            self.assertTrue(fixture_metadata.is_uat_fixture)
            self.assertEqual(fixture_metadata.uat_fixture_id, created.fixture_id)
            self.assertIsNotNone(fixture_metadata.uat_expires_at)
            self.assertEqual(
                connection.scalar(
                    text("SELECT status FROM organization_staff WHERE id = :id"),
                    {"id": ordinary_id},
                ),
                "active",
            )

        with self.Session() as db:
            cleanup_correlation_id = self._correlation_id("downgrade-cleanup")
            with patch.object(fixtures, "settings", self.enabled_settings):
                fixtures.cleanup_read_only_fixture(
                    db,
                    fixture_id=created.fixture_id,
                    principal=self.principal,
                    correlation_id=cleanup_correlation_id,
                )
            db.commit()
        self.fixture_ids.discard(created.fixture_id)
        command.downgrade(config, "0015_expiry_reject_ownership")
        with self.engine.connect() as connection:
            self.assertNotIn(
                "is_uat_fixture",
                {column["name"] for column in inspect(connection).get_columns("organization_staff")},
            )
            self.assertEqual(
                connection.scalar(
                    text("SELECT status FROM organization_staff WHERE id = :id"),
                    {"id": ordinary_id},
                ),
                "active",
            )
            self.assertGreaterEqual(
                connection.scalar(
                    text(
                        "SELECT count(*) FROM audit_logs "
                        "WHERE organization_id = :organization_id "
                        "AND action = 'uat.read_only_fixture.cleaned'"
                    ),
                    {"organization_id": self.organization_id},
                ),
                1,
            )
        command.upgrade(config, "0016_staging_uat_fixtures")
        self.assertEqual(
            ScriptDirectory.from_config(config).get_heads(),
            ["0016_staging_uat_fixtures"],
        )
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(text("SELECT version_num FROM alembic_version")),
                "0016_staging_uat_fixtures",
            )


if __name__ == "__main__":
    unittest.main()
