import json
import unittest
from collections import namedtuple
from unittest.mock import patch

from app import main


REQUIRED_TABLES = {
    "platform",
    "organizations",
    "subscriptions",
    "audit_logs",
    "feature_flags",
    "roles",
    "customers",
    "users",
    "service_plans",
    "billing_accounts",
    "radacct",
    "organization_staff",
    "organization_roles",
    "zones",
    "organization_billing_profiles",
    "notification_settings",
    "payment_transactions",
    "customer_portal_accounts",
    "support_tickets",
    "ticket_messages",
    "payment_webhook_events",
    "network_access_servers",
    "expiry_scan_runs",
    "expiry_disconnect_jobs",
    "radius_reject_ownerships",
    "auth_token_revocations",
    "olt_credential_references",
    "olt_devices",
    "olt_cards",
    "olt_uplinks",
    "olt_pon_ports",
    "olt_onus",
    "olt_service_associations",
    "olt_poll_runs",
    "alembic_version",
}


class _Result:
    def __init__(self, revision=None):
        self.revision = revision

    def scalar_one_or_none(self):
        return self.revision


class _Connection:
    def __init__(self, revision):
        self.revision = revision

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement):
        if "alembic_version" in str(statement):
            return _Result(self.revision)
        return _Result()


class _Engine:
    def __init__(self, revision):
        self.revision = revision

    def connect(self):
        return _Connection(self.revision)


class _Inspector:
    @staticmethod
    def get_table_names():
        return list(REQUIRED_TABLES)


class HealthReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def _request(self, revision):
        disk_usage = namedtuple("usage", "total used free")(1024, 0, 1024 * 1024 * 1024)
        messages = []
        request_sent = False

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        with (
            patch.object(main, "engine", _Engine(revision)),
            patch.object(main, "inspect", return_value=_Inspector()),
            patch.object(main.os.path, "isfile", return_value=True),
            patch.object(main.os, "access", return_value=True),
            patch.object(main.shutil, "disk_usage", return_value=disk_usage),
        ):
            await main.app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/health/ready",
                    "raw_path": b"/health/ready",
                    "query_string": b"",
                    "headers": [],
                    "client": ("127.0.0.1", 12345),
                    "server": ("127.0.0.1", 80),
                    "root_path": "",
                },
                receive,
                send,
            )
        status = next(message["status"] for message in messages if message["type"] == "http.response.start")
        body = b"".join(
            message.get("body", b"") for message in messages if message["type"] == "http.response.body"
        )
        return status, json.loads(body)

    async def test_current_0015_revision_is_ready(self):
        status, body = await self._request("0015_expiry_reject_ownership")

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ready")
        self.assertTrue(body["checks"]["migration_status"])

    async def test_mismatched_revision_is_not_ready(self):
        status, body = await self._request("0009_network_access_servers")

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "not_ready")
        self.assertFalse(body["checks"]["migration_status"])


if __name__ == "__main__":
    unittest.main()
