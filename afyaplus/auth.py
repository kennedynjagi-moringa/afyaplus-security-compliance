"""JWT authentication reused unchanged from Weeks 6 and 7.

- `POST /token` exchanges username/password (OAuth2 password form) for a bearer token.
- `get_current_user` turns a missing, malformed, expired or forged token into 401.
- `require_scopes(...)` turns a valid token without the needed scope into 403.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

from afyaplus.models import TokenResponse
from afyaplus.settings import get_settings
from afyaplus.users import User, authenticate, get_user

log = logging.getLogger("api")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
router = APIRouter(tags=["auth"])

_UNAUTHORISED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or expired token. Request a new one from POST /token.",
    headers={"WWW-Authenticate": "Bearer"},
)


def create_access_token(user: User) -> tuple[str, int]:
    settings = get_settings()
    now = datetime.now(UTC)
    expires_in = settings.access_token_minutes * 60
    claims = {
        "sub": user.username,
        "role": user.role,
        "scopes": list(user.scopes),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(seconds=expires_in),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm)
    return token, expires_in


def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]) -> User:
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],  # pinned: rejects alg=none and algorithm swaps
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError:
        log.info("auth_rejected", extra={"reason": "expired"})
        raise _UNAUTHORISED from None
    except jwt.InvalidTokenError as exc:
        log.info("auth_rejected", extra={"reason": type(exc).__name__})
        raise _UNAUTHORISED from None
    user = get_user(claims["sub"])
    if user is None:  # account removed after the token was issued
        raise _UNAUTHORISED from None
    # Scopes come from the user store, not the token, so revoking a scope takes effect immediately.
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_scopes(*needed: str):
    def checker(user: CurrentUser) -> User:
        missing = [s for s in needed if s not in user.scopes]
        if missing:
            log.info("auth_forbidden", extra={"user": user.username, "missing_scopes": missing})
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' lacks the required scope(s): {', '.join(missing)}.",
            )
        return user

    return checker


@router.post("/token", response_model=TokenResponse, summary="Exchange credentials for a bearer token")
def login(form: Annotated[OAuth2PasswordRequestForm, Depends()]) -> TokenResponse:
    user = authenticate(form.username, form.password)
    if user is None:
        log.info("login_failed", extra={"username": form.username[:64]})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, expires_in = create_access_token(user)
    log.info("login_ok", extra={"user": user.username, "role": user.role})
    return TokenResponse(access_token=token, expires_in=expires_in, role=user.role, scopes=list(user.scopes))
