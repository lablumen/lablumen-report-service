"""Cognito JWT verification (in-process).

User provisioning is owned by appointment-service; here we only verify the token and expose
claims/roles. PyJWKClient caches signing keys in-process (Redis is the intended cross-worker
JWKS cache as the platform grows).
"""

from dataclasses import dataclass, field

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from .config import settings

_issuer = (
    f"https://cognito-idp.{settings.aws_region}.amazonaws.com/{settings.cognito_user_pool_id}"
)
_jwk_client = PyJWKClient(f"{_issuer}/.well-known/jwks.json")
_bearer = HTTPBearer(auto_error=True)


@dataclass
class CurrentUser:
    sub: str
    email: str | None
    groups: list[str] = field(default_factory=list)


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> CurrentUser:
    token = creds.credentials
    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=_issuer,
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {exc}"
        ) from exc

    return CurrentUser(
        sub=claims["sub"],
        email=claims.get("email"),
        groups=claims.get("cognito:groups", []) or [],
    )


def require_roles(*roles: str):
    """Dependency factory enforcing membership in at least one Cognito group."""

    async def _checker(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not set(roles).intersection(user.groups):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {', '.join(roles)}",
            )
        return user

    return _checker
