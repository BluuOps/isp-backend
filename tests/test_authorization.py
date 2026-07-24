from __future__ import annotations

import time
import unittest
import secrets
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.core import authorization
from app import database
from app.core.authorization import (
    ALL_ORGANIZATION_PERMISSIONS,
    AuthenticatedPrincipal,
    Permission,
    PrincipalType,
    create_access_token,
    decode_access_token,
    get_authenticated_principal,
    require_permission,
    role_permissions,
)
from app.main import app
from app.models.organization import Organization
from app.models.organization_staff import OrganizationStaff
from app.models.customer import Customer
from app.services.security import (
    generate_temporary_password,
    hash_password,
    verify_password,
)


SECRET = secrets.token_hex(32)
NOW = 2_000_000_000
_ISOLATION_PATCHERS = []


def _blocked_effect(*args, **kwargs):
    raise AssertionError("A real database or external adapter was invoked")


def setUpModule():
    global _ISOLATION_PATCHERS
    _ISOLATION_PATCHERS = [
        patch.object(database.engine, "connect", side_effect=_blocked_effect),
        patch("subprocess.run", side_effect=_blocked_effect),
        patch("socket.create_connection", side_effect=_blocked_effect),
        patch("urllib.request.urlopen", side_effect=_blocked_effect),
    ]
    for patcher in _ISOLATION_PATCHERS:
        patcher.start()


def tearDownModule():
    for patcher in reversed(_ISOLATION_PATCHERS):
        patcher.stop()


def valid_claims(**overrides):
    claims = {
        "sub": "staff:1001",
        "principal_type": PrincipalType.ORGANIZATION_STAFF.value,
        "token_type": "access",
        "staff_id": 1001,
        "organization_id": 101,
        "organization_slug": "tenant-alpha",
        "role": "Organization Admin",
        "auth_method": "password",
        "iat": NOW - 10,
        "exp": NOW + 300,
        "iss": authorization.settings.auth_token_issuer,
        "aud": authorization.settings.auth_token_audience,
        "jti": "M39_AUTHORIZATION_TEST_JTI",
    }
    claims.update(overrides)
    return claims


def organization_principal(role="Organization Admin"):
    return AuthenticatedPrincipal(
        subject_id="staff:1001",
        principal_type=PrincipalType.ORGANIZATION_STAFF,
        authentication_method="password",
        active=True,
        platform_authority=False,
        organization_id=101,
        organization_slug="tenant-alpha",
        organization_role=role,
        effective_permissions=role_permissions(role),
        actor_label="tenant-alpha-admin@example.invalid",
    )


class FakeQuery:
    def __init__(self, row):
        self.row = row

    def filter(self, *args):
        for expression in args:
            if self.row is None:
                break
            field = getattr(getattr(expression, "left", None), "name", None)
            value = getattr(getattr(expression, "right", None), "value", object())
            if field and getattr(self.row, field, object()) != value:
                self.row = None
        return self

    def first(self):
        return self.row


class CapturingQuery(FakeQuery):
    def __init__(self, row):
        super().__init__(row)
        self.expressions = []

    def filter(self, *args):
        self.expressions.extend(args)
        return self


class FakeDB:
    def __init__(self, staff, organization):
        self.staff = staff
        self.organization = organization

    def query(self, model):
        if model is OrganizationStaff:
            return FakeQuery(self.staff)
        if model is Organization:
            return FakeQuery(self.organization)
        raise AssertionError(f"Unexpected model: {model}")


def dependency_calls(node):
    calls = []
    for dependency in getattr(node, "dependencies", []):
        call = getattr(dependency, "call", None)
        if call is not None:
            calls.append(call)
        calls.extend(dependency_calls(dependency))
    return calls


def route_for(method, path):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in (
            getattr(route, "methods", set()) or set()
        ):
            return route
    raise AssertionError(f"Route not registered: {method} {path}")


