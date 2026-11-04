"""Authentication input and response contracts; passwords never serialize into responses."""

from typing import Annotated
from uuid import UUID

from pydantic import Field, SecretStr, field_validator

from spm_api.errors import Model

Password = Annotated[SecretStr, Field(min_length=1, max_length=200)]


def strong_password(value: SecretStr) -> SecretStr:
    password = value.get_secret_value()
    common = {"password1234", "password12345", "password123456", "123456789012", "qwerty123456"}
    if len(password) < 12 or len(set(password)) < 4 or password.casefold() in common:
        raise ValueError("Use a unique passphrase of at least 12 characters")
    return value


class SetupStatus(Model):
    initialized: bool
    requires_token: bool


class SetupInitialize(Model):
    setup_token: Annotated[SecretStr, Field(min_length=12, max_length=200)]
    password: Password

    _strong = field_validator("password")(strong_password)


class Login(Model):
    password: Password


class ChangePassword(Model):
    old_password: Password
    new_password: Password

    _strong = field_validator("new_password")(strong_password)


class Owner(Model):
    id: UUID
    created_at: str
    is_active: bool


class Session(Model):
    id: UUID
    created_at: str
    last_seen_at: str
    expires_at: str
    revoked_at: str | None
    current: bool


class SessionPage(Model):
    items: list[Session]
    next_cursor: str | None = None


class CsrfToken(Model):
    csrf_token: str
    expires_at: str
