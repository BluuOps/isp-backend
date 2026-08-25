# Live Backend Deployment Package

This folder is the deployment package for the live Ubuntu backend.

Before deploying, read:

```text
DEPLOYMENT.md
```

The files copied to the server are controlled by:

```text
deployment_manifest.txt
```

## Server Backend Shape

```text
backend/
  app/
    __init__.py
    database.py
    main.py
    seed.py
    models/
      __init__.py
      billing.py
      customer.py
      radacct.py
      radius.py
      service_plan.py
      user.py
    schemas/
      __init__.py
      billing.py
      customer.py
      radius_session.py
      service_plan.py
      user.py
    routers/
      __init__.py
      billing.py
      customers.py
      plans.py
      radius_sessions.py
      users.py
  init_db.py
  preflight.py
  requirements.txt
  venv/
```

Models are intentionally kept in the `app/models/` package. Routers should import the public model names through:

```python
from app.models import RadCheck, RadReply, ServicePlan, User
```

`app/models/__init__.py` is responsible for exporting those names.

Schemas are intentionally kept in the `app/schemas/` package. Routers import public schema names through:

```python
from app.schemas import UserCreate, UserResponse, ServicePlanCreate
```

`app/schemas/__init__.py` is responsible for exporting those names.

## Deploy

On the Ubuntu server:

```bash
cd /path/to/VUX-2.0/isp-platform/ops/backend-flat
chmod +x deploy_to_backend.sh
SKIP_PIP_INSTALL=1 ./deploy_to_backend.sh ~/isp-platform/backend radiusfiber-backend
```

Remove `SKIP_PIP_INSTALL=1` if you want the script to install/update Python dependencies.

## Verify

```bash
cd ~/isp-platform/backend
source venv/bin/activate
python preflight.py
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/users/
curl http://127.0.0.1:8000/plans
```

## Deletion lifecycle

RadiusFiber treats deletes as operational lifecycle events, not raw database row
removal. API handlers must return business-safe responses instead of exposing
database integrity errors.

### Subscribers / PPPoE users

`DELETE /users/{id}` is only allowed when the subscriber is already terminated
and has no linked billing or RADIUS provisioning rows.

Otherwise the API returns `409 Conflict` with a structured response such as:

```json
{
  "error": "user_has_billing_account",
  "message": "Remove or archive the subscriber billing account before deletion."
}
```

Use suspend, terminate, recharge, password reset, and RADIUS lifecycle endpoints
for normal operations. Hard deletion is reserved for records that have already
been detached from billing and RADIUS state.

### Customers

`DELETE /customers/{id}` is blocked while linked PPPoE subscribers exist.
Return `409 Conflict` with:

```json
{
  "error": "customer_has_subscribers",
  "message": "Remove or reassign linked PPPoE subscribers before deleting this customer."
}
```

### Service plans

`DELETE /plans/{id}` is blocked while non-terminated subscribers use the plan.
Return `409 Conflict` with:

```json
{
  "error": "service_plan_has_active_subscribers",
  "message": "Cannot delete a service plan assigned to non-terminated subscribers."
}
```

### SaaS/commercial objects

- Default organization (`smart-fiber`) is not hard deleted; platform deletion
  requests cancel/suspend non-default organizations.
- Organization staff can be deleted except when it would remove the last active
  organization admin.
- Feature flags are configuration records; prefer updates/disablement over
  deletion.
- Audit logs are immutable and should not be deleted by normal API flows.

## Rollback

The deploy script prints the backup directory it created under:

```text
~/backend-backups/
```

To restore:

```bash
sudo systemctl stop radiusfiber-backend
cp -a ~/backend-backups/BACKUP_NAME/app ~/isp-platform/backend/app
cp -a ~/backend-backups/BACKUP_NAME/init_db.py ~/isp-platform/backend/init_db.py 2>/dev/null || true
sudo systemctl start radiusfiber-backend
```
# Authentication token compatibility and cleanup

Access tokens issued from migration `0013_auth_token_revocations` onward contain
a nonempty `jti`. Tokens issued before this claim was introduced are rejected
and users must sign in again after deployment. No fallback identifier is
generated from a legacy token, and raw tokens/JTIs are never persisted.

Logout performs one opportunistic, savepoint-isolated cleanup batch (100 rows by
default, configurable with `AUTH_TOKEN_REVOCATION_CLEANUP_BATCH_SIZE`). PostgreSQL
`CURRENT_TIMESTAMP` selects rows in deterministic `(expires_at, id)` order only
after JWT expiry plus a clock-skew retention interval. The retention defaults to
300 seconds and is bounded to 60-3600 seconds through
`AUTH_TOKEN_REVOCATION_RETENTION_SECONDS`. The exact retention boundary is
deletion-eligible; rows before it remain enforceable. Repeated logout activity
drains a backlog without adding cleanup work to ordinary token validation. A
future high-volume deployment may add a scheduled maintenance command that
repeatedly calls the same bounded cleanup function; that command is intentionally
outside the current release scope.
