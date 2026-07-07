import os
from dataclasses import dataclass

from dotenv import load_dotenv

ENV_FILE = os.getenv("RADIUSFIBER_ENV_FILE", "/etc/radiusfiber/.env")
load_dotenv(ENV_FILE, override=False)


def required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required environment variable is not configured: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str = required_environment("DATABASE_URL")
    default_organization_slug: str = os.getenv("DEFAULT_ORGANIZATION_SLUG", "smart-fiber")
    radclient_bin: str = os.getenv("RADIUS_RADCLIENT_BIN", "/usr/bin/radclient")
    coa_secret_path: str = os.getenv(
        "COA_SECRET_PATH",
        os.getenv("RADIUS_COA_SECRET_FILE", "/etc/radiusfiber/coa.secret"),
    )
    coa_nas_ip: str = os.getenv("RADIUS_COA_NAS_IP", "192.168.222.1")
    coa_port: str = os.getenv("RADIUS_COA_PORT", "3799")
    pilot_calledstationid: str = os.getenv(
        "PILOT_CALLEDSTATIONID",
        os.getenv("RADIUS_PILOT_CALLED_STATION_ID", "core-radius-pilot"),
    )
    smtp_url: str | None = os.getenv("SMTP_URL")
    jwt_secret: str | None = os.getenv("JWT_SECRET")
    platform_admin_api_key: str | None = os.getenv("PLATFORM_ADMIN_API_KEY")
    upload_path: str = os.getenv("UPLOAD_PATH", "/var/lib/radiusfiber/uploads")


settings = Settings()