class TokenValidationTests(unittest.TestCase):
    def test_missing_credentials_are_rejected(self):
        test_settings = replace(authorization.settings, jwt_secret=SECRET)
        with patch.object(authorization, "settings", test_settings):
            with self.assertRaises(HTTPException) as raised:
                get_authenticated_principal(None, FakeDB(None, None))
        self.assertEqual(raised.exception.status_code, 401)

    def test_malformed_credentials_are_rejected(self):
        test_settings = replace(authorization.settings, jwt_secret=SECRET)
        with patch.object(authorization, "settings", test_settings):
            with self.assertRaises(HTTPException) as raised:
                get_authenticated_principal("Basic invalid", FakeDB(None, None))
        self.assertEqual(raised.exception.status_code, 401)

    def test_valid_typed_token_round_trip(self):
        claims = valid_claims()
        decoded = decode_access_token(
            create_access_token(claims, SECRET),
            SECRET,
            now=NOW,
        )
        self.assertEqual(decoded["principal_type"], "organization_staff")
        self.assertEqual(decoded["organization_id"], 101)
        self.assertEqual(decoded["organization_slug"], "tenant-alpha")

    def test_tampered_token_is_rejected(self):
        token = create_access_token(valid_claims(), SECRET)
        with self.assertRaises(HTTPException) as raised:
            decode_access_token(token + "tampered", SECRET, now=NOW)
        self.assertEqual(raised.exception.status_code, 401)

    def test_malformed_base64_token_is_rejected(self):
        token = "%%%." + authorization._sign("%%%", SECRET)
        with self.assertRaises(HTTPException) as raised:
            decode_access_token(token, SECRET, now=NOW)
        self.assertEqual(raised.exception.status_code, 401)

    def test_expired_token_is_rejected(self):
        token = create_access_token(valid_claims(exp=NOW), SECRET)
        with self.assertRaises(HTTPException) as raised:
            decode_access_token(token, SECRET, now=NOW)
        self.assertEqual(raised.exception.status_code, 401)

    def test_wrong_principal_type_is_rejected(self):
        token = create_access_token(
            valid_claims(principal_type=PrincipalType.CUSTOMER.value),
            SECRET,
        )
        with self.assertRaises(HTTPException) as raised:
            decode_access_token(token, SECRET, now=NOW)
        self.assertEqual(raised.exception.status_code, 401)

    def test_wrong_audience_is_rejected(self):
        token = create_access_token(valid_claims(aud="wrong-audience"), SECRET)
        with self.assertRaises(HTTPException) as raised:
            decode_access_token(token, SECRET, now=NOW)
        self.assertEqual(raised.exception.status_code, 401)

    def test_incomplete_claims_are_rejected(self):
        claims = valid_claims()
        claims.pop("organization_id")
        token = create_access_token(claims, SECRET)
        with self.assertRaises(HTTPException) as raised:
            decode_access_token(token, SECRET, now=NOW)
        self.assertEqual(raised.exception.status_code, 401)


class MembershipValidationTests(unittest.TestCase):
    def setUp(self):
        self.staff = SimpleNamespace(
            id=1001,
            organization_id=101,
            email="tenant-alpha-admin@example.invalid",
            role="Organization Admin",
            status="active",
        )
        self.organization = SimpleNamespace(
            id=101,
            slug="tenant-alpha",
            status="active",
        )

    def authenticate(self, claims=None, staff=None, organization=None):
        token = create_access_token(claims or valid_claims(), SECRET)
        db = FakeDB(
            self.staff if staff is None else staff,
            self.organization if organization is None else organization,
        )
        test_settings = replace(authorization.settings, jwt_secret=SECRET)
        with patch.object(authorization, "settings", test_settings):
            with patch.object(time, "time", return_value=NOW):
                return get_authenticated_principal(f"Bearer {token}", db)

    def test_active_membership_builds_server_authoritative_principal(self):
        principal = self.authenticate()
        self.assertEqual(principal.principal_type, PrincipalType.ORGANIZATION_STAFF)
        self.assertEqual(principal.organization_id, 101)
        self.assertEqual(
            principal.effective_permissions,
            ALL_ORGANIZATION_PERMISSIONS,
        )

    def test_removed_membership_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            self.authenticate(staff=False)
        self.assertEqual(raised.exception.status_code, 401)

    def test_disabled_membership_is_rejected(self):
        self.staff.status = "disabled"
        with self.assertRaises(HTTPException) as raised:
            self.authenticate()
        self.assertEqual(raised.exception.status_code, 401)

    def test_inactive_organization_is_rejected(self):
        self.organization.status = "suspended"
        with self.assertRaises(HTTPException) as raised:
            self.authenticate()
        self.assertEqual(raised.exception.status_code, 403)

    def test_unknown_organization_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            self.authenticate(organization=False)
        self.assertEqual(raised.exception.status_code, 403)

    def test_cross_organization_subject_is_rejected(self):
        claims = valid_claims(sub="staff:2001")
        with self.assertRaises(HTTPException) as raised:
            self.authenticate(claims=claims)
        self.assertEqual(raised.exception.status_code, 401)

    def test_tenant_beta_claim_cannot_use_tenant_alpha_membership(self):
        claims = valid_claims(
            organization_id=202,
            organization_slug="tenant-beta",
        )
        with self.assertRaises(HTTPException) as raised:
            self.authenticate(claims=claims)
        self.assertEqual(raised.exception.status_code, 401)


