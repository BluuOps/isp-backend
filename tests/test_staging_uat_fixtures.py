from __future__ import annotations

import unittest
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.authorization import (
    READ_ONLY_PERMISSIONS,
    AuthenticatedPrincipal,
    Permission,
    PrincipalType,
    role_permissions,
    create_access_token,
    get_authenticated_principal,
    require_permission,
)
from app.core import authorization
from app.core.config import settings
from app.models import AuditLog, AuthTokenRevocation, Organization, OrganizationStaff, Platform
from app.routers import auth
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


if __name__ == "__main__":
    unittest.main()
