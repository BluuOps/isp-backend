from __future__ import annotations

import socket
import subprocess
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.core.authorization import (
    ALL_ORGANIZATION_PERMISSIONS,
    OLT_FOUNDATION_PERMISSIONS,
    AuthenticatedPrincipal,
    Permission,
    PrincipalType,
    require_permission,
    role_permissions,
)
from app.integrations.olt import OltCapability, ResultState, adapter_registry, get_adapter
from app.integrations.olt.contracts import AdapterContext
from app.main import app
from app.core.platform_auth import require_platform_admin
from app.core.principal import reject_customer_principal
from app.routers.olt import _disabled_operation
from app.models import OltDevice, OltOnu, OltServiceAssociation
from app.services.audit import redact
from app.services.olt_security import ManagementAddressError, validate_management_address
from app.services.olt_inventory import STALE_CACHE_STATUSES
from app.schemas.olt import OltDeviceResponse
import app.services.olt_security as olt_security


def principal(role: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        subject_id="staff:1",
        principal_type=PrincipalType.ORGANIZATION_STAFF,
        authentication_method="fixture",
        active=True,
        platform_authority=False,
        organization_id=10,
        organization_slug="fixture",
        organization_role=role,
        effective_permissions=role_permissions(role),
    )


def dependency_calls(node):
    calls = []
    for dependency in getattr(node, "dependencies", []):
        call = getattr(dependency, "call", None)
        if call is not None:
            calls.append(call)
        calls.extend(dependency_calls(dependency))
    return calls


