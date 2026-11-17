import asyncio
from datetime import UTC, datetime
from typing import Annotated

import jwt
import pyotp
from cryptography.exceptions import InvalidTag
from fastapi import APIRouter
from pa_shared import get_settings, ok
from pa_shared.security import (
    create_jwt,
    decode_jwt,
    decrypt_secret,
    encrypt_secret,
    verify_password,
)
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import text

from .common import AdminRoute, Database, audit, unauthorized

router = APIRouter(prefix="/auth", route_class=AdminRoute)


class Login(BaseModel):
    username: Annotated[str, Field(min_length=1, max_length=128)]
    password: Annotated[SecretStr, Field(min_length=1, max_length=1024)]
    totpCode: Annotated[str | None, Field(pattern=r"^[0-9]{6}$")] = None


class BindTotp(BaseModel):
    setupToken: Annotated[SecretStr, Field(min_length=1, max_length=4096)]
    totpCode: Annotated[str, Field(pattern=r"^[0-9]{6}$")]


def matching_step(secret: str, code: str, last_step: int | None) -> int | None:
    totp = pyotp.TOTP(secret)
    current = int(datetime.now(UTC).timestamp()) // totp.interval
    for step in (current, current - 1, current + 1):
        if (last_step is None or step > last_step) and pyotp.utils.strings_equal(
            totp.generate_otp(step), code
        ):
            return step
    return None


async def finish_login(db: Database, admin_id: int, step: int, action: str):
    await db.execute(
        text("UPDATE admin_user SET last_login_at = now(), totp_last_step = :step WHERE id = :id"),
        {"step": step, "id": admin_id},
    )
    await audit(db, admin_id, action, "admin_user", admin_id, outcome="success")
    await db.commit()
    token = create_jwt(str(admin_id), get_settings().admin_jwt_secret, "admin", expires_hours=8)
    return ok({"adminJwt": token, "requireTotpSetup": False})


@router.post("/login")
async def login(body: Login, db: Database):
    settings = get_settings()
    row = (
        (
            await db.execute(
                text("SELECT * FROM admin_user WHERE username = :username FOR UPDATE"),
                {"username": body.username},
            )
        )
        .mappings()
        .first()
    )
    if (
        not row
        or row["status"] != "active"
        or not await asyncio.to_thread(
            verify_password, body.password.get_secret_value(), row["password_hash"]
        )
    ):
        await audit(db, None, "login", "admin_user", outcome="rejected")
        await db.commit()
        raise unauthorized()
    admin_id = row["id"]
    if row["totp_secret"] is None:
        secret = pyotp.random_base32()
        token = jwt.encode(
            {
                "sub": str(admin_id),
                "role": "admin_totp_setup",
                "secret": encrypt_secret(secret, settings.key_encrypt_master),
                "exp": int(datetime.now(UTC).timestamp()) + 300,
            },
            settings.admin_jwt_secret,
            algorithm="HS256",
        )
        await audit(db, admin_id, "login", "admin_user", admin_id, outcome="totp_setup_required")
        await db.commit()
        return ok(
            {
                "requireTotpSetup": True,
                "setupToken": token,
                "otpauthUri": pyotp.TOTP(secret).provisioning_uri(
                    body.username, "Personal Assistant"
                ),
            }
        )
    secret = decrypt_secret(row["totp_secret"], settings.key_encrypt_master)
    step = matching_step(secret, body.totpCode or "", row["totp_last_step"])
    if step is None:
        await audit(db, admin_id, "login", "admin_user", admin_id, outcome="rejected")
        await db.commit()
        raise unauthorized()
    return await finish_login(db, admin_id, step, "login")


@router.post("/bindTotp")
async def bind_totp(body: BindTotp, db: Database):
    settings = get_settings()
    try:
        claims = decode_jwt(
            body.setupToken.get_secret_value(), settings.admin_jwt_secret, "admin_totp_setup"
        )
        admin_id = int(claims["sub"])
        secret = decrypt_secret(claims["secret"], settings.key_encrypt_master)
    except (jwt.InvalidTokenError, InvalidTag, KeyError, TypeError, ValueError):
        await audit(db, None, "bind_totp", "admin_user", outcome="rejected")
        await db.commit()
        raise unauthorized() from None
    row = (
        (
            await db.execute(
                text("SELECT status, totp_secret FROM admin_user WHERE id = :id FOR UPDATE"),
                {"id": admin_id},
            )
        )
        .mappings()
        .first()
    )
    step = matching_step(secret, body.totpCode, None)
    if not row or row["status"] != "active" or row["totp_secret"] is not None or step is None:
        await audit(db, admin_id, "bind_totp", "admin_user", admin_id, outcome="rejected")
        await db.commit()
        raise unauthorized()
    await db.execute(
        text("UPDATE admin_user SET totp_secret = :secret WHERE id = :id"),
        {"secret": claims["secret"], "id": admin_id},
    )
    return await finish_login(db, admin_id, step, "bind_totp")
