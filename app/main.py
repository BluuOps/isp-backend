import os
import shutil

from fastapi import FastAPI
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.errors import ApiError, api_error_handler, integrity_error_handler
from app.database import engine
from app.routers import auth, billing, customer_auth, customers, organization, payments, plans, platform, radius_sessions, release, users


app = FastAPI(
    title="ISP OSS/BSS API",
    description="Backend API for ISP subscriber and RADIUS provisioning workflows.",
    version="0.1.0",
)

app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(IntegrityError, integrity_error_handler)


app.include_router(users.router)
app.include_router(plans.router)
app.include_router(billing.router)
app.include_router(customers.router)
app.include_router(payments.router)
app.include_router(radius_sessions.router)
app.include_router(platform.router)
app.include_router(organization.router)
app.include_router(auth.router)
app.include_router(customer_auth.router)
app.include_router(release.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness() -> dict[str, object]:
    required_tables = {
        "platform", "organizations", "subscriptions", "audit_logs",
        "feature_flags", "roles", "customers", "users", "service_plans",
        "billing_accounts", "radacct", "organization_staff", "organization_roles",
        "zones", "organization_billing_profiles", "notification_settings",
        "payment_transactions", "customer_portal_accounts",
    }
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        tables = set(inspect(connection).get_table_names())
        migration_current = None
        if "alembic_version" in tables:
            migration_current = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()

    checks = {
        "database": True,
        "tables": not bool(required_tables - tables),
        "coa_secret": os.path.isfile(settings.coa_secret_path)
        and os.access(settings.coa_secret_path, os.R_OK),
        "radclient": os.path.isfile(settings.radclient_bin)
        and os.access(settings.radclient_bin, os.X_OK),
        "disk": shutil.disk_usage("/").free >= 512 * 1024 * 1024,
        "migration_status": migration_current == "0005_identity_audit_boundaries",
    }
    ready = all(value for value in checks.values() if isinstance(value, bool))
    return {"status": "ready" if ready else "not_ready", "checks": checks}