class PermissionTests(unittest.TestCase):
    def test_organization_admin_has_all_permissions(self):
        self.assertEqual(
            role_permissions("Organization Admin"),
            ALL_ORGANIZATION_PERMISSIONS,
        )

    def test_unknown_and_customer_roles_fail_closed(self):
        self.assertEqual(role_permissions("Unknown"), frozenset())
        self.assertEqual(role_permissions("Customer"), frozenset())

    def test_read_only_cannot_mutate(self):
        principal = organization_principal("Read Only")
        self.assertTrue(principal.has_permission(Permission.CUSTOMERS_READ))
        self.assertFalse(principal.has_permission(Permission.CUSTOMERS_UPDATE))
        self.assertFalse(principal.has_permission(Permission.PAYMENTS_CREATE))

    def test_permission_dependency_allows_and_denies(self):
        dependency = require_permission(Permission.CUSTOMERS_UPDATE)
        allowed = organization_principal("Organization Admin")
        denied = organization_principal("Read Only")
        self.assertIs(dependency(allowed), allowed)
        with self.assertRaises(HTTPException) as raised:
            dependency(denied)
        self.assertEqual(raised.exception.status_code, 403)

    def test_customer_principal_has_no_operational_access(self):
        customer = AuthenticatedPrincipal(
            subject_id="customer:tenant-alpha-customer",
            principal_type=PrincipalType.CUSTOMER,
            authentication_method="password",
            active=True,
            platform_authority=False,
            organization_id=101,
            organization_slug="tenant-alpha",
            organization_role=None,
            effective_permissions=frozenset(),
            customer_id="tenant-alpha-customer",
        )
        for permission in ALL_ORGANIZATION_PERMISSIONS:
            with self.subTest(permission=permission):
                self.assertFalse(customer.has_permission(permission))


class TenantScopeHelperTests(unittest.TestCase):
    def test_scope_query_adds_authoritative_organization_filter(self):
        query = CapturingQuery(None)
        returned = authorization.scope_query_to_tenant(query, Customer, 101)
        self.assertIs(returned, query)
        self.assertEqual(len(query.expressions), 1)
        expression = query.expressions[0]
        self.assertEqual(expression.left.name, "organization_id")
        self.assertEqual(expression.right.value, 101)

    def test_tenant_object_loader_returns_404_without_cross_tenant_row(self):
        query = CapturingQuery(None)

        class LoaderDB:
            def query(self, model):
                self.model = model
                return query

        with self.assertRaises(HTTPException) as raised:
            authorization.load_tenant_object_or_404(
                LoaderDB(),
                Customer,
                "tenant-beta-customer",
                101,
            )
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(
            {expression.left.name for expression in query.expressions},
            {"id", "organization_id"},
        )


class PasswordTests(unittest.TestCase):
    def test_password_hash_verification(self):
        password = generate_temporary_password()
        encoded = hash_password(password)
        self.assertNotIn(password, encoded)
        self.assertTrue(verify_password(password, encoded))
        self.assertFalse(verify_password(password + "x", encoded))
        self.assertFalse(verify_password(password, "pbkdf2_sha256$310000$%%%$%%%"))


