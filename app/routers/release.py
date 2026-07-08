import os

from fastapi import APIRouter


router = APIRouter(tags=["Release"])

RELEASE_VERSION = "1.0.0-rc1"
BACKEND_COMMIT = "9d4cddb86f3a265ec2044e320e75c36af275b879"
MIGRATION_HEAD = "0003_commercial_management"
BUILT_AT = "2026-07-08T00:00:00+01:00"


@router.get("/release")
def release() -> dict[str, str]:
    return {
        "version": RELEASE_VERSION,
        "backend_commit": BACKEND_COMMIT,
        "migration_head": MIGRATION_HEAD,
        "environment": os.getenv("RADIUSFIBER_ENVIRONMENT", os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "unknown"))),
        "built_at": os.getenv("RADIUSFIBER_BUILT_AT", BUILT_AT),
    }
