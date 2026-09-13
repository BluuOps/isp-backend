from __future__ import annotations

import json
import hashlib
import logging
import os
import threading
import time
import unittest
import uuid
from datetime import timedelta
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException, Response
from pydantic import ValidationError
from sqlalchemy import create_engine, func, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.authorization import (
    AuthenticatedPrincipal,
    PrincipalType,
    create_access_token,
    get_authenticated_principal,
)
from app.core.config import settings
from app.core.platform_auth import require_recent_platform_admin
from app.core.principal import create_principal_token
from app.models import (
    AuditLog,
    AuthTokenRevocation,
    Organization,
    OrganizationAdminInvitation,
    OrganizationAdminInvitationRateLimit,
    OrganizationStaff,
    Platform,
)
from app.routers.platform import AdminInvitationCreate, create_organization_admin_invitation
from app.routers import auth
from app.services import admin_invitations
from app.services.admin_invitations import (
    GENERIC_INVALID_INVITATION,
    accept_admin_invitation,
    create_admin_invitation,
    database_now,
    revoke_admin_invitation,
)
from app.services.security import hash_password, verify_password


class OrganizationAdminInvitationTests(unittest.TestCase):
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
            OrganizationAdminInvitation.__table__,
            OrganizationAdminInvitationRateLimit.__table__,
        ):
            table.create(cls.engine, checkfirst=True)
        cls.Session = sessionmaker(bind=cls.engine)

    def setUp(self):
        self.db = self.Session()
        for model in (
            OrganizationAdminInvitationRateLimit,
            OrganizationAdminInvitation,
            AuthTokenRevocation,
            AuditLog,
            OrganizationStaff,
            Organization,
            Platform,
        ):
            self.db.query(model).delete()
        self.db.add(Platform(id=1, name="RadiusFiber"))
        self.db.add_all(
            [
                Organization(id=1, platform_id=1, name="One", slug="one", status="active"),
                Organization(id=2, platform_id=1, name="Two", slug="two", status="active"),
            ]
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
            effective_permissions=frozenset({"platform.organization_admins.manage"}),
            correlation_id="platform-request",
            actor_label="platform@example.test",
        )

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def create(self, purpose="bootstrap", email="admin@one.test"):
        result = create_admin_invitation(
            self.db,
            organization_id=1,
            email=email,
            purpose=purpose,
            reason="Authorized administrator lifecycle operation",
            principal=self.principal,
        )
        self.db.commit()
        return result

    def accept(self, token, password="Strong Unique Passphrase 47!"):
        result = accept_admin_invitation(
            self.db,
            token=token,
            new_password=password,
            rate_identity=f"test-{time.time_ns()}",
        )
        self.db.commit()
        return result

    def add_admin(self, organization_id=1, email="existing@one.test"):
        staff = OrganizationStaff(
            organization_id=organization_id,
            name="Existing Admin",
            email=email,
            password_hash=hash_password("Old Strong Passphrase 12!"),
            role="Organization Admin",
            status="active",
            is_temporary_password=False,
        )
        self.db.add(staff)
        self.db.commit()
        return staff

    def test_migration_has_expected_predecessor_and_single_head(self):
        script = ScriptDirectory.from_config(Config("alembic.ini"))
        self.assertEqual(script.get_current_head(), "0017_organization_admin_invitations")
        revision = script.get_revision("0017_organization_admin_invitations")
        self.assertEqual(revision.down_revision, "0016_staging_uat_fixtures")

    def test_request_contract_forbids_privilege_and_expiry_fields(self):
        for key, value in (
            ("role", "Organization Admin"),
            ("permissions", ["*"]),
            ("password", "secret"),
            ("organization_id", 2),
            ("expires_at", "2099-01-01T00:00:00Z"),
        ):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                AdminInvitationCreate(
                    email="admin@one.test",
                    purpose="bootstrap",
                    reason="Authorized bootstrap request",
                    **{key: value},
                )

    def test_high_risk_authorization_requires_recent_typed_bearer_and_permission(self):
        valid = create_principal_token(
            {
                "sub": "platform-admin",
                "principal_type": "platform_admin",
                "email": "platform@example.test",
                "permissions": ["platform.organization_admins.manage"],
            }
        )
        principal = require_recent_platform_admin(f"Bearer {valid}", self.db)
        self.assertTrue(principal.platform_authority)
        for payload in (
            {
                "sub": "staff:1",
                "principal_type": "organization_staff",
                "permissions": ["platform.organization_admins.manage"],
            },
            {
                "sub": "platform-admin",
                "principal_type": "platform_admin",
                "permissions": [],
            },
        ):
            token = create_principal_token(payload)
            with self.assertRaises(HTTPException):
                require_recent_platform_admin(f"Bearer {token}", self.db)
        with self.assertRaises(HTTPException):
            require_recent_platform_admin(None, self.db)
        stale = create_principal_token(
            {
                "sub": "platform-admin",
                "principal_type": "platform_admin",
                "permissions": ["platform.organization_admins.manage"],
                "iat": int(time.time()) - settings.admin_invitation_recent_auth_seconds - 1,
                "exp": int(time.time()) + 3600,
            }
        )
        with self.assertRaises(HTTPException) as raised:
            require_recent_platform_admin(f"Bearer {stale}", self.db)
        self.assertEqual(raised.exception.status_code, 401)

    def test_revoked_platform_session_is_denied(self):
        token = create_principal_token(
            {
                "sub": "platform-admin",
                "principal_type": "platform_admin",
                "email": "platform@example.test",
                "permissions": ["platform.organization_admins.manage"],
            }
        )
        from app.core.principal import decode_principal_token
        claims = decode_principal_token(token)
        self.db.add(
            AuthTokenRevocation(
                id=1,
                jti_hash=hashlib.sha256(claims["jti"].encode()).hexdigest(),
                principal_type="platform_admin",
                subject_id="platform-admin",
                expires_at=database_now(self.db) + timedelta(hours=1),
                reason="security",
            )
        )
        self.db.commit()
        with self.assertRaises(HTTPException):
            require_recent_platform_admin(f"Bearer {token}", self.db)

    def test_bootstrap_is_single_use_and_stores_only_digest(self):
        created = self.create()
        self.assertGreaterEqual(len(created.token), 43)
        self.assertNotEqual(created.invitation.token_hash, created.token)
        self.assertNotIn(created.token, json.dumps(created.invitation.__dict__, default=str))

        invitation, staff = self.accept(created.token)
        self.assertIsNotNone(invitation.used_at)
        self.assertEqual(staff.role, "Organization Admin")
        self.assertTrue(verify_password("Strong Unique Passphrase 47!", staff.password_hash))
        with self.assertRaises(HTTPException) as raised:
            self.accept(created.token)
        self.assertEqual(raised.exception.detail, GENERIC_INVALID_INVITATION)

    def test_bootstrap_rejects_existing_admin_and_duplicate_active_invitation(self):
        self.add_admin()
        with self.assertRaises(HTTPException):
            self.create()
        self.db.query(OrganizationStaff).delete()
        self.db.commit()
        self.create()
        with self.assertRaises(HTTPException):
            self.create(email="other@one.test")

    def test_invite_requires_active_org_and_new_email(self):
        self.add_admin()
        created = self.create("invite", "new@one.test")
        _, staff = self.accept(created.token)
        self.assertEqual(staff.email, "new@one.test")
        self.assertEqual(self.db.query(OrganizationStaff).filter_by(organization_id=2).count(), 0)
        with self.assertRaises(HTTPException):
            self.create("invite", "existing@one.test")

    def test_invite_acceptance_fails_closed_if_organization_leaves_active_state(self):
        self.add_admin()
        created = self.create("invite", "new@one.test")
        organization = self.db.get(Organization, 1)
        organization.status = "trial"
        self.db.commit()
        with self.assertRaises(HTTPException) as raised:
            self.accept(created.token)
        self.assertEqual(raised.exception.detail, GENERIC_INVALID_INVITATION)
        self.assertEqual(
            self.db.query(OrganizationStaff).filter_by(email="new@one.test").count(),
            0,
        )

    def test_recovery_changes_password_without_creating_staff(self):
        staff = self.add_admin()
        old_hash = staff.password_hash
        created = self.create("recovery", staff.email)
        _, recovered = self.accept(created.token)
        self.assertEqual(self.db.query(OrganizationStaff).count(), 1)
        self.assertNotEqual(recovered.password_hash, old_hash)
        self.assertEqual(recovered.credential_version, 2)
        self.assertIsNotNone(recovered.credentials_revoked_at)

    def test_recovery_disables_legacy_internal_password_bridge(self):
        staff = self.add_admin(email="bridge@one.test")
        configured = settings.__class__(
            **{
                **settings.__dict__,
                "internal_admin_email": staff.email,
                "internal_admin_password": "Legacy Bridge Password 55!",
            }
        )
        with patch.object(auth, "settings", configured):
            self.assertEqual(
                auth._staff_password_valid(staff, "Legacy Bridge Password 55!"),
                (True, "internal_bridge"),
            )
            created = self.create("recovery", staff.email)
            self.accept(created.token)
            self.assertEqual(
                auth._staff_password_valid(staff, "Legacy Bridge Password 55!"),
                (False, "password"),
            )

    def test_recovery_immediately_invalidates_preexisting_token(self):
        staff = self.add_admin()
        now = int(time.time())
        token = create_access_token(
            {
                "sub": f"staff:{staff.id}",
                "principal_type": "organization_staff",
                "token_type": "access",
                "staff_id": staff.id,
                "organization_id": 1,
                "organization_slug": "one",
                "iat": now,
                "exp": now + 3600,
                "iss": settings.auth_token_issuer,
                "aud": settings.auth_token_audience,
                "jti": "old-session-token",
                "credential_version": 1,
            },
            settings.jwt_secret,
        )
        self.assertEqual(get_authenticated_principal(f"Bearer {token}", self.db).subject_id, f"staff:{staff.id}")
        created = self.create("recovery", staff.email)
        self.accept(created.token)
        with self.assertRaises(HTTPException) as raised:
            get_authenticated_principal(f"Bearer {token}", self.db)
        self.assertEqual(raised.exception.detail, "Credentials have been revoked")

    def test_recovery_rejects_non_admin_and_wrong_tenant(self):
        self.db.add(
            OrganizationStaff(
                organization_id=1,
                name="Support",
                email="support@one.test",
                password_hash=hash_password("Some Strong Passphrase 33!"),
                role="Support",
                status="active",
            )
        )
        self.db.commit()
        with self.assertRaises(HTTPException):
            self.create("recovery", "support@one.test")
        with self.assertRaises(HTTPException):
            create_admin_invitation(
                self.db,
                organization_id=2,
                email="support@one.test",
                purpose="recovery",
                reason="Authorized recovery request",
                principal=self.principal,
            )

    def test_expired_revoked_and_unknown_tokens_share_generic_error(self):
        details = []
        created = self.create()
        created.invitation.expires_at = database_now(self.db) - timedelta(seconds=1)
        self.db.commit()
        candidates = [created.token, "x" * 43]
        for token in candidates:
            with self.assertRaises(HTTPException) as raised:
                self.accept(token)
            details.append((raised.exception.status_code, raised.exception.detail))
        self.assertEqual(details[0], details[1])

    def test_revocation_is_scoped_and_idempotent(self):
        created = self.create()
        changed = revoke_admin_invitation(self.db, invitation=created.invitation, principal=self.principal)
        self.db.commit()
        self.assertTrue(changed)
        self.assertFalse(revoke_admin_invitation(self.db, invitation=created.invitation, principal=self.principal))
        with self.assertRaises(HTTPException) as raised:
            self.accept(created.token)
        self.assertEqual(raised.exception.detail, GENERIC_INVALID_INVITATION)

    def test_attempt_limit_revokes_known_invitation(self):
        created = self.create()
        created.invitation.max_attempts = 1
        created.invitation.expires_at = database_now(self.db) - timedelta(seconds=1)
        self.db.commit()
        with self.assertRaises(HTTPException):
            self.accept(created.token)
        self.db.refresh(created.invitation)
        self.assertEqual(created.invitation.attempt_count, 1)
        self.assertIsNotNone(created.invitation.revoked_at)

    def test_creation_response_is_non_cacheable(self):
        response = Response()
        output = create_organization_admin_invitation(
            1,
            AdminInvitationCreate(
                email="admin@one.test",
                purpose="bootstrap",
                reason="Authorized bootstrap request",
            ),
            response,
            self.db,
            self.principal,
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertEqual(output.purpose, "bootstrap")

    def test_audits_do_not_contain_credentials(self):
        created = self.create()
        password = "Strong Unique Passphrase 47!"
        self.accept(created.token, password)
        serialized = json.dumps(
            [(item.old_value, item.new_value) for item in self.db.query(AuditLog).all()],
            default=str,
        )
        self.assertNotIn(created.token, serialized)
        self.assertNotIn(created.invitation.token_hash, serialized)
        self.assertNotIn(password, serialized)

    def test_audit_failure_rolls_back_account_and_token_consumption(self):
        created = self.create()
        with patch.object(admin_invitations, "record_audit", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                accept_admin_invitation(
                    self.db,
                    token=created.token,
                    new_password="Strong Unique Passphrase 47!",
                    rate_identity="audit-failure",
                )
            self.db.rollback()
        invitation = self.db.get(OrganizationAdminInvitation, created.invitation.id)
        self.assertIsNone(invitation.used_at)
        self.assertEqual(self.db.query(OrganizationStaff).count(), 0)

    def test_password_policy_rejects_trivial_values_without_consuming_token(self):
        created = self.create()
        with self.assertRaises(HTTPException) as raised:
            self.accept(created.token, "aaaaaaaaaaaa")
        self.assertEqual(raised.exception.status_code, 422)
        self.db.refresh(created.invitation)
        self.assertIsNone(created.invitation.used_at)

    def test_acceptance_rate_limit_is_database_backed(self):
        outcomes = []
        for _ in range(21):
            try:
                accept_admin_invitation(
                    self.db,
                    token="z" * 43,
                    new_password="Strong Unique Passphrase 47!",
                    rate_identity="one-client",
                )
            except HTTPException as exc:
                outcomes.append(exc.status_code)
        self.assertEqual(outcomes[:20], [400] * 20)
        self.assertEqual(outcomes[20], 429)
        self.assertEqual(
            self.db.query(OrganizationAdminInvitationRateLimit)
            .filter_by(scope="acceptance")
            .one()
            .attempt_count,
            20,
        )


POSTGRES_URL = os.getenv("RADIUSFIBER_DISPOSABLE_TEST_DATABASE_URL")


@unittest.skipUnless(POSTGRES_URL, "disposable PostgreSQL URL is not configured")
class OrganizationAdminInvitationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(POSTGRES_URL, future=True)
        cls.Session = sessionmaker(bind=cls.engine, future=True)
        with cls.Session.begin() as db:
            platform = db.query(Platform).order_by(Platform.id).first()
            if platform is None:
                platform = Platform(name="Disposable invitation test")
                db.add(platform)
                db.flush()
            suffix = uuid.uuid4().hex[:12]
            cls.suffix = suffix
            organization_id = db.query(func.coalesce(func.max(Organization.id), 0) + 1).scalar()
            organization = Organization(
                id=organization_id,
                platform_id=platform.id,
                name="Disposable Invitation Organization",
                slug=f"admin-invitation-{suffix}",
                status="active",
            )
            db.add(organization)
            db.flush()
            cls.organization_id = organization.id
        cls.principal = AuthenticatedPrincipal(
            subject_id=f"postgres-test-platform-{cls.suffix}",
            principal_type=PrincipalType.PLATFORM_ADMIN,
            authentication_method="bearer",
            active=True,
            platform_authority=True,
            organization_id=None,
            organization_slug=None,
            organization_role=None,
            effective_permissions=frozenset({"platform.organization_admins.manage"}),
            correlation_id="postgres-concurrency",
            actor_label="platform@postgres.test",
        )

    def setUp(self):
        with self.Session.begin() as db:
            db.query(AuditLog).filter(AuditLog.organization_id == self.organization_id).delete(
                synchronize_session=False
            )
            db.query(OrganizationAdminInvitation).filter_by(
                organization_id=self.organization_id
            ).delete(synchronize_session=False)
            db.query(OrganizationStaff).filter_by(organization_id=self.organization_id).delete(
                synchronize_session=False
            )
            db.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence('organization_staff', 'id'), "
                    "GREATEST(COALESCE((SELECT MAX(id) FROM organization_staff), 0), 1), true)"
                )
            )

    def _revision(self):
        with self.engine.connect() as connection:
            return connection.scalar(text("SELECT version_num FROM alembic_version"))

    @staticmethod
    def _run_alembic(operation, config, revision):
        # Alembic's in-process logging configuration disables existing
        # application loggers. Preserve the security logger so this migration
        # test cannot contaminate later security-event assertions.
        security_logger = logging.getLogger("radiusfiber.security")
        was_disabled = security_logger.disabled
        try:
            return operation(config, revision)
        finally:
            security_logger.disabled = was_disabled

    def _protected_snapshot(self):
        with self.engine.connect() as connection:
            protected_counts = tuple(
                connection.scalar(text(f"SELECT count(*) FROM {table_name}"))
                for table_name in ("customers", "billing_accounts", "radcheck")
            )
            staff_rows = tuple(
                connection.execute(
                    text(
                        "SELECT id, organization_id, name, email, password_hash, role, status, "
                        "is_temporary_password, is_uat_fixture, uat_fixture_id, "
                        "uat_expires_at, uat_revoked_at FROM organization_staff ORDER BY id"
                    )
                ).all()
            )
        return protected_counts, staff_rows

    @classmethod
    def tearDownClass(cls):
        with cls.Session.begin() as db:
            db.query(AuditLog).filter(AuditLog.organization_id == cls.organization_id).delete(
                synchronize_session=False
            )
            db.query(OrganizationAdminInvitation).filter_by(
                organization_id=cls.organization_id
            ).delete(synchronize_session=False)
            db.query(OrganizationStaff).filter_by(organization_id=cls.organization_id).delete(
                synchronize_session=False
            )
            db.query(Organization).filter_by(id=cls.organization_id).delete()
        cls.engine.dispose()

    def _create_concurrently(self, email: str):
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def worker():
            with self.Session() as db:
                barrier.wait()
                try:
                    result = create_admin_invitation(
                        db,
                        organization_id=self.organization_id,
                        email=email,
                        purpose="bootstrap",
                        reason="Authorized PostgreSQL concurrency test",
                        principal=self.principal,
                    )
                    db.commit()
                    outcome = (201, result.token)
                except HTTPException as exc:
                    db.rollback()
                    outcome = (exc.status_code, None)
                with lock:
                    outcomes.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        return outcomes

    def test_concurrent_bootstrap_creation_yields_one_active_invitation(self):
        outcomes = self._create_concurrently("bootstrap@postgres.test")
        self.assertEqual(sorted(code for code, _ in outcomes), [201, 409])
        with self.Session() as db:
            self.assertEqual(
                db.query(OrganizationAdminInvitation)
                .filter_by(organization_id=self.organization_id, purpose="bootstrap")
                .count(),
                1,
            )
    def test_concurrent_acceptance_yields_one_administrator(self):
        with self.Session.begin() as db:
            db.query(OrganizationAdminInvitation).filter_by(
                organization_id=self.organization_id
            ).delete(synchronize_session=False)
        with self.Session() as db:
            created = create_admin_invitation(
                db,
                organization_id=self.organization_id,
                email="accepted@postgres.test",
                purpose="bootstrap",
                reason="Authorized PostgreSQL acceptance test",
                principal=self.principal,
            )
            token = created.token
            db.commit()

        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def worker(number: int):
            with self.Session() as db:
                barrier.wait()
                try:
                    accept_admin_invitation(
                        db,
                        token=token,
                        new_password=f"Strong Concurrent Passphrase {number}!",
                        rate_identity="postgres-same-client",
                    )
                    db.commit()
                    outcome = 200
                except HTTPException as exc:
                    db.rollback()
                    outcome = exc.status_code
                with lock:
                    outcomes.append(outcome)

        threads = [threading.Thread(target=worker, args=(number,)) for number in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(outcomes), [200, 400])
        with self.Session() as db:
            self.assertEqual(
                db.query(OrganizationStaff).filter_by(
                    organization_id=self.organization_id,
                    role="Organization Admin",
                    status="active",
                ).count(),
                1,
            )

    def test_active_unused_invitation_refuses_downgrade_and_preserves_head(self):
        with self.Session() as db:
            created = create_admin_invitation(
                db,
                organization_id=self.organization_id,
                email="active-downgrade@postgres.test",
                purpose="bootstrap",
                reason="Authorized active invitation downgrade test",
                principal=self.principal,
            )
            invitation_id = created.invitation.id
            db.commit()

        config = Config("alembic.ini")
        with self.assertRaisesRegex(RuntimeError, "active administrator invitations remain"):
            self._run_alembic(command.downgrade, config, "0016_staging_uat_fixtures")

        self.assertEqual(self._revision(), "0017_organization_admin_invitations")
        with self.Session() as db:
            invitation = db.get(OrganizationAdminInvitation, invitation_id)
            self.assertIsNotNone(invitation)
            self.assertIsNone(invitation.used_at)
            self.assertIsNone(invitation.revoked_at)

    def test_completed_recovery_refuses_downgrade_without_losing_security_state(self):
        with self.Session() as db:
            staff = OrganizationStaff(
                organization_id=self.organization_id,
                name="Recovery Downgrade Admin",
                email="recovery-downgrade@postgres.test",
                password_hash=hash_password("Original Strong Passphrase 61!"),
                role="Organization Admin",
                status="active",
                is_temporary_password=False,
            )
            db.add(staff)
            db.commit()
            created = create_admin_invitation(
                db,
                organization_id=self.organization_id,
                email=staff.email,
                purpose="recovery",
                reason="Authorized recovery downgrade safety test",
                principal=self.principal,
            )
            token = created.token
            invitation_id = created.invitation.id
            staff_id = staff.id
            db.commit()
            accept_admin_invitation(
                db,
                token=token,
                new_password="Replacement Strong Passphrase 62!",
                rate_identity=f"recovery-downgrade-{self.suffix}",
            )
            db.commit()

        with self.engine.connect() as connection:
            staff_before = connection.execute(
                text(
                    "SELECT credential_version, credentials_revoked_at, password_hash "
                    "FROM organization_staff WHERE id = :staff_id"
                ),
                {"staff_id": staff_id},
            ).one()
            invitation_before = connection.execute(
                text(
                    "SELECT used_at, revoked_at, token_hash FROM organization_admin_invitations "
                    "WHERE id = :invitation_id"
                ),
                {"invitation_id": invitation_id},
            ).one()
            audit_before = tuple(
                connection.execute(
                    text(
                        "SELECT id, action, success, old_value, new_value FROM audit_logs "
                        "WHERE organization_id = :organization_id ORDER BY id"
                    ),
                    {"organization_id": self.organization_id},
                ).all()
            )
            token_revocations_before = connection.scalar(
                text("SELECT count(*) FROM auth_token_revocations")
            )

        self.assertEqual(staff_before.credential_version, 2)
        self.assertIsNotNone(staff_before.credentials_revoked_at)
        self.assertIsNotNone(invitation_before.used_at)
        config = Config("alembic.ini")
        with self.assertRaisesRegex(
            RuntimeError, "recovery-derived credential revocation state remains"
        ):
            self._run_alembic(command.downgrade, config, "0016_staging_uat_fixtures")

        self.assertEqual(self._revision(), "0017_organization_admin_invitations")
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    text(
                        "SELECT credential_version, credentials_revoked_at, password_hash "
                        "FROM organization_staff WHERE id = :staff_id"
                    ),
                    {"staff_id": staff_id},
                ).one(),
                staff_before,
            )
            self.assertEqual(
                connection.execute(
                    text(
                        "SELECT used_at, revoked_at, token_hash "
                        "FROM organization_admin_invitations WHERE id = :invitation_id"
                    ),
                    {"invitation_id": invitation_id},
                ).one(),
                invitation_before,
            )
            self.assertEqual(
                tuple(
                    connection.execute(
                        text(
                            "SELECT id, action, success, old_value, new_value FROM audit_logs "
                            "WHERE organization_id = :organization_id ORDER BY id"
                        ),
                        {"organization_id": self.organization_id},
                    ).all()
                ),
                audit_before,
            )
            self.assertEqual(
                connection.scalar(text("SELECT count(*) FROM auth_token_revocations")),
                token_revocations_before,
            )

    def test_clean_downgrade_and_reupgrade_preserve_protected_and_fixture_data(self):
        before = self._protected_snapshot()
        config = Config("alembic.ini")

        self._run_alembic(command.downgrade, config, "0016_staging_uat_fixtures")
        self.assertEqual(self._revision(), "0016_staging_uat_fixtures")
        with self.engine.connect() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns("organization_staff")}
        self.assertNotIn("credential_version", columns)
        self.assertNotIn("credentials_revoked_at", columns)

        self._run_alembic(command.upgrade, config, "0017_organization_admin_invitations")
        self.assertEqual(self._revision(), "0017_organization_admin_invitations")
        self.assertEqual(self._protected_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
