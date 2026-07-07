# Smart Fiber Limited live-customer migration plan

This plan prepares Smart Fiber Limited customer and PPPoE migration into RadiusFiber SaaS.

Do not run against production until staging dry-run, staging commit, functional regression, and rollback have passed.

## Non-negotiable safety rules

- Do not modify production until approved.
- Do not touch VLAN 300.
- Do not manually close or edit `radacct`.
- Do not delete existing customers.
- Do not overwrite existing PPPoE users unless `--update-existing` is explicitly used.
- Do not overwrite passwords unless `--allow-password-overwrite` is explicitly used.
- Take a PostgreSQL backup before every staging or production commit attempt.
- Keep the old system available during the migration window.

## 1. Migration readiness checklist

Run these checks on staging first, then repeat on production only after approval.

```bash
# STAGING ONLY
cd /home/bluops/isp-platform/backend-phase1
source venv/bin/activate

python - <<'PY'
from app.database import SessionLocal
from app.models import Organization, ServicePlan, Zone
db = SessionLocal()
org = db.query(Organization).filter_by(slug="smart-fiber").first()
print("organization:", org.id if org else None, org.name if org else None, org.status if org else None)
print("plans:", [p.name for p in db.query(ServicePlan).filter_by(organization_id=org.id).all()] if org else [])
print("zones:", [z.name for z in db.query(Zone).filter_by(organization_id=org.id).all()] if org else [])
db.close()
PY
```

Confirm:

- Smart Fiber organization exists and is active.
- `organization_id` is assigned on existing Smart Fiber rows.
- Plans exist and names match the CSV exactly.
- Zones exist and names match the CSV exactly.
- NAS exists in FreeRADIUS and MikroTik can authenticate test RADIUS.
- PostgreSQL backup exists.
- Rollback report/export destination exists.
- `/health` and `/health/ready` pass.

## 2. CSV import format

Template:

```text
ops/backend-flat/import_templates/smartfiber_live_customers_template.csv
```

Required columns:

```text
customer_name,phone,email,address,customer_type,location,username,password,plan,zone,expiration_date,status,static_ip,onu_serial,olt_name,pon_port,comment
```

Rules:

- `location` format is `latitude,longitude`.
- `customer_type` must be `individual` or `corporate`.
- `status` must be `active`, `expired`, `suspended`, `pending`, or `terminated`.
- `expiration_date` must be ISO/date parseable, for example `2026-12-31`.
- `plan` must already exist for `smart-fiber`.
- `zone` must already exist for `smart-fiber`.

## 3. Import command

Dry-run:

```bash
# STAGING ONLY
cd /home/bluops/isp-platform/backend-phase1
source venv/bin/activate
python scripts/smartfiber_customer_import.py \
  --dry-run \
  --organization smart-fiber \
  --file import_templates/smartfiber_staging_sample.csv \
  --skip-existing
```

Commit to staging:

```bash
# STAGING ONLY
cd /home/bluops/isp-platform/backend-phase1
source venv/bin/activate
python scripts/smartfiber_customer_import.py \
  --commit \
  --organization smart-fiber \
  --file import_templates/smartfiber_clean_validated.csv \
  --skip-existing
```

Update existing records only when intentionally rehearsing updates:

```bash
# STAGING ONLY
python scripts/smartfiber_customer_import.py \
  --commit \
  --organization smart-fiber \
  --file import_templates/smartfiber_clean_validated.csv \
  --update-existing
```

Password overwrite requires a separate explicit flag:

```bash
# STAGING ONLY - only after written approval
python scripts/smartfiber_customer_import.py \
  --commit \
  --organization smart-fiber \
  --file import_templates/smartfiber_clean_validated.csv \
  --update-existing \
  --allow-password-overwrite
```

## 4. Dry-run behavior

Dry-run makes no database changes.

It validates:

- CSV columns.
- Duplicate usernames inside CSV.
- Existing usernames in RadiusFiber.
- Missing plans.
- Missing zones.
- Invalid expiration dates.
- Invalid location format.
- Invalid statuses.
- CRM/PPPoE link plan.

Example dry-run output:

```text
Batch ID: 20260707011500-abc123ef
Mode: dry-run
Organization: smart-fiber
Rows: total=10 valid=8 invalid=2 skipped=0
Customers: created=0 updated=0
Users: created=0 updated=0
RADIUS rows created/updated: 0
Issues: 2
- row 10 [error/duplicate_csv_username] sf_active_001: username appears more than once in the CSV
- row 11 [error/missing_plan] sf_missing_plan: plan 'Missing Plan' does not exist or is inactive for smart-fiber
Report: import_reports/smartfiber-import-20260707011500-abc123ef.json
```

## 5. Commit behavior

Commit mode applies valid rows in one transaction and skips invalid rows. Invalid rows are listed in the report and must be fixed before the final production import.

