import secrets

from fastapi import Header, HTTPException, status

from app.core.config import settings
from app.core.principal import bearer_payload


def require_platform_admin(
    authorization: str | None = Header(default=None),
    x_platform_admin_key: str | None = Header(default=None),
) -> None:
    payload = bearer_payload(authorization)
    if payload and payload.get("principal_type") == "platform_admin":
        return
    if payload:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Platform administrator access is required")

    expected = settings.platform_admin_api_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Platform administration is not configured",
        )
    if not x_platform_admin_key or not secrets.compare_digest(x_platform_admin_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid platform key")
