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
    paystack_webhook_route_token: str | None = os.getenv("PAYSTACK_WEBHOOK_ROUTE_TOKEN")
    payment_gateway: str = os.getenv("PAYMENT_GATEWAY", "manual").lower()
    payment_currency: str = os.getenv("PAYMENT_CURRENCY", "NGN").upper()
    payment_pending_timeout_minutes: int = int(os.getenv("PAYMENT_PENDING_TIMEOUT_MINUTES", "60"))
    webhook_max_payload_bytes: int = int(os.getenv("WEBHOOK_MAX_PAYLOAD_BYTES", "262144"))
    webhook_max_processing_attempts: int = int(os.getenv("WEBHOOK_MAX_PROCESSING_ATTEMPTS", "5"))

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
