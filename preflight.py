from __future__ import annotations

from app import app
from app.database import Base
from app.models import PaymentTransaction, RadAcct, RadCheck, RadReply, ServicePlan, User
from app.schemas import PaymentCreate, PaymentResponse, RadiusDisconnectRequest, RadiusSessionResponse, ServicePlanCreate, UserCreate


def main() -> None:
    route_inventory = {
        (method, route.path)
        for route in app.routes
        for method in (route.methods or set())
    }
    required_routes = {
        ("GET", "/health"),
        ("GET", "/health/ready"),
        ("GET", "/release"),
        ("POST", "/auth/login"),
        ("GET", "/auth/me"),
        ("POST", "/auth/logout"),
        ("GET", "/platform/dashboard"),
        ("GET", "/platform/organizations"),
        ("POST", "/platform/organizations"),
        ("GET", "/platform/organizations/{organization_id}"),
        ("PUT", "/platform/organizations/{organization_id}"),
        ("DELETE", "/platform/organizations/{organization_id}"),
        ("GET", "/platform/subscriptions"),
        ("PUT", "/platform/subscriptions/{subscription_id}"),
        ("GET", "/platform/health"),
        ("GET", "/platform/feature-flags"),
        ("PUT", "/platform/feature-flags/{flag_id}"),
        ("GET", "/organization/profile"),
        ("PUT", "/organization/profile"),
        ("GET", "/organization/settings"),
        ("PUT", "/organization/settings"),
        ("GET", "/organization/staff"),
        ("POST", "/organization/staff"),
        ("PUT", "/organization/staff/{staff_id}"),
        ("DELETE", "/organization/staff/{staff_id}"),
        ("GET", "/organization/subscription"),
        ("GET", "/organization/feature-flags"),
        ("GET", "/organization/audit-logs"),
        ("GET", "/payments"),
        ("GET", "/payments/summary"),
        ("GET", "/payments/export"),
        ("GET", "/payments/{payment_id}"),
        ("POST", "/payments"),
        ("GET", "/users/"),
        ("POST", "/users/"),
        ("PUT", "/users/{user_id}"),
        ("DELETE", "/users/{user_id}"),
        ("PUT", "/users/{user_id}/activate"),
        ("PUT", "/users/{user_id}/suspend"),
        ("PUT", "/users/{user_id}/pending"),
        ("PUT", "/users/{user_id}/terminate"),
        ("PUT", "/users/{user_id}/plan"),
        ("POST", "/users/{user_id}/recharge"),
        ("GET", "/customers"),
        ("POST", "/customers"),
        ("GET", "/customers/{customer_id}"),
        ("PUT", "/customers/{customer_id}"),
        ("DELETE", "/customers/{customer_id}"),
        ("GET", "/plans"),
        ("POST", "/plans"),
        ("PUT", "/plans/{plan_id}"),
        ("DELETE", "/plans/{plan_id}"),
        ("GET", "/billing/accounts"),
        ("POST", "/billing/accounts"),
        ("GET", "/billing/users/{user_id}"),
        ("PUT", "/billing/users/{user_id}"),
        ("GET", "/radius/sessions"),
        ("POST", "/radius/disconnect"),
    }
    missing_routes = required_routes.difference(route_inventory)
    if missing_routes:
        formatted = ", ".join(f"{method} {path}" for method, path in sorted(missing_routes))
        raise RuntimeError(f"Missing expected routes: {formatted}")

    application_routes = {
        item
        for item in route_inventory
        if item[1] not in {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}
    }
    unexpected_routes = application_routes.difference(required_routes)
    if unexpected_routes:
        formatted = ", ".join(f"{method} {path}" for method, path in sorted(unexpected_routes))
        raise RuntimeError(f"Unexpected application routes: {formatted}")

    expected_tables = {
        "billing_accounts",
        "audit_logs",
        "customers",
        "feature_flags",
        "organizations",
        "organization_staff",
        "organization_roles",
        "organization_billing_profiles",
        "notification_settings",
        "payment_transactions",
        "platform",
        "radacct",
        "radcheck",
        "radreply",
        "service_plans",
        "subscriptions",
        "roles",
        "zones",
        "users",
    }
    metadata_tables = set(Base.metadata.tables)
    missing_tables = expected_tables.difference(metadata_tables)
    if missing_tables:
        raise RuntimeError(f"Missing expected metadata tables: {', '.join(sorted(missing_tables))}")

    print("Preflight OK")
    print("Routes:", len(app.routes))
    print("Tables:", ", ".join(sorted(expected_tables)))
    print(
        "Models:",
        User.__name__,
        ServicePlan.__name__,
        RadCheck.__name__,
        RadReply.__name__,
        RadAcct.__name__,
        PaymentTransaction.__name__,
    )
    print(
        "Schemas:",
        UserCreate.__name__,
        ServicePlanCreate.__name__,
        RadiusSessionResponse.__name__,
        RadiusDisconnectRequest.__name__,
        PaymentCreate.__name__,
        PaymentResponse.__name__,
    )


if __name__ == "__main__":
    main()
