import os
from dataclasses import dataclass

from dotenv import load_dotenv
from sqlalchemy.engine import make_url

ENV_FILE = os.getenv("RADIUSFIBER_ENV_FILE", "/etc/radiusfiber/.env")
load_dotenv(ENV_FILE, override=False)
STAGING_UAT_DATABASE_NAME = "isp_db_stage"


def required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required environment variable is not configured: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str = required_environment("DATABASE_URL")
    deployment_environment: str = os.getenv("RADIUSFIBER_ENVIRONMENT", "production").strip().lower()
    staging_uat_fixtures_enabled: bool = os.getenv(
        "STAGING_UAT_FIXTURES_ENABLED", "false"
    ).lower() in {"1", "true", "yes", "on"}
    default_organization_slug: str = os.getenv("DEFAULT_ORGANIZATION_SLUG", "smart-fiber")
    radclient_bin: str = os.getenv("RADIUS_RADCLIENT_BIN", "/usr/bin/radclient")
    coa_secret_path: str = os.getenv(
        "COA_SECRET_PATH",
        os.getenv("RADIUS_COA_SECRET_FILE", "/etc/radiusfiber/coa.secret"),
    )
    coa_nas_ip: str = os.getenv("RADIUS_COA_NAS_IP", "192.168.222.1")
    coa_port: str = os.getenv("RADIUS_COA_PORT", "3799")
    radius_coa_enabled: bool = os.getenv("RADIUS_COA_ENABLED", "false").lower() in {
        "1", "true", "yes", "on",
    }
    radius_disconnect_mode: str = os.getenv("RADIUS_DISCONNECT_MODE", "disabled").lower()
    radius_disconnect_timeout_seconds: int = max(
        1, min(30, int(os.getenv("RADIUS_DISCONNECT_TIMEOUT_SECONDS", "12")))
    )
    radius_session_freshness_seconds: int = max(
        60, min(3600, int(os.getenv("RADIUS_SESSION_FRESHNESS_SECONDS", "900")))
    )
    radius_disconnect_nas_allowlist: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv("RADIUS_DISCONNECT_NAS_ALLOWLIST", "").split(",")
        if item.strip()
    )
    expiry_worker_enabled: bool = os.getenv("EXPIRY_WORKER_ENABLED", "false").lower() in {
        "1", "true", "yes", "on",
    }
    expiry_worker_dry_run: bool = os.getenv("EXPIRY_WORKER_DRY_RUN", "true").lower() in {
        "1", "true", "yes", "on",
    }
    expiry_scan_batch_size: int = max(1, min(1000, int(os.getenv("EXPIRY_SCAN_BATCH_SIZE", "100"))))
    expiry_disconnect_max_attempts: int = max(
        1, min(10, int(os.getenv("EXPIRY_DISCONNECT_MAX_ATTEMPTS", "3")))
    )
    expiry_disconnect_backoff_seconds: int = max(
        5, min(3600, int(os.getenv("EXPIRY_DISCONNECT_BACKOFF_SECONDS", "30")))
    )
    expiry_disconnect_processing_timeout_seconds: int = max(
        30, min(3600, int(os.getenv("EXPIRY_DISCONNECT_PROCESSING_TIMEOUT_SECONDS", "120")))
    )
    pilot_calledstationid: str = os.getenv(
        "PILOT_CALLEDSTATIONID",
        os.getenv("RADIUS_PILOT_CALLED_STATION_ID", "core-radius-pilot"),
    )
    smtp_url: str | None = os.getenv("SMTP_URL")
    jwt_secret: str | None = os.getenv("JWT_SECRET")
    auth_token_issuer: str = os.getenv("AUTH_TOKEN_ISSUER", "radiusfiber")
    auth_token_audience: str = os.getenv(
        "AUTH_TOKEN_AUDIENCE",
        "radiusfiber-organization-api",
    )
    auth_access_token_ttl_seconds: int = max(
        300,
        min(12 * 60 * 60, int(os.getenv("AUTH_ACCESS_TOKEN_TTL_SECONDS", "3600"))),
    )
    auth_token_revocation_cleanup_batch_size: int = max(
        1,
        min(1000, int(os.getenv("AUTH_TOKEN_REVOCATION_CLEANUP_BATCH_SIZE", "100"))),
    )
    auth_token_revocation_retention_seconds: int = max(
        60,
        min(3600, int(os.getenv("AUTH_TOKEN_REVOCATION_RETENTION_SECONDS", "300"))),
    )
    admin_invitation_ttl_seconds: int = max(
        900, min(1800, int(os.getenv("ADMIN_INVITATION_TTL_SECONDS", "1200")))
    )
    admin_invitation_recent_auth_seconds: int = max(
        300, min(1800, int(os.getenv("ADMIN_INVITATION_RECENT_AUTH_SECONDS", "600")))
    )
    admin_invitation_max_attempts: int = max(
        3, min(10, int(os.getenv("ADMIN_INVITATION_MAX_ATTEMPTS", "5")))
    )
    olt_management_networks: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv(
            "OLT_MANAGEMENT_NETWORKS",
            "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
        ).split(",")
        if item.strip()
    )
    olt_integration_enabled: bool = os.getenv("OLT_INTEGRATION_ENABLED", "false").lower() in {
        "1", "true", "yes", "on",
    }
    platform_admin_api_key: str | None = os.getenv("PLATFORM_ADMIN_API_KEY")
    platform_admin_email: str | None = os.getenv("PLATFORM_ADMIN_EMAIL")
    platform_admin_password: str | None = os.getenv("PLATFORM_ADMIN_PASSWORD")
    internal_admin_email: str | None = os.getenv("INTERNAL_ADMIN_EMAIL")
    internal_admin_password: str | None = os.getenv("INTERNAL_ADMIN_PASSWORD")
    upload_path: str = os.getenv("UPLOAD_PATH", "/var/lib/radiusfiber/uploads")
    tenant_allowed_domains: tuple[str, ...] = tuple(
        item.strip().lower().lstrip(".")
        for item in os.getenv(
            "TENANT_ALLOWED_DOMAINS",
            os.getenv("ALLOWED_TENANT_DOMAINS", "radiusfiber.com"),
        ).split(",")
        if item.strip()
    )
    tenant_reserved_subdomains: tuple[str, ...] = tuple(
        item.strip().lower()
        for item in os.getenv("TENANT_RESERVED_SUBDOMAINS", "app,api,www,admin,platform").split(",")
        if item.strip()
    )
    tenant_host_aliases: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv("TENANT_HOST_ALIASES", "").split(",")
        if item.strip()
    )
    allow_staging_tenant_fallback: bool = os.getenv("ALLOW_STAGING_TENANT_FALLBACK", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    staging_organization_slug: str = os.getenv("STAGING_ORGANIZATION_SLUG", default_organization_slug)
    trust_forwarded_host: bool = os.getenv("TRUST_FORWARDED_HOST", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    trusted_proxy_ips: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv("TRUSTED_PROXY_IPS", "127.0.0.1,::1").split(",")
        if item.strip()
    )
    paystack_enabled: bool = os.getenv("PAYSTACK_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    paystack_mode: str = os.getenv("PAYSTACK_MODE", "test").lower()
    paystack_secret_key: str | None = os.getenv("PAYSTACK_SECRET_KEY")
    paystack_public_key: str | None = os.getenv("PAYSTACK_PUBLIC_KEY")
    paystack_base_url: str = os.getenv("PAYSTACK_BASE_URL", "https://api.paystack.co").rstrip("/")
    paystack_callback_base_url: str | None = os.getenv("PAYSTACK_CALLBACK_BASE_URL")
    paystack_callback_base_urls: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv("PAYSTACK_CALLBACK_BASE_URLS", "").split(",")
        if item.strip()
    )
    paystack_webhook_route_token: str | None = os.getenv("PAYSTACK_WEBHOOK_ROUTE_TOKEN")
    payment_gateway: str = os.getenv("PAYMENT_GATEWAY", "manual").lower()
    payment_currency: str = os.getenv("PAYMENT_CURRENCY", "NGN").upper()
    payment_pending_timeout_minutes: int = int(os.getenv("PAYMENT_PENDING_TIMEOUT_MINUTES", "60"))
    webhook_max_payload_bytes: int = int(os.getenv("WEBHOOK_MAX_PAYLOAD_BYTES", "262144"))
    webhook_max_processing_attempts: int = int(os.getenv("WEBHOOK_MAX_PROCESSING_ATTEMPTS", "5"))

    def validate_staging_uat_fixture_configuration(self) -> None:
        if not self.staging_uat_fixtures_enabled:
            return
        if self.deployment_environment != "staging":
            raise RuntimeError(
                "STAGING_UAT_FIXTURES_ENABLED may only be enabled when "
                "RADIUSFIBER_ENVIRONMENT=staging"
            )
        if make_url(self.database_url).database != STAGING_UAT_DATABASE_NAME:
            raise RuntimeError(
                "STAGING_UAT_FIXTURES_ENABLED requires database isp_db_stage"
            )

    def staging_uat_fixture_routes_enabled(self) -> bool:
        return (
            self.deployment_environment == "staging"
            and self.staging_uat_fixtures_enabled
            and make_url(self.database_url).database == STAGING_UAT_DATABASE_NAME
        )

    def require_paystack(self) -> None:
        if not self.paystack_enabled:
            return
        missing = [
            name
            for name, value in {
                "PAYSTACK_SECRET_KEY": self.paystack_secret_key,
                "PAYSTACK_PUBLIC_KEY": self.paystack_public_key,
                "PAYSTACK_CALLBACK_BASE_URL": self.paystack_callback_base_url,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(f"Paystack is enabled but missing required configuration: {', '.join(missing)}")


settings = Settings()
settings.validate_staging_uat_fixture_configuration()