It creates or updates:

- CRM `customers`.
- App `users`.
- `users.organization_id = smart-fiber` organization row ID.
- `customers.organization_id = smart-fiber` organization row ID.
- `radcheck` `Cleartext-Password`.
- `radreply` `Mikrotik-Rate-Limit`.
- `radreply` `Framed-IP-Address` when `static_ip` is present.
- `radcheck` `Auth-Type := Reject` for expired, suspended, pending, terminated, or date-expired users.

Example commit output:

```text
Batch ID: 20260707013000-def456ab
Mode: commit
Organization: smart-fiber
Rows: total=8 valid=8 invalid=0 skipped=0
Customers: created=8 updated=0
Users: created=8 updated=0
RADIUS rows created/updated: 25
Issues: 0
Report: import_reports/smartfiber-import-20260707013000-def456ab.json
```

## 6. Staging test plan

Use:

```text
ops/backend-flat/import_templates/smartfiber_staging_sample.csv
```

Expected dry-run:

- 5 active users valid.
- 2 expired users valid and planned for Reject.
- 1 suspended user valid and planned for Reject.
- 1 duplicate username invalid.
- 1 missing plan invalid.

For commit testing:

1. Run commit on the original sample to confirm invalid rows are skipped and valid rows are imported.
2. Copy the sample CSV.
3. Remove the duplicate username row.
4. Remove or fix the missing plan row.
5. Save as `smartfiber_clean_validated.csv`.
6. Run commit on staging only with the cleaned CSV to confirm a clean import path.

After commit, verify:

```bash
# STAGING ONLY
psql "$DATABASE_URL" -c "SELECT count(*) FROM customers WHERE tenant_id='smart-fiber';"
psql "$DATABASE_URL" -c "SELECT username,status,expiration_date FROM users WHERE username LIKE 'sf_%' ORDER BY username;"
psql "$DATABASE_URL" -c "SELECT username,attribute,value FROM radcheck WHERE username LIKE 'sf_%' ORDER BY username,attribute;"
psql "$DATABASE_URL" -c "SELECT username,attribute,value FROM radreply WHERE username LIKE 'sf_%' ORDER BY username,attribute;"
```

Functional regression:

- CRM list/detail loads imported customers.
- PPPoE list loads imported users.
- Expired users have `Auth-Type := Reject`.
- Suspended users have `Auth-Type := Reject`.
- Active users have `Cleartext-Password` and `Mikrotik-Rate-Limit`.
- Static IP rows have `Framed-IP-Address`.
- Recharge works on an imported user.
- Disconnect mock ACK still works.
- Reconnect removes/overrides Reject where applicable.
- Password reset still writes `Cleartext-Password`.
- `/radius/sessions` still reads `radacct`.

## 7. Rollback strategy

Every run writes:

```text
import_reports/smartfiber-import-<batch_id>.json
```

Rollback options:

1. Preferred staging rollback:
   - Use the import report.
   - Delete imported `radcheck` and `radreply` rows by `username`.
   - Delete imported `users` rows by `username`.
   - Delete imported `customers` rows by `customer_id`.

2. Critical rollback:
   - Restore the pre-import PostgreSQL backup.

Generate rollback SQL from a report:

```bash
# STAGING ONLY - inspect before running
python - <<'PY'
import json
from pathlib import Path
report = json.loads(Path("import_reports/REPLACE_WITH_REPORT.json").read_text())
usernames = [r["username"] for r in report["records"] if "created" in r["action"] or "would" not in r["action"]]
customer_ids = [r["customer_id"] for r in report["records"] if "created" in r["action"] or "would" not in r["action"]]
def q(v): return "'" + v.replace("'", "''") + "'"
print("DELETE FROM radreply WHERE username IN (" + ",".join(map(q, usernames)) + ");")
print("DELETE FROM radcheck WHERE username IN (" + ",".join(map(q, usernames)) + ");")
print("DELETE FROM users WHERE username IN (" + ",".join(map(q, usernames)) + ");")
print("DELETE FROM customers WHERE id IN (" + ",".join(map(q, customer_ids)) + ");")
PY
```

Run rollback SQL only after inspection and approval.

## 8. Scale check

The current FastAPI + PostgreSQL + FreeRADIUS + MikroTik architecture can support:

- 400 customers.
- 400 PPPoE users.
- `radacct` session reads.
- Session polling.
- Recharge lifecycle.
- Disconnect/reconnect lifecycle.

Main risk is migration accuracy, not scale.

## 9. Recommended migration window

After UI is stable, importer passes staging, and rollback is tested:

- Low traffic window: midnight–4am.
- Keep old system available.
- Take backup immediately before import.
- Dry-run production CSV immediately before commit.
- Import in one batch only if dry-run is clean.
- Validate RADIUS auth, CRM, billing, sessions, recharge, disconnect, reconnect.
