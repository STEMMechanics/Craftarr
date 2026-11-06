import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from fastapi import (
    Depends,
    HTTPException,
    status,
)

from fastapi.security import OAuth2PasswordBearer

from jose import JWTError, jwt

from pwdlib import PasswordHash

from sqlalchemy.orm import Session

from .database import get_db
from .models import User
from .config import SECRET_KEY
from .permissions import has_permission


ALGORITHM = "HS256"

TOKEN_HOURS = 12


password_hash = PasswordHash.recommended()

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/auth/login"
)

def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(
    password: str,
    hashed_password: str,
) -> bool:
    return password_hash.verify(
        password,
        hashed_password,
    )


def password_policy_error(password) -> str | None:
    if not isinstance(password, str):
        return "Password must be text."
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if len(password) > 128:
        return "Password must be no more than 128 characters."
    return None


def auth_session_version(user: User) -> str:
    value = f"{user.id}:{user.password_hash}:{user.auth_session_generation or 0}".encode("utf-8")
    return hmac.new(SECRET_KEY.encode("utf-8"), value, hashlib.sha256).hexdigest()


def rotate_auth_session(user: User) -> str:
    user.auth_session_generation = (user.auth_session_generation or 0) + 1
    return auth_session_version(user)


def is_auth_session_valid(value, user: User) -> bool:
    return isinstance(value, str) and hmac.compare_digest(value, auth_session_version(user))


def create_access_token(user: User) -> str:
    now = datetime.now(timezone.utc)

    payload = {
        "sub": str(user.id),

        "username": user.username,

        "role": user.role_name,
        "role_id": user.role_id,
        "auth_version": auth_session_version(user),

        "iat": now,

        "exp": now + timedelta(
            hours=TOKEN_HOURS
        ),
    }

    return jwt.encode(
        payload,
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):

    auth_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication token",
    )

    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
        )

        user_id = int(
            payload.get("sub")
        )

    except (
        JWTError,
        ValueError,
        TypeError,
    ):
        raise auth_error

    user = db.get(
        User,
        user_id,
    )

    if not user:
        raise auth_error

    if not user.enabled:
        raise auth_error

    if not is_auth_session_valid(payload.get("auth_version"), user):
        raise auth_error

    return user


def require_admin(
    user: User = Depends(
        get_current_user
    ),
):

    if not has_permission(user, "users.manage"):

        raise HTTPException(
            status_code=403,
            detail="Administrator access required",
        )

    return user


def require_users_manage(user: User = Depends(get_current_user)):
    if not has_permission(user, "users.manage"):
        raise HTTPException(status_code=403, detail="User management permission required")
    return user


def require_servers_create(user: User = Depends(get_current_user)):
    if not has_permission(user, "servers.create"):
        raise HTTPException(status_code=403, detail="Server creation permission required")
    return user


def require_servers_properties(user: User = Depends(get_current_user)):
    if not has_permission(user, "servers.properties"):
        raise HTTPException(status_code=403, detail="Server properties permission required")
    return user


def require_servers_delete(user: User = Depends(get_current_user)):
    if not has_permission(user, "servers.delete"):
        raise HTTPException(status_code=403, detail="Server deletion permission required")
    return user
