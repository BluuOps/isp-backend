from __future__ import annotations

import asyncio
import json
import time
import unittest
from contextlib import ExitStack, nullcontext
from dataclasses import replace
from unittest.mock import patch

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from app.core import authorization, platform_auth, principal
from app.database import get_db
from app.models import CustomerPortalAccount, Organization, OrganizationStaff
from app.models.auth_token_revocation import AuthTokenRevocation
from app.routers import auth, customer_auth, platform


SECRET = "route-test-secret-that-is-not-a-production-credential"
PLATFORM_KEY = "route-test-platform-key-that-is-not-a-production-credential"


class _Query:
    def __init__(self, row=None, *, count=0):
        self.row = row
        self.count_value = count

    def filter(self, *args):
        return self

    def first(self):
        return self.row

    def count(self):
        return self.count_value


class _RouteDB:
    def __init__(self, *, fail_revocation_lookup: bool = False):
        self.fail_revocation_lookup = fail_revocation_lookup
        self.revocations: list[AuthTokenRevocation] = []
        self.staff = OrganizationStaff(
            id=9,
            organization_id=20,
            name="Synthetic Organization Administrator",
            email="route-org-admin@synthetic.test",
            password_hash="not-used",
            role="Organization Admin",
            status="active",
            is_temporary_password=False,
        )
        self.organization = Organization(
            id=20,
            platform_id=1,
            name="Synthetic Tenant",
            slug="synthetic-tenant",
            status="active",
            country="NG",
            timezone="Africa/Lagos",
            currency="NGN",
            subscription_status="active",
        )
        self.customer_account = CustomerPortalAccount(
            id=11,
            organization_id=20,
            customer_id="RF_AUTH_ROUTE_CUSTOMER",
            email="route-customer@synthetic.test",
            phone="+2340000000000",
            password_hash="not-used",
            status="active",
        )

    def query(self, model, *args):
        if model is AuthTokenRevocation:
            if self.fail_revocation_lookup:
                raise SQLAlchemyError("synthetic denylist outage")
            return _Query(self.revocations[0] if self.revocations else None)
        if model is OrganizationStaff:
            return _Query(self.staff)
        if model is Organization:
            return _Query(self.organization, count=1)
        if model is CustomerPortalAccount:
            return _Query(self.customer_account)
        raise AssertionError(f"Unexpected route-test model: {model}")

    def scalars(self, statement):
        return []

    def execute(self, statement):
        raise AssertionError("No cleanup delete is expected in this route test")

    def begin_nested(self):
        return nullcontext()

    def add(self, row):
        if isinstance(row, AuthTokenRevocation):
            self.revocations.append(row)

    def flush(self):
        return None

    def commit(self):
        return None


async def _asgi_request(
    application: FastAPI,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    sent: list[dict] = []
    request_available = True

    async def receive():
        nonlocal request_available
        if request_available:
            request_available = False
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    encoded_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    await application(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": encoded_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
        send,
    )
    status_code = next(message["status"] for message in sent if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return status_code, json.loads(body or b"{}")


class TokenRevocationRouteTests(unittest.TestCase):
    def setUp(self):
        self.application = FastAPI()
        self.application.include_router(auth.router)
        self.application.include_router(customer_auth.router)
        self.application.include_router(platform.router)
        self.configured = replace(
            principal.settings,
            jwt_secret=SECRET,
            platform_admin_api_key=PLATFORM_KEY,
            platform_admin_email="platform@synthetic.test",
            auth_access_token_ttl_seconds=3600,
        )
        self.settings_patches = ExitStack()
        for module in (principal, authorization, platform_auth, auth):
            self.settings_patches.enter_context(
                patch.object(module, "settings", self.configured)
            )

    def tearDown(self):
        self.application.dependency_overrides.clear()
        self.settings_patches.close()

    def _request(self, method: str, path: str, token: str | None = None, **headers):
        request_headers = dict(headers)
        if token:
            request_headers["authorization"] = f"Bearer {token}"
        return asyncio.run(
            _asgi_request(
                self.application,
                method,
                path,
                headers=request_headers,
            )
        )

    def _token_for(self, principal_type: str) -> str:
        now = int(time.time())
        if principal_type == "platform_admin":
            return principal.create_principal_token(
                {
                    "sub": "platform-admin",
                    "principal_type": "platform_admin",
                    "email": "platform@synthetic.test",
                    "permissions": list(principal.PLATFORM_PERMISSIONS),
                }
            )
        if principal_type == "organization_staff":
            return authorization.create_access_token(
                {
                    "sub": "staff:9",
                    "principal_type": "organization_staff",
                    "token_type": "access",
                    "staff_id": 9,
                    "organization_id": 20,
                    "organization_slug": "synthetic-tenant",
                    "iat": now,
                    "exp": now + 3600,
                    "iss": self.configured.auth_token_issuer,
                    "aud": self.configured.auth_token_audience,
                    "jti": "route-organization-jti",
                },
                SECRET,
            )
        return principal.create_principal_token(
            {
                "sub": "11",
                "principal_type": "customer",
                "organization_id": 20,
                "organization_slug": "synthetic-tenant",
                "customer_id": "RF_AUTH_ROUTE_CUSTOMER",
            }
        )

    def test_logout_revokes_all_jwt_principals_and_repeated_logout_is_safe(self):
        routes = {
            "platform_admin": ("/auth/me", "/auth/logout"),
            "organization_staff": ("/auth/me", "/auth/logout"),
            "customer": ("/customer-auth/me", "/customer-auth/logout"),
        }
        for principal_type, (protected_path, logout_path) in routes.items():
            with self.subTest(principal_type=principal_type):
                db = _RouteDB()
                self.application.dependency_overrides[get_db] = lambda: db
                token = self._token_for(principal_type)

                self.assertEqual(self._request("GET", protected_path, token)[0], 200)
                self.assertEqual(self._request("POST", logout_path, token)[0], 200)
                self.assertEqual(self._request("GET", protected_path, token)[0], 401)
                self.assertEqual(self._request("POST", logout_path, token)[0], 200)
                self.assertEqual(self._request("GET", protected_path, token)[0], 401)
                self.assertEqual(len(db.revocations), 1)

    def test_route_level_denylist_failure_is_fail_closed(self):
        db = _RouteDB(fail_revocation_lookup=True)
        self.application.dependency_overrides[get_db] = lambda: db
        token = self._token_for("platform_admin")

        status_code, body = self._request("GET", "/auth/me", token)

        self.assertEqual(status_code, 503)
        self.assertEqual(body["detail"], "Authentication revocation state is unavailable")

    def test_platform_api_key_authentication_does_not_query_jwt_denylist(self):
        db = _RouteDB(fail_revocation_lookup=True)
        self.application.dependency_overrides[get_db] = lambda: db

        status_code, body = self._request(
            "GET",
            "/platform/health",
            **{"x-platform-admin-key": PLATFORM_KEY},
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(body, {"status": "ok", "organizations": 1})


if __name__ == "__main__":
    unittest.main()