class RouteGuardRegressionTests(unittest.TestCase):
    EXPECTED_GUARDS = {
        ("GET", "/customers"): Permission.CUSTOMERS_READ,
        ("POST", "/customers"): Permission.CUSTOMERS_CREATE,
        ("GET", "/customers/{customer_id}"): Permission.CUSTOMERS_READ,
        ("PUT", "/customers/{customer_id}"): Permission.CUSTOMERS_UPDATE,
        ("DELETE", "/customers/{customer_id}"): Permission.CUSTOMERS_DELETE,
        ("GET", "/users/"): Permission.SUBSCRIBERS_READ,
        ("POST", "/users/"): Permission.SUBSCRIBERS_CREATE,
        ("PUT", "/users/{user_id}"): Permission.SUBSCRIBERS_UPDATE,
        ("POST", "/users/{user_id}/recharge"): Permission.SUBSCRIBERS_RECHARGE,
        ("PUT", "/users/{user_id}/suspend"): Permission.SUBSCRIBERS_SUSPEND,
        ("PUT", "/users/{user_id}/activate"): Permission.SUBSCRIBERS_RECONNECT,
        ("PUT", "/users/{user_id}/pending"): Permission.SUBSCRIBERS_UPDATE,
        ("PUT", "/users/{user_id}/terminate"): Permission.SUBSCRIBERS_DELETE,
        ("PUT", "/users/{user_id}/plan"): Permission.SUBSCRIBERS_PLAN_CHANGE,
        ("DELETE", "/users/{user_id}"): Permission.SUBSCRIBERS_DELETE,
        ("GET", "/plans"): Permission.PLANS_READ,
        ("POST", "/plans"): Permission.PLANS_CREATE,
        ("PUT", "/plans/{plan_id}"): Permission.PLANS_UPDATE,
        ("DELETE", "/plans/{plan_id}"): Permission.PLANS_DELETE,
        ("GET", "/billing/accounts"): Permission.BILLING_ACCOUNTS_READ,
        ("POST", "/billing/accounts"): Permission.BILLING_ACCOUNTS_CREATE,
        ("GET", "/billing/users/{user_id}"): Permission.BILLING_ACCOUNTS_READ,
        ("PUT", "/billing/users/{user_id}"): Permission.BILLING_ACCOUNTS_UPDATE,
        ("GET", "/payments"): Permission.PAYMENTS_READ,
        ("GET", "/payments/summary"): Permission.PAYMENTS_READ,
        ("GET", "/payments/export"): Permission.PAYMENTS_EXPORT,
        ("GET", "/payments/{payment_id}"): Permission.PAYMENTS_READ,
        ("POST", "/payments"): Permission.PAYMENTS_CREATE,
        ("GET", "/radius/sessions"): Permission.RADIUS_SESSIONS_READ,
        ("POST", "/radius/disconnect"): Permission.RADIUS_SESSIONS_DISCONNECT,
        ("GET", "/organization/profile"): Permission.ORGANIZATION_PROFILE_READ,
        ("PUT", "/organization/profile"): Permission.ORGANIZATION_PROFILE_UPDATE,
        ("GET", "/organization/settings"): Permission.ORGANIZATION_SETTINGS_READ,
        ("PUT", "/organization/settings"): Permission.ORGANIZATION_SETTINGS_UPDATE,
        ("GET", "/organization/staff"): Permission.ORGANIZATION_STAFF_READ,
        ("POST", "/organization/staff"): Permission.ORGANIZATION_STAFF_MANAGE,
        ("PUT", "/organization/staff/{staff_id}"): Permission.ORGANIZATION_STAFF_MANAGE,
        ("DELETE", "/organization/staff/{staff_id}"): Permission.ORGANIZATION_STAFF_MANAGE,
        ("GET", "/organization/subscription"): Permission.ORGANIZATION_SUBSCRIPTION_READ,
        ("GET", "/organization/feature-flags"): Permission.ORGANIZATION_FEATURE_FLAGS_READ,
        ("GET", "/organization/audit-logs"): Permission.ORGANIZATION_AUDIT_LOGS_READ,
    }

    def test_every_critical_route_has_expected_permission(self):
        for route_key, permission in self.EXPECTED_GUARDS.items():
            with self.subTest(route=route_key):
                route = route_for(*route_key)
                permissions = {
                    getattr(call, "required_permission", None)
                    for call in dependency_calls(route.dependant)
                }
                self.assertIn(permission, permissions)
                dependency_names = {
                    getattr(call, "__name__", "")
                    for call in dependency_calls(route.dependant)
                }
                self.assertIn("get_authenticated_principal", dependency_names)
                self.assertIn("get_organization_context", dependency_names)

    def test_mutation_authorization_is_registered_before_handler_dependencies(self):
        for (method, path), permission in self.EXPECTED_GUARDS.items():
            if method == "GET":
                continue
            with self.subTest(method=method, path=path):
                route = route_for(method, path)
                first = route.dependant.dependencies[0]
                self.assertEqual(
                    getattr(getattr(first, "call", None), "required_permission", None),
                    permission,
                )

    def test_platform_routes_use_typed_platform_principal(self):
        for route in app.routes:
            if not getattr(route, "path", "").startswith("/platform"):
                continue
            names = {getattr(call, "__name__", "") for call in dependency_calls(route.dependant)}
            self.assertIn("require_platform_principal", names)


if __name__ == "__main__":
    unittest.main()
