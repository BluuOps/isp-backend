from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from app.core.config import settings
from app.database import SessionLocal
from app.models import Customer, CustomerPortalAccount, FeatureFlag, Organization, ServicePlan, User
from app.services.security import hash_password


SYNTHETIC_SLUG = "smart-fiber-staging-acceptance"
SYNTHETIC_MARKER = "synthetic_staging"
SYNTHETIC_WEBSITE = "https://synthetic-staging.invalid/smartfiber-acceptance"
CUSTOMER_IDS = (
    "M41_SF_ACCEPT_ACTIVE",
    "M41_SF_ACCEPT_EXPIRED",
    "M41_SF_ACCEPT_SUSPENDED",
    "M41_SF_ACCEPT_NO_SERVICE",
    "M41_SF_ACCEPT_MULTI_SERVICE",
)
PLAN_ROWS = (
    ("Smart Basic", "10M/5M", 1_395_000),
    ("Smart Flex", "15M/7M", 1_898_000),
    ("Smart Plus", "20M/10M", 2_292_400),
    ("Smart Premium", "25M/12M", 2_690_000),
    ("Smart Silver", "30M/15M", 3_098_500),
)


def require_isolated_environment() -> Path:
    expected_database = os.getenv("SYNTHETIC_ACCEPTANCE_DATABASE", "")
    actual_database = urlsplit(settings.database_url).path.lstrip("/")
    if os.getenv("ALLOW_SYNTHETIC_ACCEPTANCE_SEED", "").lower() not in {"1", "true", "yes"}:
        raise RuntimeError("Synthetic acceptance seeding is not explicitly enabled")
    if not expected_database or expected_database != actual_database:
        raise RuntimeError("Synthetic acceptance database identity is not exact")
    if not actual_database.startswith("isp_db_customer_payments_runtime_"):
        raise RuntimeError("Synthetic acceptance database name is not isolated")
    if settings.paystack_mode != "test" or not str(settings.paystack_secret_key or "").startswith("sk_test_"):
        raise RuntimeError("Paystack test mode is not positively confirmed")
    if "smartfiber=smart-fiber-staging-acceptance" not in settings.tenant_host_aliases:
        raise RuntimeError("Synthetic Smart Fiber hostname alias is not configured")
    callback_entry = (
        "smart-fiber-staging-acceptance=http://smartfiber.localhost:5182"
    )
    if callback_entry not in settings.paystack_callback_base_urls:
        raise RuntimeError("Synthetic Smart Fiber callback is not configured")
    credential_path = Path(os.environ["SYNTHETIC_ACCEPTANCE_CREDENTIAL_FILE"])
    allowed_parent = Path("/home/bluops/validation").resolve()
    if credential_path.parent.resolve() != allowed_parent or credential_path.is_symlink():
        raise RuntimeError("Synthetic credential path is outside the validation directory")
    return credential_path


def upsert_plan(
    db,
    organization_id: int,
    *,
    name: str,
    rate_limit: str,
    price_minor: int,
    visible: bool = True,
    status: str = "active",
) -> ServicePlan:
    plan = (
        db.query(ServicePlan)
        .filter(ServicePlan.organization_id == organization_id, ServicePlan.name == name)
        .first()
    )
    if not plan:
        plan = ServicePlan(organization_id=organization_id, name=name)
        db.add(plan)
    plan.rate_limit = rate_limit
    plan.price = f"{price_minor / 100:.2f}"
    plan.price_minor = price_minor
    plan.currency = "NGN"
    plan.duration_days = 30
    plan.billing_interval = "monthly"
    plan.customer_visible = visible
    plan.description = f"[{SYNTHETIC_MARKER}] Synthetic Smart Fiber acceptance plan"
    plan.status = status
    db.flush()
    return plan


