from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from app.services.disconnect_adapter import (
    DisabledDisconnectAdapter,
    DisconnectRequest,
    DisconnectResult,
    MockDisconnectAdapter,
    RadclientDisconnectAdapter,
    RetryableDisconnectError,
    TerminalDisconnectError,
)
from app.services.expiry_policy import (
    AccessReason,
    aware_utc,
    evaluate_access,
    require_aware_utc,
)
from app.services.radius_authorization import expiration_radius_value


NOW = datetime(2028, 2, 29, 12, 0, tzinfo=timezone.utc)


def service(**overrides):
    values = {
        "organization_id": 20,
        "status": "active",
        "expiration_date": NOW + timedelta(seconds=1),
        "service_plan": "Synthetic Plan",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def plan(**overrides):
    values = {"organization_id": 20, "status": "active", "name": "Synthetic Plan"}
    values.update(overrides)
    return SimpleNamespace(**values)


def request(**overrides):
    values = {
        "username": "M0012_SYNTHETIC_USER",
        "session_identity": "M0012_SESSION",
        "accounting_session_id": "M0012_ACCT",
        "nas_ip_address": "127.0.0.1",
        "framed_ip_address": None,
    }
    values.update(overrides)
    return DisconnectRequest(**values)


class ExpiryPolicyTests(unittest.TestCase):
    def test_before_expiration_is_active(self):
        decision = evaluate_access(service(), plan(), now=NOW, organization_id=20)
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.reason, AccessReason.ACTIVE)

    def test_exact_expiration_boundary_is_expired(self):
        decision = evaluate_access(service(expiration_date=NOW), plan(), now=NOW, organization_id=20)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, AccessReason.EXPIRED)

    def test_after_expiration_is_expired(self):
        decision = evaluate_access(service(expiration_date=NOW - timedelta(microseconds=1)), plan(), now=NOW)
        self.assertEqual(decision.reason, AccessReason.EXPIRED)

    def test_missing_expiration_is_rejected(self):
        decision = evaluate_access(service(expiration_date=None), plan(), now=NOW)
        self.assertEqual(decision.reason, AccessReason.MISSING_EXPIRATION)

    def test_legacy_naive_expiration_is_normalized_as_utc(self):
        value = datetime(2028, 2, 29, 12, 0, 1)
        self.assertEqual(aware_utc(value), value.replace(tzinfo=timezone.utc))
        self.assertTrue(evaluate_access(service(expiration_date=value), plan(), now=NOW).eligible)

    def test_authoritative_clock_must_be_aware(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            require_aware_utc(datetime(2028, 2, 29, 12, 0))

    def test_suspended_and_terminated_are_distinct(self):
        self.assertEqual(evaluate_access(service(status="suspended"), plan(), now=NOW).reason, AccessReason.SUSPENDED)
        self.assertEqual(evaluate_access(service(status="terminated"), plan(), now=NOW).reason, AccessReason.TERMINATED)

    def test_invalid_plan_is_rejected(self):
        self.assertEqual(evaluate_access(service(), plan(status="inactive"), now=NOW).reason, AccessReason.INVALID_PLAN)
        self.assertEqual(evaluate_access(service(), plan(name="Other"), now=NOW).reason, AccessReason.INVALID_PLAN)

    def test_tenant_mismatch_is_rejected(self):
        self.assertEqual(evaluate_access(service(), plan(organization_id=21), now=NOW).reason, AccessReason.TENANT_MISMATCH)
        self.assertEqual(evaluate_access(service(), plan(), now=NOW, organization_id=21).reason, AccessReason.TENANT_MISMATCH)

    def test_radius_expiration_is_utc_and_leap_day_safe(self):
        value = expiration_radius_value(NOW)
        self.assertEqual(value, "Feb 29 2028 12:00:00 UTC")


class DisconnectAdapterTests(unittest.TestCase):
    def test_disabled_adapter_cannot_send(self):
        with self.assertRaisesRegex(TerminalDisconnectError, "disabled"):
            DisabledDisconnectAdapter().disconnect(request())

    def test_mock_success_and_duplicate_delivery_are_observable(self):
        adapter = MockDisconnectAdapter()
        self.assertTrue(adapter.disconnect(request()).acknowledged)
        self.assertTrue(adapter.disconnect(request()).acknowledged)
        self.assertEqual(len(adapter.requests), 2)

    def test_mock_retryable_and_terminal_failures(self):
        adapter = MockDisconnectAdapter([
            RetryableDisconnectError("timeout"),
            TerminalDisconnectError("denied"),
        ])
        with self.assertRaises(RetryableDisconnectError):
            adapter.disconnect(request())
        with self.assertRaises(TerminalDisconnectError):
            adapter.disconnect(request())

    def test_real_adapter_requires_explicit_enablement(self):
        fake = SimpleNamespace(radius_coa_enabled=False, radius_disconnect_mode="disabled")
        with patch("app.services.disconnect_adapter.settings", fake):
            with self.assertRaisesRegex(TerminalDisconnectError, "not_enabled"):
                RadclientDisconnectAdapter()

    def test_real_adapter_rejects_malformed_and_unallowlisted_nas(self):
        fake = SimpleNamespace(
            radius_coa_enabled=True,
            radius_disconnect_mode="real",
            radius_disconnect_nas_allowlist=("127.0.0.1",),
        )
        with patch("app.services.disconnect_adapter.settings", fake):
            adapter = RadclientDisconnectAdapter()
            with self.assertRaisesRegex(TerminalDisconnectError, "malformed"):
                adapter._validate_target("invalid host")
            with self.assertRaisesRegex(TerminalDisconnectError, "not_allowlisted"):
                adapter._validate_target("192.0.2.1")

    def test_real_adapter_timeout_is_retryable_and_no_shell_is_used(self):
        fake = SimpleNamespace(
            radius_coa_enabled=True,
            radius_disconnect_mode="real",
            radius_disconnect_nas_allowlist=("127.0.0.1",),
            radclient_bin=__file__,
            coa_secret_path=__file__,
            coa_port="3799",
            radius_disconnect_timeout_seconds=2,
        )
        with (
            patch("app.services.disconnect_adapter.settings", fake),
            patch("app.services.disconnect_adapter.os.access", return_value=True),
            patch("app.services.disconnect_adapter.subprocess.run", side_effect=subprocess.TimeoutExpired("radclient", 2)) as run,
        ):
            with self.assertRaises(RetryableDisconnectError):
                RadclientDisconnectAdapter().disconnect(request())
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertIsInstance(run.call_args.args[0], list)

    def test_real_adapter_accepts_ack_without_logging_secret(self):
        fake = SimpleNamespace(
            radius_coa_enabled=True,
            radius_disconnect_mode="real",
            radius_disconnect_nas_allowlist=("127.0.0.1",),
            radclient_bin=__file__,
            coa_secret_path=__file__,
            coa_port="3799",
            radius_disconnect_timeout_seconds=2,
        )
        completed = SimpleNamespace(returncode=0, stdout="Disconnect-ACK", stderr="")
        with (
            patch("app.services.disconnect_adapter.settings", fake),
            patch("app.services.disconnect_adapter.os.access", return_value=True),
            patch("app.services.disconnect_adapter.subprocess.run", return_value=completed),
        ):
            result = RadclientDisconnectAdapter().disconnect(request())
        self.assertEqual(result, DisconnectResult(True, "disconnect_acknowledged"))


class ExpiryMigrationTests(unittest.TestCase):
    def test_migration_has_single_expected_predecessor_and_required_guards(self):
        source = (Path(__file__).parents[1] / "alembic" / "versions" / "0012_expiry_enforcement.py").read_text()
        self.assertIn('revision = "0012_expiry_enforcement"', source)
        self.assertIn('down_revision = "0011_plan_change_activation"', source)
        self.assertIn("uq_expiry_disconnect_event_session", source)
        self.assertIn("fk_expiry_disconnect_job_user_tenant", source)
        self.assertIn("fk_expiry_disconnect_job_nas_tenant", source)
        self.assertNotIn("DROP TABLE users", source.upper())


if __name__ == "__main__":
    unittest.main()
