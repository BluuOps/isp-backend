# Live Backend Deployment

## Required environment

Create `/etc/radiusfiber/.env` outside Git and restrict it to the service account.
At minimum it must define `DATABASE_URL`. Configure the systemd unit with:

```ini
EnvironmentFile=/etc/radiusfiber/.env
```

Run migrations explicitly before deploying tenant-aware application code:

```bash
cd ~/isp-platform/backend
set -a
source /etc/radiusfiber/.env
set +a
venv/bin/alembic current
```

Do not run `alembic upgrade` against production until the backup, baseline stamp,
and migration SQL have been reviewed and approved.

This folder is the live-server backend deployment package. Deploy from here when the server is using the modular FastAPI backend.

## What Ships

`deployment_manifest.txt` is the source of truth. Only the files listed there are copied into the live backend directory.

## One-Time Server Layout

Expected server paths:

```bash
~/isp-platform/backend
~/isp-platform/backend/venv
```

Expected service:

```bash
radiusfiber-backend
```

If your service name is different, pass it as the second argument to `deploy_to_backend.sh`.

## Deploy Steps

1. Upload or pull the latest repo onto the server.

2. Go to this deployment package:

```bash
cd ~/VUX-2.0/isp-platform/ops/backend-flat
```

Use the real path where this repo lives on your server.

3. Run the deploy script:

```bash
chmod +x deploy_to_backend.sh
./deploy_to_backend.sh ~/isp-platform/backend radiusfiber-backend
```

If dependencies are already installed and the server has limited internet, skip package installation:

```bash
SKIP_PIP_INSTALL=1 ./deploy_to_backend.sh ~/isp-platform/backend radiusfiber-backend
```

4. Confirm the API:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/users/
curl http://127.0.0.1:8000/plans
```

## What The Script Does

- Verifies every manifest file exists in this package.
- Backs up the current server backend files to `~/backend-backups`.
- Copies only the manifest files into `~/isp-platform/backend`.
- Keeps `app/models/` as the model package and updates its exported models.
- Keeps `app/schemas/` as the schema package and updates its exported schemas.
- Installs or updates Python packages if `requirements.txt` is present.
- Runs import and route preflight checks.
- Creates or verifies database tables and seed plans.
- Restarts the systemd service.

## Rollback

If a deploy fails after files were copied, restore the printed backup directory:

```bash
sudo systemctl stop radiusfiber-backend
cp -a ~/backend-backups/BACKUP_NAME/app ~/isp-platform/backend/app
cp -a ~/backend-backups/BACKUP_NAME/init_db.py ~/isp-platform/backend/init_db.py
sudo systemctl start radiusfiber-backend
```

Replace `BACKUP_NAME` with the backup folder printed by the deploy script.
