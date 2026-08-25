from __future__ import annotations

import unittest
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, sentinel

from fastapi import HTTPException

from app.core import authorization, principal
from app.models.auth_token_revocation import AuthTokenRevocation
from app.routers import auth, customer_auth
from app.services.token_revocation import ensure_token_not_revoked, revoke_token
from app.services.token_revocation import cleanup_expired_revocations


class TokenClaimTests(unittest.TestCase):
    def test_customer_token_has_standard_claims_and_configured_one_hour_ttl(self):
        configured = replace(
            principal.settings,
            jwt_secret="test-secret-that-is-not-a-production-credential",
            auth_access_token_ttl_seconds=3600,
        )
        with patch.object(principal, "settings", configured), patch.object(principal.time, "time", return_value=1000):
            token = principal.create_principal_token(
                {"sub": "7", "principal_type": "customer"}
            )
            payload = principal.decode_principal_token(token)
        self.assertEqual(payload["token_type"], "access")
        self.assertEqual(payload["exp"] - payload["iat"], 3600)
        self.assertEqual(payload["iss"], configured.auth_token_issuer)
        self.assertEqual(payload["aud"], configured.auth_token_audience)
        self.assertTrue(payload["jti"])

    def _signed_token(self, claims):
        encoded = principal._b64url_encode(
            json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        return f"{encoded}.{principal._sign(encoded, principal.settings.jwt_secret)}"

    def test_pre_jti_tokens_for_all_user_principals_require_relogin(self):
        configured = replace(
            principal.settings,
            jwt_secret="test-secret-that-is-not-a-production-credential",
        )
        for principal_type in ("platform_admin", "organization_staff", "customer"):
            with self.subTest(principal_type=principal_type), patch.object(principal, "settings", configured):
                token = self._signed_token(
                    {
                        "sub": f"legacy-{principal_type}",
                        "principal_type": principal_type,
                        "token_type": "access",
                        "iat": 1000,
                        "exp": 5000,
                        "iss": configured.auth_token_issuer,
                        "aud": configured.auth_token_audience,
                    }
                )
                with patch.object(principal.time, "time", return_value=2000), self.assertRaises(HTTPException) as raised:
                    principal.decode_principal_token(token)
                self.assertEqual(raised.exception.status_code, 401)
                self.assertEqual(raised.exception.detail["error"], "invalid_token")
                self.assertNotIn("denylist", str(raised.exception.detail).lower())

    def test_empty_and_invalidly_typed_jti_are_rejected(self):
        configured = replace(
            principal.settings,
            jwt_secret="test-secret-that-is-not-a-production-credential",
        )
        for invalid_jti in ("", 17, None, ["not-a-jti"]):
            with self.subTest(jti=invalid_jti), patch.object(principal, "settings", configured):
                token = self._signed_token(
                    {
                        "sub": "legacy-platform",
                        "principal_type": "platform_admin",
                        "token_type": "access",
                        "iat": 1000,
                        "exp": 5000,
                        "iss": configured.auth_token_issuer,
                        "aud": configured.auth_token_audience,
                        "jti": invalid_jti,
                    }
                )
                with patch.object(principal.time, "time", return_value=2000), self.assertRaises(HTTPException):
                    principal.decode_principal_token(token)

    def test_organization_staff_decoder_rejects_legacy_and_invalid_jti(self):
        configured = replace(
            authorization.settings,
            jwt_secret="test-secret-that-is-not-a-production-credential",
        )
        base_claims = {
            "sub": "staff:9",
            "principal_type": "organization_staff",
            "token_type": "access",
            "staff_id": 9,
            "organization_id": 20,
            "organization_slug": "synthetic-tenant",
            "iat": 1000,
            "exp": 5000,
            "iss": configured.auth_token_issuer,
            "aud": configured.auth_token_audience,
        }
        for jti in (sentinel.missing, "", 17, None):
            claims = dict(base_claims)
            if jti is not sentinel.missing:
                claims["jti"] = jti
            token = authorization.create_access_token(claims, configured.jwt_secret)
            with self.subTest(jti=jti), patch.object(authorization, "settings", configured), self.assertRaises(HTTPException) as raised:
                authorization.decode_access_token(token, configured.jwt_secret, now=2000)
            self.assertEqual(raised.exception.status_code, 401)
            self.assertNotIn("denylist", str(raised.exception.detail).lower())

    def test_issued_tokens_use_distinct_jtis(self):
        configured = replace(
            principal.settings,
            jwt_secret="test-secret-that-is-not-a-production-credential",
        )
        with patch.object(principal, "settings", configured):
            first = principal.decode_principal_token(
                principal.create_principal_token({"sub": "one", "principal_type": "customer"})
            )
            second = principal.decode_principal_token(
                principal.create_principal_token({"sub": "one", "principal_type": "customer"})
            )
        self.assertNotEqual(first["jti"], second["jti"])


class RevocationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "sub": "staff:9",
            "principal_type": "organization_staff",
            "organization_id": 20,
            "jti": "raw-jti-must-not-be-stored",
            "exp": 2_000_000_000,
        }

    def test_revoke_persists_only_a_hash_and_is_idempotent(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        self.assertTrue(revoke_token(db, self.payload))
        row = db.add.call_args.args[0]
        self.assertIsInstance(row, AuthTokenRevocation)
        self.assertEqual(len(row.jti_hash), 64)
        self.assertNotEqual(row.jti_hash, self.payload["jti"])

        second = MagicMock()
        second.query.return_value.filter.return_value.first.return_value = SimpleNamespace(id=1)
        self.assertFalse(revoke_token(second, self.payload))
        second.add.assert_not_called()

    def test_revoked_token_is_rejected(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(id=1)
        with self.assertRaises(HTTPException) as raised:
            ensure_token_not_revoked(db, self.payload)
        self.assertEqual(raised.exception.status_code, 401)

    def test_revocation_lookup_failure_fails_closed(self):
        db = MagicMock()
        # Patch the query call with SQLAlchemy's public base exception to prove a 503, not fail-open.
        from sqlalchemy.exc import SQLAlchemyError

        db.query.side_effect = SQLAlchemyError("database unavailable")
        with self.assertRaises(HTTPException) as raised:
            ensure_token_not_revoked(db, self.payload)
        self.assertEqual(raised.exception.status_code, 503)

    def test_cleanup_uses_database_time_stable_order_and_exact_limit(self):
        db = MagicMock()
        db.scalars.return_value = [7, 8]
        db.execute.return_value.rowcount = 2
        self.assertEqual(
            cleanup_expired_revocations(db, batch_size=2, retention_seconds=300),
            2,
        )
        selection = str(db.scalars.call_args.args[0]).upper()
        self.assertIn("CURRENT_TIMESTAMP", selection)
        self.assertIn("EXPIRES_AT <= CURRENT_TIMESTAMP", selection)
        self.assertIn("ORDER BY", selection)
        self.assertIn("EXPIRES_AT", selection)
        self.assertIn("AUTH_TOKEN_REVOCATIONS.ID", selection)
        self.assertIn("LIMIT", selection)
        deletion = str(db.execute.call_args.args[0]).upper()
        self.assertIn("DELETE FROM AUTH_TOKEN_REVOCATIONS", deletion)

    def test_cleanup_with_no_expired_rows_is_idempotent(self):
        db = MagicMock()
        db.scalars.return_value = []
        self.assertEqual(cleanup_expired_revocations(db, batch_size=100), 0)
        self.assertEqual(cleanup_expired_revocations(db, batch_size=100), 0)
        db.execute.assert_not_called()

    def test_cleanup_batch_is_bounded(self):
        db = MagicMock()
        with self.assertRaises(ValueError):
            cleanup_expired_revocations(db, batch_size=0)
        with self.assertRaises(ValueError):
            cleanup_expired_revocations(db, batch_size=1001)

    def test_cleanup_retention_is_securely_bounded(self):
        db = MagicMock()
        with self.assertRaises(ValueError):
            cleanup_expired_revocations(db, retention_seconds=59)
        with self.assertRaises(ValueError):
            cleanup_expired_revocations(db, retention_seconds=3601)

    def test_cleanup_failure_does_not_discard_new_revocation(self):
        from sqlalchemy.exc import SQLAlchemyError

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        with patch(
            "app.services.token_revocation.cleanup_expired_revocations",
            side_effect=SQLAlchemyError("cleanup unavailable"),
        ):
            self.assertTrue(revoke_token(db, self.payload))
        db.add.assert_called_once()
        db.rollback.assert_not_called()


class CustomerOrganizationStatusTests(unittest.TestCase):
    def test_inactive_organization_invalidates_customer_token(self):
        account = SimpleNamespace(id=11, customer_id="RF_UAT", organization_id=20, status="active")
        organization = SimpleNamespace(id=20, status="suspended")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [account, organization]
        claims = {
            "sub": "11",
            "principal_type": "customer",
            "organization_id": 20,
            "customer_id": "RF_UAT",
            "jti": "customer-jti",
        }
        with (
            patch.object(customer_auth, "bearer_payload", return_value=claims),
            patch.object(customer_auth, "ensure_token_not_revoked"),
            self.assertRaises(HTTPException) as raised,
        ):
            customer_auth._account_from_token(db, "Bearer token")
        self.assertEqual(raised.exception.status_code, 401)


class LogoutContractTests(unittest.TestCase):
    def test_workspace_logout_revokes_and_audits_without_exposing_jti(self):
        payload = {
            "sub": "platform-admin",
            "principal_type": "platform_admin",
            "jti": "workspace-logout-jti",
            "exp": 2_000_000_000,
        }
        db = MagicMock()
        with (
            patch.object(auth, "decode_principal_token", return_value=payload),
            patch.object(auth, "revoke_token", return_value=True) as revoke,
            patch.object(auth, "record_audit") as audit,
        ):
            result = auth.logout("Bearer signed-token", db)
        self.assertEqual(result, {"status": "ok"})
        revoke.assert_called_once_with(db, payload)
        self.assertNotEqual(audit.call_args.kwargs["target_id"], payload["jti"])
        db.commit.assert_called_once()

    def test_customer_logout_revokes_and_audits(self):
        account = SimpleNamespace(id=11, organization_id=20, email="uat-customer@smartfiber.test")
        payload = {
            "sub": "11",
            "principal_type": "customer",
            "jti": "customer-logout-jti",
            "exp": 2_000_000_000,
        }
        db = MagicMock()
        with (
            patch.object(customer_auth, "_account_from_token", return_value=(account, SimpleNamespace(), payload)),
            patch.object(customer_auth, "revoke_token", return_value=True) as revoke,
            patch.object(customer_auth, "record_audit") as audit,
        ):
            result = customer_auth.logout("Bearer signed-token", db)
        self.assertEqual(result, {"status": "ok"})
        revoke.assert_called_once_with(db, payload)
        self.assertEqual(audit.call_args.kwargs["action"], "auth.logout")
        db.commit.assert_called_once()


class MigrationAndHarnessTests(unittest.TestCase):
    def test_migration_chain_and_fixture_safety_guards_are_explicit(self):
        root = Path(__file__).parents[1]
        migration = (root / "alembic" / "versions" / "0013_auth_token_revocations.py").read_text()
        harness = (root / "scripts" / "manage_auth_uat_fixtures.py").read_text()
        self.assertIn('revision = "0013_auth_token_revocations"', migration)
        self.assertIn('down_revision = "0012_expiry_enforcement"', migration)
        self.assertIn('"ix_auth_token_revocations_cleanup"', migration)
        self.assertIn('startswith("isp_db_jwt_fixture_")', harness)
        self.assertIn('RADIUSFIBER_ALLOW_DISPOSABLE_FIXTURES', harness)
        self.assertNotIn('TEST_ONU"', harness)


if __name__ == "__main__":
    unittest.main()
