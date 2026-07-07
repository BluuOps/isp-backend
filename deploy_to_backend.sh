#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-$HOME/isp-platform/backend}"
SERVICE_NAME="${2:-radiusfiber-backend}"
BACKUP_ROOT="$HOME/backend-backups"
BACKUP_DIR="$BACKUP_ROOT/backend-before-lifecycle-$(date +%F-%H%M%S)"
MANIFEST_FILE="$SOURCE_DIR/deployment_manifest.txt"

echo "Source: $SOURCE_DIR"
echo "Target: $TARGET_DIR"
echo "Service: $SERVICE_NAME"
echo "Manifest: $MANIFEST_FILE"

if [ ! -d "$TARGET_DIR" ]; then
  echo "ERROR: Target backend directory does not exist: $TARGET_DIR" >&2
  exit 1
fi

if [ ! -f "$MANIFEST_FILE" ]; then
  echo "ERROR: Deployment manifest does not exist: $MANIFEST_FILE" >&2
  exit 1
fi

echo "Verifying deployment package..."
while IFS= read -r relative_path || [ -n "$relative_path" ]; do
  if [ -z "$relative_path" ] || [[ "$relative_path" == \#* ]]; then
    continue
  fi

  if [ ! -f "$SOURCE_DIR/$relative_path" ]; then
    echo "ERROR: Manifest file is missing from source package: $relative_path" >&2
    exit 1
  fi
done < "$MANIFEST_FILE"

mkdir -p "$BACKUP_DIR"

if [ -d "$TARGET_DIR/app" ]; then
  cp -a "$TARGET_DIR/app" "$BACKUP_DIR/app"
fi

if [ -f "$TARGET_DIR/init_db.py" ]; then
  cp -a "$TARGET_DIR/init_db.py" "$BACKUP_DIR/init_db.py"
fi

if [ -f "$TARGET_DIR/preflight.py" ]; then
  cp -a "$TARGET_DIR/preflight.py" "$BACKUP_DIR/preflight.py"
fi

if [ -f "$TARGET_DIR/requirements.txt" ]; then
  cp -a "$TARGET_DIR/requirements.txt" "$BACKUP_DIR/requirements.txt"
fi

echo "Backup saved to: $BACKUP_DIR"

echo "Clearing legacy flat files that conflict with modular packages..."
for conflict_path in app/models.py app/schemas.py; do
  if [ -f "$TARGET_DIR/$conflict_path" ]; then
    mkdir -p "$BACKUP_DIR/$(dirname "$conflict_path")"
    mv "$TARGET_DIR/$conflict_path" "$BACKUP_DIR/$conflict_path"
    echo "Moved conflicting file to backup: $conflict_path"
  fi
done

echo "Copying backend package..."
while IFS= read -r relative_path || [ -n "$relative_path" ]; do
  if [ -z "$relative_path" ] || [[ "$relative_path" == \#* ]]; then
    continue
  fi

  mkdir -p "$TARGET_DIR/$(dirname "$relative_path")"
  cp "$SOURCE_DIR/$relative_path" "$TARGET_DIR/$relative_path"
done < "$MANIFEST_FILE"

cd "$TARGET_DIR"

if [ -d venv ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
else
  echo "WARNING: No venv directory found in $TARGET_DIR. Using system Python." >&2
fi

if [ "${SKIP_PIP_INSTALL:-0}" = "1" ]; then
  echo "Skipping Python dependency install because SKIP_PIP_INSTALL=1."
elif [ -f requirements.txt ]; then
  echo "Installing/updating Python dependencies..."
  python -m pip install --disable-pip-version-check -r requirements.txt
fi

echo "Checking imports and routes..."
python preflight.py
python -c "from app import app; print('Package app import routes:', len(app.routes))"
python -c "from app.main import app; print('FastAPI routes:', len(app.routes))"

echo "Checking migration state (migrations are never applied automatically)..."
python -m alembic current

echo "Restarting systemd service..."
sudo systemctl restart "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager

echo
echo "Deployment complete."
echo "Open Swagger: http://SERVER_IP:8000/docs"
echo "If anything failed, restore from backup: $BACKUP_DIR"
