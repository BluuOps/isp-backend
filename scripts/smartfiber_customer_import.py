#!/usr/bin/env python3
"""
Smart Fiber Limited live-customer importer.

Safe defaults:
- dry-run unless --commit is explicitly provided
- never deletes existing data
- never updates existing rows unless --update-existing is provided
- never overwrites passwords unless --allow-password-overwrite is provided
- never touches radacct, VLANs, NAS config, or live sessions
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal  # noqa: E402
from app.models import Customer, Organization, RadCheck, RadReply, ServicePlan, User, Zone  # noqa: E402


REQUIRED_FIELDS = [
    "customer_name",
    "phone",
    "email",
    "address",
    "customer_type",
    "location",
    "username",
    "password",
    "plan",
    "zone",
    "expiration_date",
    "status",
    "static_ip",
    "onu_serial",
    "olt_name",
    "pon_port",
    "comment",
]

PASSWORD_ATTRIBUTE = "Cleartext-Password"
RATE_LIMIT_ATTRIBUTE = "Mikrotik-Rate-Limit"
STATIC_IP_ATTRIBUTE = "Framed-IP-Address"
REJECT_ATTRIBUTE = "Auth-Type"
REJECT_VALUE = "Reject"

VALID_STATUSES = {"active", "expired", "suspended", "pending", "terminated"}
VALID_CUSTOMER_TYPES = {"individual", "corporate"}


@dataclass
class ImportIssue:
    row: int
    username: str
    severity: str
    code: str
    message: str


@dataclass
class ImportRecordPlan:
    row: int
    customer_id: str
    username: str
    action: str
    radius_action: str
    status: str
    effective_user_status: str
    reject_auth: bool
    static_ip: str | None


@dataclass
class ImportReport:
    batch_id: str
    mode: str
    organization: str
    source_file: str
    generated_at: str
    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    skipped_rows: int = 0
    created_customers: int = 0
    updated_customers: int = 0
    created_users: int = 0
    updated_users: int = 0
    radius_rows_created_or_updated: int = 0
    issues: list[ImportIssue] = field(default_factory=list)
    records: list[ImportRecordPlan] = field(default_factory=list)


def normalize(value: str | None) -> str:
    return (value or "").strip()


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return cleaned or uuid.uuid4().hex[:12]


def customer_id_for(username: str) -> str:
    return f"sf-{slugify(username)}"[:100]


def parse_location(value: str) -> tuple[float, float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        raise ValueError("location must be 'latitude,longitude'")
    lat = float(parts[0])
    lng = float(parts[1])
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError("location latitude/longitude is out of range")
    return lat, lng


def parse_expiration(value: str) -> datetime:
    raw = normalize(value)
    if not raw:
        raise ValueError("expiration_date is required")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def effective_user_status(status: str) -> str:
    if status == "expired":
        return "active"
    return status


def should_reject(status: str, expiration_date: datetime) -> bool:
    if status in {"expired", "suspended", "pending", "terminated"}:
        return True
    return expiration_date <= datetime.now(timezone.utc)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [field for field in REQUIRED_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(missing)}")
        return [dict(row) for row in reader]


def issue(report: ImportReport, row: int, username: str, severity: str, code: str, message: str) -> None:
    report.issues.append(ImportIssue(row=row, username=username, severity=severity, code=code, message=message))


def upsert_radcheck(db, username: str, attribute: str, value: str, op: str = ":=") -> None:
    row = db.query(RadCheck).filter(RadCheck.username == username, RadCheck.attribute == attribute).first()
    if row:
        row.op = op
        row.value = value
        return
    db.add(RadCheck(username=username, attribute=attribute, op=op, value=value))


def upsert_radreply(db, username: str, attribute: str, value: str, op: str = ":=") -> None:
    row = db.query(RadReply).filter(RadReply.username == username, RadReply.attribute == attribute).first()
    if row:
        row.op = op
        row.value = value
        return
    db.add(RadReply(username=username, attribute=attribute, op=op, value=value))


def remove_radcheck_attribute(db, username: str, attribute: str) -> None:
    for row in db.query(RadCheck).filter(RadCheck.username == username, RadCheck.attribute == attribute).all():
        db.delete(row)


def validate_rows(db, rows: list[dict[str, str]], organization: Organization, args, report: ImportReport) -> list[dict]:
    plans = {
        plan.name: plan
        for plan in db.query(ServicePlan)
        .filter(ServicePlan.organization_id == organization.id, ServicePlan.status == "active")
        .all()
    }
    zones = {
        zone.name
        for zone in db.query(Zone)
        .filter(Zone.organization_id == organization.id, Zone.status == "active")
        .all()
    }

    seen_usernames: set[str] = set()
    valid: list[dict] = []

    for index, row in enumerate(rows, start=2):
        username = normalize(row.get("username")).lower()
        row_errors = 0

        if not username:
            issue(report, index, username, "error", "missing_username", "username is required")
            row_errors += 1
        elif username in seen_usernames:
            issue(report, index, username, "error", "duplicate_csv_username", "username appears more than once in the CSV")
            row_errors += 1
        seen_usernames.add(username)

        required_value_fields = ["customer_name", "phone", "email", "address", "customer_type", "password", "plan", "zone", "expiration_date", "status"]
        for field_name in required_value_fields:
            if not normalize(row.get(field_name)):
                issue(report, index, username, "error", f"missing_{field_name}", f"{field_name} is required")
                row_errors += 1

        customer_type = normalize(row.get("customer_type")).lower()
        if customer_type and customer_type not in VALID_CUSTOMER_TYPES:
            issue(report, index, username, "error", "invalid_customer_type", "customer_type must be individual or corporate")
            row_errors += 1

        status = normalize(row.get("status")).lower()
        if status and status not in VALID_STATUSES:
            issue(report, index, username, "error", "invalid_status", f"status must be one of: {', '.join(sorted(VALID_STATUSES))}")
            row_errors += 1

        plan_name = normalize(row.get("plan"))
        plan = plans.get(plan_name)
        if plan_name and not plan:
            issue(report, index, username, "error", "missing_plan", f"plan '{plan_name}' does not exist or is inactive for {organization.slug}")
            row_errors += 1

        zone_name = normalize(row.get("zone"))
        if zone_name and zone_name not in zones:
            issue(report, index, username, "error", "missing_zone", f"zone '{zone_name}' does not exist or is inactive for {organization.slug}")
            row_errors += 1

        try:
            lat, lng = parse_location(normalize(row.get("location")))
        except Exception as exc:
            issue(report, index, username, "error", "invalid_location", str(exc))
            row_errors += 1
            lat, lng = 0.0, 0.0

        try:
            expiration_date = parse_expiration(normalize(row.get("expiration_date")))
        except Exception as exc:
            issue(report, index, username, "error", "invalid_expiration_date", str(exc))
            row_errors += 1
            expiration_date = datetime.now(timezone.utc)

        existing_user = db.query(User).filter(User.username == username).first() if username else None
        existing_customer = db.query(Customer).filter(
            Customer.id == customer_id_for(username),
            Customer.organization_id == organization.id,
        ).first() if username else None
        existing_radcheck_password = db.query(RadCheck).filter(
            RadCheck.username == username,
            RadCheck.attribute == PASSWORD_ATTRIBUTE,
        ).first() if username else None

        if existing_user and existing_user.organization_id != organization.id:
            issue(report, index, username, "error", "username_other_organization", "username exists under another organization")
            row_errors += 1
        elif existing_user and not args.update_existing and not args.skip_existing:
            issue(report, index, username, "error", "existing_username", "username already exists; use --skip-existing or --update-existing")
            row_errors += 1

        if existing_radcheck_password and existing_user and args.update_existing and not args.allow_password_overwrite:
            issue(report, index, username, "warning", "password_not_overwritten", "existing password will be preserved; use --allow-password-overwrite to replace it")

        if row_errors:
            report.invalid_rows += 1
            continue

        if existing_user and args.skip_existing and not args.update_existing:
            report.skipped_rows += 1
            report.records.append(ImportRecordPlan(
                row=index,
                customer_id=customer_id_for(username),
                username=username,
                action="skip_existing",
                radius_action="none",
                status=status,
                effective_user_status=effective_user_status(status),
                reject_auth=should_reject(status, expiration_date),
                static_ip=normalize(row.get("static_ip")) or None,
            ))
            continue

        valid.append({
            "row_number": index,
            "row": row,
            "username": username,
            "customer_id": customer_id_for(username),
            "customer_type": customer_type,
            "status": status,
            "effective_user_status": effective_user_status(status),
            "expiration_date": expiration_date,
            "latitude": lat,
            "longitude": lng,
            "plan": plan,
            "existing_user": existing_user,
            "existing_customer": existing_customer,
            "reject_auth": should_reject(status, expiration_date),
        })

    report.valid_rows = len(valid)
    return valid


def apply_valid_rows(db, valid_rows: Iterable[dict], organization: Organization, args, report: ImportReport) -> None:
    for item in valid_rows:
        row = item["row"]
        username = item["username"]
        customer = item["existing_customer"]
        user = item["existing_user"]
        customer_action = "updated" if customer else "created"
        user_action = "updated" if user else "created"

        if customer is None:
            customer = Customer(id=item["customer_id"], organization_id=organization.id)
            db.add(customer)
            report.created_customers += 1
        else:
            report.updated_customers += 1

        customer.tenant_id = organization.slug
        customer.name = normalize(row.get("customer_name"))
        customer.customer_type = item["customer_type"]
        customer.email = normalize(row.get("email"))
        customer.phone = normalize(row.get("phone"))
        customer.address = normalize(row.get("address"))
        customer.latitude = item["latitude"]
        customer.longitude = item["longitude"]
        customer.onu_serial = normalize(row.get("onu_serial"))
        customer.olt_name = normalize(row.get("olt_name"))
        customer.pon_port = normalize(row.get("pon_port"))
        customer.account_status = "suspended" if item["status"] in {"suspended", "expired", "terminated"} else "active"

        if user is None:
            user = User(username=username, organization_id=organization.id)
            db.add(user)
            report.created_users += 1
        else:
            report.updated_users += 1

        user.customer_id = customer.id
        user.service_plan = item["plan"].name
        user.zone = normalize(row.get("zone"))
        user.status = item["effective_user_status"]
        user.expiration_date = item["expiration_date"]
        if not user.password or args.allow_password_overwrite or user_action == "created":
            user.password = normalize(row.get("password"))

        if item["status"] in {"terminated", "pending"}:
            remove_radcheck_attribute(db, username, PASSWORD_ATTRIBUTE)
        else:
            upsert_radcheck(db, username, PASSWORD_ATTRIBUTE, user.password)
            report.radius_rows_created_or_updated += 1
            upsert_radreply(db, username, RATE_LIMIT_ATTRIBUTE, item["plan"].rate_limit)
            report.radius_rows_created_or_updated += 1

        static_ip = normalize(row.get("static_ip"))
        if static_ip:
            upsert_radreply(db, username, STATIC_IP_ATTRIBUTE, static_ip)
            report.radius_rows_created_or_updated += 1

        if item["reject_auth"]:
            upsert_radcheck(db, username, REJECT_ATTRIBUTE, REJECT_VALUE)
            report.radius_rows_created_or_updated += 1
        else:
            remove_radcheck_attribute(db, username, REJECT_ATTRIBUTE)

        report.records.append(ImportRecordPlan(
            row=item["row_number"],
            customer_id=customer.id,
            username=username,
            action=f"{customer_action}_customer/{user_action}_user",
            radius_action="reject" if item["reject_auth"] else "accept",
            status=item["status"],
            effective_user_status=item["effective_user_status"],
            reject_auth=item["reject_auth"],
            static_ip=static_ip or None,
        ))


def write_report(report: ImportReport, report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"smartfiber-import-{report.batch_id}.json"
    payload = asdict(report)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def print_summary(report: ImportReport, report_path: Path) -> None:
    print(f"Batch ID: {report.batch_id}")
    print(f"Mode: {report.mode}")
    print(f"Organization: {report.organization}")
    print(f"Rows: total={report.total_rows} valid={report.valid_rows} invalid={report.invalid_rows} skipped={report.skipped_rows}")
    print(f"Customers: created={report.created_customers} updated={report.updated_customers}")
    print(f"Users: created={report.created_users} updated={report.updated_users}")
    print(f"RADIUS rows created/updated: {report.radius_rows_created_or_updated}")
    print(f"Issues: {len(report.issues)}")
    for item in report.issues[:20]:
        print(f"- row {item.row} [{item.severity}/{item.code}] {item.username}: {item.message}")
    if len(report.issues) > 20:
        print(f"- ... {len(report.issues) - 20} more issues in report")
    print(f"Report: {report_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Smart Fiber Limited customers and PPPoE users safely.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Validate and report only; make no database changes.")
    mode.add_argument("--commit", action="store_true", help="Apply valid rows in a single transaction.")
    parser.add_argument("--organization", default="smart-fiber", help="Organization slug. Default: smart-fiber.")
    parser.add_argument("--file", required=True, help="CSV file to import.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip usernames that already exist.")
    parser.add_argument("--update-existing", action="store_true", help="Update existing customers/users in the same organization.")
    parser.add_argument("--allow-password-overwrite", action="store_true", help="Allow --update-existing to overwrite existing passwords.")
    parser.add_argument("--report-dir", default="import_reports", help="Directory for JSON import reports.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    csv_path = Path(args.file).resolve()
    report = ImportReport(
        batch_id=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8],
        mode="commit" if args.commit else "dry-run",
        organization=args.organization,
        source_file=str(csv_path),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )

    try:
        rows = read_csv(csv_path)
    except Exception as exc:
        print(f"CSV error: {exc}", file=sys.stderr)
        return 2

    report.total_rows = len(rows)
    db = SessionLocal()
    try:
        organization = db.query(Organization).filter(Organization.slug == args.organization, Organization.status == "active").first()
        if not organization:
            print(f"Organization '{args.organization}' does not exist or is not active.", file=sys.stderr)
            return 2

        valid = validate_rows(db, rows, organization, args, report)
        if args.commit:
            if report.invalid_rows:
                print("Warning: invalid rows were skipped; only valid rows will be committed.", file=sys.stderr)
            apply_valid_rows(db, valid, organization, args, report)
            db.commit()
        else:
            for item in valid:
                report.records.append(ImportRecordPlan(
                    row=item["row_number"],
                    customer_id=item["customer_id"],
                    username=item["username"],
                    action="would_update" if item["existing_user"] else "would_create",
                    radius_action="would_reject" if item["reject_auth"] else "would_accept",
                    status=item["status"],
                    effective_user_status=item["effective_user_status"],
                    reject_auth=item["reject_auth"],
                    static_ip=normalize(item["row"].get("static_ip")) or None,
                ))

        report_path = write_report(report, Path(args.report_dir))
        print_summary(report, report_path)
        return 0 if args.commit or not report.invalid_rows else 2
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
