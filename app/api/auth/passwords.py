"""
Password hashing and password-reset token helpers.
"""

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import HTTPException, status

from src.config.settings import Settings

_PASSWORD_HASH_SCHEME = "pbkdf2_sha256"
_PASSWORD_RESET_TOKEN_BYTES = 32


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_password(password: str) -> None:
    if len(password) < Settings.PASSWORD_MIN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Password must be at least {Settings.PASSWORD_MIN_LENGTH} "
                "characters long"
            ),
        )


def hash_password(password: str) -> str:
    validate_password(password)

    salt = secrets.token_bytes(16)
    derived_key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        Settings.PASSWORD_HASH_ITERATIONS,
    )
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    key_b64 = base64.urlsafe_b64encode(derived_key).decode("ascii")
    return (
        f"{_PASSWORD_HASH_SCHEME}${Settings.PASSWORD_HASH_ITERATIONS}$"
        f"{salt_b64}${key_b64}"
    )


def verify_password(password: str, stored_hash: str) -> bool:
    parts = stored_hash.split("$")
    if len(parts) != 4:
        raise ValueError("Stored password hash has invalid format")

    scheme, iteration_str, salt_b64, expected_key_b64 = parts
    if scheme != _PASSWORD_HASH_SCHEME:
        raise ValueError(f"Unsupported password hash scheme: {scheme}")

    iterations = int(iteration_str)
    salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
    expected_key = base64.urlsafe_b64decode(expected_key_b64.encode("ascii"))
    candidate_key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(candidate_key, expected_key)


def generate_password_reset_token() -> str:
    return secrets.token_urlsafe(_PASSWORD_RESET_TOKEN_BYTES)


def hash_password_reset_token(token: str) -> str:
    secret = Settings.JWT_SECRET.encode("utf-8")
    return hmac.new(secret, token.encode("utf-8"), hashlib.sha256).hexdigest()


def password_reset_expires_at(
    now: datetime | None = None,
) -> datetime:
    issued_at = now or datetime.now(timezone.utc)
    return issued_at + timedelta(minutes=Settings.PASSWORD_RESET_TOKEN_EXPIRY_MINUTES)


def build_password_reset_url(token: str) -> str:
    base = Settings.FRONTEND_URL.rstrip("/")
    path = Settings.PASSWORD_RESET_PATH
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path}?{urlencode({'token': token})}"