def upsert_customer(
    db,
    organization: Organization,
    *,
    customer_id: str,
    index: int,
    account_status: str,
) -> Customer:
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        customer = Customer(id=customer_id)
        db.add(customer)
    customer.organization_id = organization.id
    customer.tenant_id = organization.slug
    customer.name = f"{customer_id} synthetic customer"
    customer.customer_type = "individual"
    customer.email = f"m41_sf_accept_{index}@example.com"
    customer.phone = f"+2348110000{index:03d}"
    customer.address = "Synthetic staging acceptance only"
    customer.latitude = 0
    customer.longitude = 0
    customer.onu_serial = ""
    customer.olt_name = ""
    customer.pon_port = ""
    customer.rx_signal = -20
    customer.tx_signal = 2
    customer.account_status = account_status
    customer.online = False
    db.flush()
    return customer


def upsert_service(
    db,
    organization: Organization,
    customer: Customer,
    *,
    suffix: str,
    plan: ServicePlan,
    status: str,
    expiration: datetime,
) -> User:
    username = f"M41_SF_ACCEPT_{suffix}"
    service = db.query(User).filter(User.username == username).first()
    if not service:
        service = User(username=username, password="M41_SYNTHETIC_NO_RADIUS_USE")
        db.add(service)
    service.organization_id = organization.id
    service.customer_id = customer.id
    service.service_plan = plan.name
    service.zone = "M41_SYNTHETIC_NO_NETWORK"
    service.status = status
    service.expiration_date = expiration
    db.flush()
    return service


