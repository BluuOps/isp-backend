from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.models import (
    AuthTokenRevocation,
    Customer,
    CustomerPortalAccount,
    Organization,
    OrganizationStaff,
    ServicePlan,
    User,
    Zone,
)
from app.services.audit import record_audit
from app.services.security import hash_password


FIXTURE_PREFIX = "RF_UAT_AUTH_"
CUSTOMER_ID = f"{FIXTURE_PREFIX}CUSTOMER_001"
SERVICE_USERNAME = f"{FIXTURE_PREFIX}NO_RADIUS_001"
ADMIN_EMAIL = "uat-org-admin@smartfiber.test"
READ_ONLY_EMAIL = "uat-read-only@smartfiber.test"
CUSTOMER_EMAIL = "uat-customer@smartfiber.test"
CUSTOMER_PHONE = "+2340000000000"
MAX_LIFETIME = timedelta(days=7)


def _database_name() -> str:
    raw = os.environ.get("DATABASE_URL", "")
    name = urlparse(raw.replace("postgresql+psycopg2://", "postgresql://")).path.lstrip("/")
    if not name.startswith("isp_db_jwt_fixture_"):
        raise RuntimeError("Fixture harness is restricted to isp_db_jwt_fixture_* disposable databases")
    if os.environ.get("RADIUSFIBER_ALLOW_DISPOSABLE_FIXTURES") != "yes":
        raise RuntimeError("RADIUSFIBER_ALLOW_DISPOSABLE_FIXTURES=yes is required")
    return name


def _password(label: str) -> str:
    first = getpass.getpass(f"{label} password: ")
    second = getpass.getpass(f"Confirm {label} password: ")
    if len(first) < 12 or first != second:
        raise RuntimeError(f"{label} password must match and contain at least 12 characters")
    return first


def _fixture_rows(db, organization_id: int) -> dict[str, int]:
    return {
        "staff": db.query(OrganizationStaff).filter(
            OrganizationStaff.organization_id == organization_id,
            OrganizationStaff.email.in_((ADMIN_EMAIL, READ_ONLY_EMAIL)),
        ).count(),
        "customers": db.query(Customer).filter(
            Customer.organization_id == organization_id,
            Customer.id == CUSTOMER_ID,
        ).count(),
        "services": db.query(User).filter(
            User.organization_id == organization_id,
            User.username == SERVICE_USERNAME,
        ).count(),
        "portal_accounts": db.query(CustomerPortalAccount).filter(
            CustomerPortalAccount.organization_id == organization_id,
            CustomerPortalAccount.email == CUSTOMER_EMAIL,
        ).count(),
    }


def apply_fixtures(db, *, organization_slug: str, plan_id: int) -> None:
    organization = db.execute(
        select(Organization).where(Organization.slug == organization_slug)
    ).scalar_one_or_none()
    if not organization or organization.status != "active":
        raise RuntimeError("The selected synthetic tenant must exist and be active")
    plan = db.execute(
        select(ServicePlan).where(
            ServicePlan.id == plan_id,
            ServicePlan.organization_id == organization.id,
            ServicePlan.status == "active",
        )
    ).scalar_one_or_none()
    zone = db.execute(
        select(Zone).where(
            Zone.organization_id == organization.id,
            Zone.name == "Default",
            Zone.status == "active",
        )
    ).scalar_one_or_none()
    if not plan or not zone:
        raise RuntimeError("An active tenant-owned plan and Default zone are required")
    if any(_fixture_rows(db, organization.id).values()):
        raise RuntimeError("Fixture identities already exist; run cleanup before apply")

    expires_at = datetime.now(timezone.utc) + MAX_LIFETIME
    admin_password = _password("Organization Admin")
    read_only_password = _password("Read Only")
    customer_password = _password("Customer")
    staff = (
        OrganizationStaff(
            organization_id=organization.id,
            name="UAT Organization Admin",
            email=ADMIN_EMAIL,
            password_hash=hash_password(admin_password),
            role="Organization Admin",
            status="active",
            is_temporary_password=False,
        ),
        OrganizationStaff(
            organization_id=organization.id,
            name="UAT Read Only",
            email=READ_ONLY_EMAIL,
            password_hash=hash_password(read_only_password),
            role="Read Only",
            status="active",
            is_temporary_password=False,
        ),
    )
    db.add_all(staff)
    customer = Customer(
        id=CUSTOMER_ID,
        organization_id=organization.id,
        tenant_id=organization.slug,
        name="UAT Authentication Customer",
        customer_type="individual",
        email=CUSTOMER_EMAIL,
        phone=CUSTOMER_PHONE,
        address="Synthetic UAT fixture",
        latitude=0,
        longitude=0,
        account_status="active",
        online=False,
    )
    db.add(customer)
    db.flush()
    service = User(
        organization_id=organization.id,
        username=SERVICE_USERNAME,
        password=secrets.token_urlsafe(32),
        customer_id=CUSTOMER_ID,
        service_plan=plan.name,
        zone=zone.name,
        status="pending",
        expiration_date=None,
    )
    portal_account = CustomerPortalAccount(
        organization_id=organization.id,
        customer_id=CUSTOMER_ID,
        email=CUSTOMER_EMAIL,
        phone=CUSTOMER_PHONE,
        password_hash=hash_password(customer_password),
        status="active",
        failed_login_count=0,
    )
    db.add_all((service, portal_account))
    record_audit(
        db,
        organization_id=organization.id,
        actor="fixture-harness",
        actor_type="system",
        actor_id="auth-uat-fixture-harness",
        actor_label="Authentication UAT fixture harness",
        action="uat.auth_fixtures.created",
        target_type="uat_fixture_set",
        target_id=FIXTURE_PREFIX.rstrip("_"),
        new_value={
            "roles": ["Organization Admin", "Read Only", "Customer"],
            "service_network_state": "not_provisioned",
            "cleanup_due_at": expires_at.isoformat(),
        },
    )
    db.commit()
    print("FIXTURE_APPLY=passed")
    print("FIXTURE_CREDENTIAL_VALUES_PRINTED=no")
    print("RADIUS_ROWS_CREATED=no")


