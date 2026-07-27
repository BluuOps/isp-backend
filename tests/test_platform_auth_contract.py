import unittest

from fastapi import HTTPException

from app.core.config import settings
from app.routers import auth


class PlatformAuthContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = {
            "platform_admin_email": settings.platform_admin_email,
            "platform_admin_password": settings.platform_admin_password,
            "jwt_secret": settings.jwt_secret,
        }
        object.__setattr__(settings, "platform_admin_email", "platform@example.invalid")
        object.__setattr__(settings, "platform_admin_password", "validation-password")
        object.__setattr__(settings, "jwt_secret", "validation-secret")

    def tearDown(self) -> None:
        for name, value in self.original.items():
            object.__setattr__(settings, name, value)

    def test_dedicated_platform_routes_are_registered(self) -> None:
        routes = {(route.path, tuple(route.methods or ())) for route in auth.platform_auth_router.routes}
        self.assertIn(("/platform/auth/login", ("POST",)), routes)
        self.assertIn(("/platform/auth/me", ("GET",)), routes)
        self.assertIn(("/platform/auth/logout", ("POST",)), routes)

    def test_platform_auth_router_is_registered_on_application(self) -> None:
        from app.main import app

        paths = {route.path for route in app.routes}
        self.assertTrue(
            {"/platform/auth/login", "/platform/auth/me", "/platform/auth/logout"}.issubset(paths)
        )

    def test_platform_login_does_not_require_tenant_host_context(self) -> None:
        response = auth.platform_login(
            auth.LoginRequest(email="platform@example.invalid", password="validation-password")
        )
        self.assertEqual(response.user.principalType, "platform_admin")
        self.assertIsNone(response.user.organizationId)
        self.assertIsNone(response.user.organizationSlug)

    def test_platform_me_rejects_organization_principal(self) -> None:
        token = auth._create_token(
            {
                "sub": "staff:1",
                "principal_type": "organization_staff",
                "exp": 4_102_444_800,
            },
            "validation-secret",
        )
        with self.assertRaises(HTTPException) as raised:
            auth.platform_me(f"Bearer {token}")
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
