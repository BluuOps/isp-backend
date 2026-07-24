from fastapi import Depends

from app.core.authorization import (
    AuthenticatedPrincipal,
    require_platform_principal,
)


def require_platform_admin(
    principal: AuthenticatedPrincipal = Depends(require_platform_principal),
) -> AuthenticatedPrincipal:
    return principal