def write_credentials(path: Path, passwords: dict[str, str]) -> None:
    lines = []
    for index, customer_id in enumerate(CUSTOMER_IDS, start=1):
        lines.extend(
            (
                f"CUSTOMER_{index}_SCENARIO={customer_id}\n",
                f"CUSTOMER_{index}_EMAIL=m41_sf_accept_{index}@example.com\n",
                f"CUSTOMER_{index}_PASSWORD={passwords[customer_id]}\n",
            )
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        destination.writelines(lines)


def main() -> None:
    credential_path = require_isolated_environment()
    db = SessionLocal()
    passwords: dict[str, str] = {}
    created_accounts = False
    try:
        real_organization = db.query(Organization).filter(Organization.slug == "smart-fiber").first()
        if not real_organization:
            raise RuntimeError("Restored Smart Fiber collision reference is unexpectedly absent")

        organization = db.query(Organization).filter(Organization.slug == SYNTHETIC_SLUG).first()
        if organization and organization.website != SYNTHETIC_WEBSITE:
            raise RuntimeError("Synthetic organization slug is owned by an unexpected record")
        if not organization:
            organization = Organization(platform_id=real_organization.platform_id, slug=SYNTHETIC_SLUG)
            db.add(organization)
        organization.name = "Smart Fiber"
        organization.status = "active"
        organization.company_email = "synthetic-support@invalid.example"
        organization.company_phone = "+2348000000000"
        organization.website = SYNTHETIC_WEBSITE
        organization.country = "NG"
        organization.timezone = "Africa/Lagos"
        organization.currency = "NGN"
        organization.logo = "/branding/tenants/smartfiber-logo.png"
        organization.subscription_plan = "synthetic-acceptance"
        organization.subscription_status = "active"
        db.flush()
        if organization.id == real_organization.id:
            raise RuntimeError("Synthetic organization collided with restored Smart Fiber")

        plans = {
            name: upsert_plan(
                db,
                organization.id,
                name=name,
                rate_limit=rate_limit,
                price_minor=price_minor,
            )
            for name, rate_limit, price_minor in PLAN_ROWS
        }
        upsert_plan(
            db,
            organization.id,
            name="Smart Internal Acceptance",
            rate_limit="1M/1M",
            price_minor=100,
            visible=False,
        )
        upsert_plan(
            db,
            organization.id,
            name="Smart Archived Acceptance",
            rate_limit="1M/1M",
            price_minor=100,
            visible=True,
            status="disabled",
        )
        flag = (
            db.query(FeatureFlag)
            .filter(
                FeatureFlag.organization_id == organization.id,
                FeatureFlag.key == "payment_gateway",
            )
            .first()
        )
        if not flag:
            flag = FeatureFlag(organization_id=organization.id, key="payment_gateway")
            db.add(flag)
        flag.enabled = True
        flag.configuration = {
            "provider": "paystack",
            "mode": "test",
            "currency": "NGN",
            "external_provisioning": False,
            "environment": SYNTHETIC_MARKER,
        }

        existing_account_count = (
            db.query(CustomerPortalAccount)
            .filter(CustomerPortalAccount.customer_id.in_(CUSTOMER_IDS))
            .count()
        )
        if existing_account_count not in {0, len(CUSTOMER_IDS)}:
            raise RuntimeError("Synthetic account seed is in a partial state")
        if existing_account_count == len(CUSTOMER_IDS) and not credential_path.is_file():
            raise RuntimeError("Synthetic accounts exist but their protected credential file is absent")
        created_accounts = existing_account_count == 0
        if created_accounts and credential_path.exists():
            raise RuntimeError("Synthetic credential file already exists unexpectedly")

        now = datetime.now(timezone.utc)
        customer_rows = (
            ("M41_SF_ACCEPT_ACTIVE", "active"),
            ("M41_SF_ACCEPT_EXPIRED", "active"),
            ("M41_SF_ACCEPT_SUSPENDED", "suspended"),
            ("M41_SF_ACCEPT_NO_SERVICE", "active"),
            ("M41_SF_ACCEPT_MULTI_SERVICE", "active"),
        )
        customers = {
            customer_id: upsert_customer(
                db,
                organization,
                customer_id=customer_id,
                index=index,
                account_status=account_status,
            )
            for index, (customer_id, account_status) in enumerate(customer_rows, start=1)
        }
        upsert_service(
            db,
            organization,
            customers["M41_SF_ACCEPT_ACTIVE"],
            suffix="ACTIVE_1",
            plan=plans["Smart Basic"],
            status="active",
            expiration=now + timedelta(days=12),
        )
        upsert_service(
            db,
            organization,
            customers["M41_SF_ACCEPT_EXPIRED"],
            suffix="EXPIRED_1",
            plan=plans["Smart Flex"],
            status="expired",
            expiration=now - timedelta(days=2),
        )
        upsert_service(
            db,
            organization,
            customers["M41_SF_ACCEPT_SUSPENDED"],
            suffix="SUSPENDED_1",
            plan=plans["Smart Plus"],
            status="suspended",
            expiration=now + timedelta(days=4),
        )
        upsert_service(
            db,
            organization,
            customers["M41_SF_ACCEPT_MULTI_SERVICE"],
            suffix="MULTI_1",
            plan=plans["Smart Basic"],
            status="active",
            expiration=now + timedelta(days=7),
        )
        upsert_service(
            db,
            organization,
            customers["M41_SF_ACCEPT_MULTI_SERVICE"],
            suffix="MULTI_2",
            plan=plans["Smart Premium"],
            status="active",
            expiration=now + timedelta(days=9),
        )

        for index, customer_id in enumerate(CUSTOMER_IDS, start=1):
            account = (
                db.query(CustomerPortalAccount)
                .filter(
                    CustomerPortalAccount.organization_id == organization.id,
                    CustomerPortalAccount.customer_id == customer_id,
                )
                .first()
            )
            if not account:
                password = secrets.token_urlsafe(18)
                passwords[customer_id] = password
                customer = customers[customer_id]
                account = CustomerPortalAccount(
                    organization_id=organization.id,
                    customer_id=customer.id,
                    email=customer.email,
                    phone=customer.phone,
                    password_hash=hash_password(password),
                )
                db.add(account)
            account.status = "active"
            account.failed_login_count = 0
            account.locked_until = None
        db.commit()
        if created_accounts:
            write_credentials(credential_path, passwords)

        print(f"SYNTHETIC_ORGANIZATION_ID={organization.id}")
        print(f"SYNTHETIC_ORGANIZATION_SLUG={organization.slug}")
        print(f"REAL_SMART_FIBER_ORGANIZATION_ID={real_organization.id}")
        print("SYNTHETIC_PLAN_COUNT=5")
        print("SYNTHETIC_HIDDEN_PLAN_COUNT=2")
        print("SYNTHETIC_CUSTOMER_COUNT=5")
        print("SYNTHETIC_SERVICE_COUNT=5")
        print("CREDENTIAL_VALUES_PRINTED=no")
        print("SYNTHETIC_SMARTFIBER_SEED=passed")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
