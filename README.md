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