def cleanup_fixtures(db, *, organization_slug: str) -> None:
    organization = db.execute(
        select(Organization).where(Organization.slug == organization_slug)
    ).scalar_one_or_none()
    if not organization:
        raise RuntimeError("Fixture tenant does not exist")
    rows = _fixture_rows(db, organization.id)
    account_ids = list(
        db.scalars(
            select(CustomerPortalAccount.id).where(
                CustomerPortalAccount.organization_id == organization.id,
                CustomerPortalAccount.email == CUSTOMER_EMAIL,
            )
        )
    )
    staff_ids = list(
        db.scalars(
            select(OrganizationStaff.id).where(
                OrganizationStaff.organization_id == organization.id,
                OrganizationStaff.email.in_((ADMIN_EMAIL, READ_ONLY_EMAIL)),
            )
        )
    )
    subjects = [f"staff:{value}" for value in staff_ids] + [str(value) for value in account_ids]
    if subjects:
        db.execute(delete(AuthTokenRevocation).where(AuthTokenRevocation.subject_id.in_(subjects)))
    db.query(User).filter(
        User.organization_id == organization.id,
        User.username == SERVICE_USERNAME,
    ).delete(synchronize_session=False)
    db.query(CustomerPortalAccount).filter(
        CustomerPortalAccount.organization_id == organization.id,
        CustomerPortalAccount.email == CUSTOMER_EMAIL,
    ).delete(synchronize_session=False)
    db.query(Customer).filter(
        Customer.organization_id == organization.id,
        Customer.id == CUSTOMER_ID,
    ).delete(synchronize_session=False)
    db.query(OrganizationStaff).filter(
        OrganizationStaff.organization_id == organization.id,
        OrganizationStaff.email.in_((ADMIN_EMAIL, READ_ONLY_EMAIL)),
    ).delete(synchronize_session=False)
    record_audit(
        db,
        organization_id=organization.id,
        actor="fixture-harness",
        actor_type="system",
        actor_id="auth-uat-fixture-harness",
        actor_label="Authentication UAT fixture harness",
        action="uat.auth_fixtures.removed",
        target_type="uat_fixture_set",
        target_id=FIXTURE_PREFIX.rstrip("_"),
        old_value={"removed_counts": rows},
        new_value={"sanitized_lifecycle_audit_retained": True},
    )
    db.commit()
    if any(_fixture_rows(db, organization.id).values()):
        raise RuntimeError("Fixture cleanup verification failed")
    print("FIXTURE_CLEANUP=passed")
    print("SANITIZED_LIFECYCLE_AUDIT_RETAINED=yes")


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage disposable authentication UAT fixtures")
    parser.add_argument("action", choices=("plan", "apply", "cleanup"))
    parser.add_argument("--organization-slug", default="smart-fiber")
    parser.add_argument("--plan-id", type=int)
    args = parser.parse_args()
    database_name = _database_name()
    print(f"DISPOSABLE_DATABASE={database_name}")
    print(f"ACTION={args.action}")
    print(f"FIXTURE_PREFIX={FIXTURE_PREFIX}")
    print("TEST_ONU_USED=no")
    if args.action == "plan":
        print("FIXTURE_PLAN=validated")
        return 0
    with SessionLocal() as db:
        try:
            if args.action == "apply":
                if args.plan_id is None:
                    raise RuntimeError("--plan-id is required for apply")
                apply_fixtures(db, organization_slug=args.organization_slug, plan_id=args.plan_id)
            else:
                cleanup_fixtures(db, organization_slug=args.organization_slug)
        except Exception:
            db.rollback()
            raise
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FIXTURE_HARNESS=failed:{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