def route_for(method: str, path: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in (getattr(route, "methods", set()) or set()):
            return route
    raise AssertionError(f"Route not registered: {method} {path}")


class OltPermissionTests(unittest.TestCase):
    def test_only_foundation_permissions_are_active(self):
        self.assertEqual(
            OLT_FOUNDATION_PERMISSIONS,
            {
                Permission.OLT_INVENTORY_READ,
                Permission.OLT_TELEMETRY_READ,
                Permission.OLT_ALARMS_READ,
                Permission.OLT_AUDIT_READ,
                Permission.OLT_DEVICES_MANAGE,
                Permission.OLT_CONNECTIONS_TEST,
                Permission.OLT_POLL_REQUEST,
                Permission.OLT_ASSOCIATIONS_MANAGE,
            },
        )
        self.assertFalse(any("provision" in item or "reboot" in item or "configure" in item for item in ALL_ORGANIZATION_PERMISSIONS))

    def test_future_constant_cannot_mutate_frozen_admin_catalogue(self):
        before = ALL_ORGANIZATION_PERMISSIONS
        Permission.OLT_ONU_PROVISION_FUTURE = "olt.onu.provision"
        try:
            self.assertEqual(ALL_ORGANIZATION_PERMISSIONS, before)
            self.assertNotIn(Permission.OLT_ONU_PROVISION_FUTURE, role_permissions("Organization Admin"))
        finally:
            delattr(Permission, "OLT_ONU_PROVISION_FUTURE")

    def test_role_matrix(self):
        admin = principal("Organization Admin")
        noc = principal("NOC")
        support = principal("Support")
        field = principal("Field Engineer")
        billing = principal("Billing")
        readonly = principal("Read Only")
        for permission in OLT_FOUNDATION_PERMISSIONS:
            self.assertTrue(admin.has_permission(permission))
        for permission in (
            Permission.OLT_INVENTORY_READ,
            Permission.OLT_TELEMETRY_READ,
            Permission.OLT_ALARMS_READ,
            Permission.OLT_AUDIT_READ,
            Permission.OLT_CONNECTIONS_TEST,
            Permission.OLT_POLL_REQUEST,
            Permission.OLT_ASSOCIATIONS_MANAGE,
        ):
            self.assertTrue(noc.has_permission(permission))
        for actor in (support, field):
            self.assertTrue(actor.has_permission(Permission.OLT_INVENTORY_READ))
            self.assertTrue(actor.has_permission(Permission.OLT_TELEMETRY_READ))
            self.assertFalse(actor.has_permission(Permission.OLT_DEVICES_MANAGE))
        for permission in OLT_FOUNDATION_PERMISSIONS:
            self.assertFalse(billing.has_permission(permission))
        for permission in (Permission.OLT_INVENTORY_READ, Permission.OLT_TELEMETRY_READ, Permission.OLT_ALARMS_READ):
            self.assertTrue(readonly.has_permission(permission))
        self.assertFalse(readonly.has_permission(Permission.OLT_DEVICES_MANAGE))

    def test_customer_principal_is_denied(self):
        customer = AuthenticatedPrincipal(
            subject_id="customer:1",
            principal_type=PrincipalType.CUSTOMER,
            authentication_method="fixture",
            active=True,
            platform_authority=False,
            organization_id=10,
            organization_slug="fixture",
            organization_role=None,
            effective_permissions=frozenset(),
            customer_id="customer-1",
        )
        with self.assertRaises(HTTPException) as raised:
            require_permission(Permission.OLT_INVENTORY_READ)(customer)
        self.assertEqual(raised.exception.status_code, 403)


class NullAdapterTests(unittest.TestCase):
    def setUp(self):
        self.context = AdapterContext(10, 20, "10.10.10.10", maximum_items=100)

    def test_registry_is_fixed_to_null_adapter(self):
        self.assertEqual(set(adapter_registry), {"null"})
        with self.assertRaisesRegex(ValueError, "unsupported_adapter"):
            get_adapter("module.path.from.user")

    def test_contract_capabilities_and_states(self):
        self.assertEqual(get_adapter("null").capabilities(), frozenset(OltCapability))
        expected = {
            "available": ResultState.AVAILABLE,
            "unsupported": ResultState.UNSUPPORTED,
            "unavailable": ResultState.UNAVAILABLE,
            "stale": ResultState.STALE,
            "failure": ResultState.UNAVAILABLE,
        }
        for mode, state in expected.items():
            with self.subTest(mode=mode):
                result = get_adapter("null", mode=mode).read_onus(self.context)
                self.assertEqual(result.state, state)
                self.assertIsNotNone(result.observed_at)
                if mode in {"unsupported", "unavailable", "failure"}:
                    self.assertIsNotNone(result.error_code)

    def test_null_adapter_does_not_create_network_or_process_io(self):
        adapter = get_adapter("null")
        with (
            patch.object(socket, "create_connection", side_effect=AssertionError("network attempted")),
            patch.object(subprocess, "run", side_effect=AssertionError("process attempted")),
        ):
            self.assertEqual(adapter.test_connection(self.context).state, ResultState.AVAILABLE)
            self.assertEqual(adapter.read_cards(self.context).state, ResultState.AVAILABLE)
            self.assertEqual(adapter.read_onu_optics(self.context, "fixture-onu").state, ResultState.AVAILABLE)


class OltSecurityTests(unittest.TestCase):
    def test_management_address_allowlist(self):
        self.assertEqual(validate_management_address("10.10.10.10"), "10.10.10.10")
        self.assertEqual(validate_management_address("192.168.222.252"), "192.168.222.252")
        for address in ("127.0.0.1", "169.254.1.1", "224.0.0.1", "0.0.0.0", "8.8.8.8", "olt.example.test"):
            with self.subTest(address=address), self.assertRaises(ManagementAddressError):
                validate_management_address(address)

    def test_malformed_management_network_configuration_fails_closed(self):
        with patch.object(
            olt_security,
            "settings",
            replace(olt_security.settings, olt_management_networks=("not-a-network",)),
        ):
            with self.assertRaisesRegex(ManagementAddressError, "management_network_allowlist_invalid"):
                validate_management_address("10.10.10.10")

    def test_recursive_secret_redaction(self):
        value = {
            "name": "safe",
            "nested": {
                "password": "not-safe",
                "items": [
                    {"snmp_community": "not-safe"},
                    {"private-key": "not-safe"},
                    {"credential_reference_id": 42},
                    "safe-value",
                ],
            },
            "Authorization": "Bearer not-safe",
        }
        result = redact(value)
        self.assertEqual(result["name"], "safe")
        self.assertEqual(result["nested"]["password"], "[REDACTED]")
        self.assertEqual(result["nested"]["items"][0]["snmp_community"], "[REDACTED]")
        self.assertEqual(result["nested"]["items"][1]["private-key"], "[REDACTED]")
        self.assertEqual(result["nested"]["items"][2]["credential_reference_id"], "[REDACTED]")
        self.assertEqual(result["nested"]["items"][3], "safe-value")
        self.assertEqual(result["Authorization"], "[REDACTED]")


class OltSchemaAndRouteTests(unittest.TestCase):
    def test_database_metadata_has_only_authorized_0014_tables(self):
        expected = {
            "olt_credential_references",
            "olt_devices",
            "olt_cards",
            "olt_uplinks",
            "olt_pon_ports",
            "olt_onus",
            "olt_service_associations",
            "olt_poll_runs",
        }
        from app.database import Base

        self.assertEqual({name for name in Base.metadata.tables if name.startswith("olt_")}, expected)
        credential_columns = set(Base.metadata.tables["olt_credential_references"].columns.keys())
        forbidden = {"username", "password", "community", "passphrase", "private_key", "secret", "secret_value"}
        self.assertFalse(credential_columns & forbidden)

    def test_read_response_does_not_expose_credential_reference(self):
        self.assertNotIn("credential_reference_id", OltDeviceResponse.model_fields)

    def test_freshness_uses_one_authoritative_stale_state_set(self):
        self.assertEqual(STALE_CACHE_STATUSES, {"empty", "stale", "unavailable"})

    def test_composite_tenant_foreign_keys_exist(self):
        association_fks = {
            tuple(column.name for column in constraint.columns)
            for constraint in OltServiceAssociation.__table__.foreign_key_constraints
        }
        self.assertIn(("onu_id", "organization_id"), association_fks)
        self.assertIn(("customer_id", "organization_id"), association_fks)
        self.assertIn(("user_id", "organization_id"), association_fks)
        onu_fks = {
            tuple(column.name for column in constraint.columns)
            for constraint in OltOnu.__table__.foreign_key_constraints
        }
        self.assertIn(("olt_device_id", "organization_id"), onu_fks)
        self.assertIn(("pon_port_id", "organization_id"), onu_fks)

    def test_query_indexes_are_declared_in_orm_metadata(self):
        from app.database import Base

        expected = {
            "olt_devices": "ix_olt_devices_org_status",
            "olt_onus": "ix_olt_onus_org_serial",
            "olt_poll_runs": "ix_olt_poll_runs_org_created",
        }
        for table_name, index_name in expected.items():
            with self.subTest(table=table_name):
                self.assertIn(index_name, {index.name for index in Base.metadata.tables[table_name].indexes})

    def test_routes_are_versioned_and_contain_no_command_execution(self):
        paths = {route.path for route in app.routes}
        self.assertIn("/organization/olt/v1/devices", paths)
        self.assertIn("/platform/organizations/{organization_id}/olt/v1/devices", paths)
        self.assertIn("/organization/olt/v1/devices/{device_id}/connection-tests", paths)
        self.assertFalse(any("command" in path or "execute" in path or "provision" in path for path in paths if "/olt/" in path))

    def test_organization_routes_have_explicit_permission_and_customer_denial_guards(self):
        expected = {
            ("GET", "/organization/olt/v1/overview"): Permission.OLT_INVENTORY_READ,
            ("GET", "/organization/olt/v1/devices"): Permission.OLT_INVENTORY_READ,
            ("POST", "/organization/olt/v1/devices"): Permission.OLT_DEVICES_MANAGE,
            ("GET", "/organization/olt/v1/devices/{device_id}"): Permission.OLT_INVENTORY_READ,
            ("PATCH", "/organization/olt/v1/devices/{device_id}"): Permission.OLT_DEVICES_MANAGE,
            ("GET", "/organization/olt/v1/devices/{device_id}/system-info"): Permission.OLT_INVENTORY_READ,
            ("GET", "/organization/olt/v1/devices/{device_id}/cards"): Permission.OLT_INVENTORY_READ,
            ("GET", "/organization/olt/v1/devices/{device_id}/uplinks"): Permission.OLT_INVENTORY_READ,
            ("GET", "/organization/olt/v1/devices/{device_id}/pon-ports"): Permission.OLT_INVENTORY_READ,
            ("GET", "/organization/olt/v1/devices/{device_id}/onus"): Permission.OLT_INVENTORY_READ,
            ("GET", "/organization/olt/v1/devices/{device_id}/poll-runs"): Permission.OLT_INVENTORY_READ,
            ("GET", "/organization/olt/v1/onus/{onu_id}"): Permission.OLT_INVENTORY_READ,
            ("GET", "/organization/olt/v1/associations"): Permission.OLT_INVENTORY_READ,
            ("POST", "/organization/olt/v1/associations"): Permission.OLT_ASSOCIATIONS_MANAGE,
            ("DELETE", "/organization/olt/v1/associations/{association_id}"): Permission.OLT_ASSOCIATIONS_MANAGE,
            ("GET", "/organization/olt/v1/audit-events"): Permission.OLT_AUDIT_READ,
            ("POST", "/organization/olt/v1/devices/{device_id}/connection-tests"): Permission.OLT_CONNECTIONS_TEST,
            ("POST", "/organization/olt/v1/devices/{device_id}/refresh-requests"): Permission.OLT_POLL_REQUEST,
        }
        for route_key, permission in expected.items():
            with self.subTest(route=route_key):
                calls = dependency_calls(route_for(*route_key).dependant)
                self.assertIn(reject_customer_principal, calls)
                self.assertIn(permission, {getattr(call, "required_permission", None) for call in calls})

    def test_every_platform_olt_route_requires_platform_admin(self):
        routes = [
            route for route in app.routes
            if getattr(route, "path", "").startswith("/platform/organizations/{organization_id}/olt/v1")
        ]
        self.assertTrue(routes)
        for route in routes:
            with self.subTest(path=route.path):
                self.assertIn(require_platform_admin, dependency_calls(route.dependant))

    def test_operational_routes_are_explicitly_disabled(self):
        with self.assertRaises(HTTPException) as raised:
            _disabled_operation()
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail["error"], "olt_integration_disabled")
        self.assertIn("No network connection was attempted", raised.exception.detail["message"])

    def test_no_active_poller_or_protocol_dependency_is_added(self):
        root = Path(__file__).parents[1]
        self.assertFalse((root / "app" / "scripts" / "olt_poll_worker.py").exists())
        self.assertFalse(any(path.name.startswith("radiusfiber-olt") for path in root.rglob("*.service")))
        requirements = (root / "requirements.txt").read_text().lower()
        for dependency in ("paramiko", "netmiko", "pysnmp", "ncclient", "netconf", "telnetlib3"):
            self.assertNotIn(dependency, requirements)

    def test_migration_identity_and_boundary(self):
        migration = (Path(__file__).parents[1] / "alembic" / "versions" / "0014_olt_inventory_foundation.py").read_text()
        self.assertIn('revision = "0014_olt_inventory_foundation"', migration)
        self.assertIn('down_revision = "0013_auth_token_revocations"', migration)
        for forbidden_table in ("olt_optical_snapshots", "olt_alarms", "olt_vlan_bindings"):
            self.assertNotIn(f'op.create_table(\n        "{forbidden_table}"', migration)
        for protected_table in ("radacct", "radcheck", "radreply", "customers", "users", "payment_transactions"):
            self.assertNotIn(f'op.alter_column("{protected_table}"', migration)


if __name__ == "__main__":
    unittest.main()
