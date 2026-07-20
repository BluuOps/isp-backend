from __future__ import annotations

from app import app
from app.database import Base
from app.core.config import settings
from app.models import CustomerPortalAccount, PaymentTransaction, PaymentWebhookEvent, RadAcct, RadCheck, RadReply, ServicePlan, SupportTicket, TicketMessage, User
from app.schemas import (
    CustomerAuthLoginRequest,
    CustomerAuthResponse,
    CustomerTenantResponse,
    PaymentCreate,
    PaymentResponse,
    RadiusDisconnectRequest,
    RadiusSessionResponse,
    ServicePlanCreate,
    UserCreate,
)


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
        ("POST", "/customer-auth/login"),
        ("GET", "/customer-auth/tenant"),
        ("GET", "/customer-auth/me"),
        ("POST", "/customer-auth/logout"),
        ("POST", "/customer-auth/change-password"),
        ("GET", "/customer-portal/dashboard"),
        ("GET", "/customer-portal/profile"),
        ("PUT", "/customer-portal/profile"),
        ("GET", "/customer-portal/services"),
        ("GET", "/customer-portal/services/{service_id}"),
        ("GET", "/customer-portal/subscription"),
        ("GET", "/customer-portal/payments"),
        ("POST", "/customer-portal/payments/initialize"),
        ("GET", "/customer-portal/payments/{payment_id}"),
        ("GET", "/customer-portal/payments/{payment_id}/status"),
        ("POST", "/customer-portal/payments/{payment_id}/verify"),
        ("GET", "/customer-portal/tickets"),
        ("POST", "/customer-portal/tickets"),
        ("GET", "/customer-portal/tickets/{ticket_id}"),
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
        ("POST", "/webhooks/payments/paystack"),
        ("POST", "/webhooks/payments/paystack/{integration_key}"),
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
        "payment_webhook_events",
        "customer_portal_accounts",
        "support_tickets",
        "ticket_messages",
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
    if not settings.redis_url.startswith(("redis://", "rediss://")):
        raise RuntimeError("REDIS_URL must start with redis:// or rediss://")
    if not settings.celery_broker_url.startswith(("redis://", "rediss://")):
        raise RuntimeError("CELERY_BROKER_URL must start with redis:// or rediss://")

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
        PaymentWebhookEvent.__name__,
        CustomerPortalAccount.__name__,
        SupportTicket.__name__,
        TicketMessage.__name__,
    )
    print(
        "Schemas:",
        UserCreate.__name__,
        ServicePlanCreate.__name__,
        RadiusSessionResponse.__name__,
        RadiusDisconnectRequest.__name__,
        PaymentCreate.__name__,
        PaymentResponse.__name__,
        CustomerAuthLoginRequest.__name__,
        CustomerAuthResponse.__name__,
        CustomerTenantResponse.__name__,
    )


if __name__ == "__main__":
    main()
