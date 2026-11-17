from hashlib import sha256
from typing import Annotated, Literal

from fastapi import APIRouter
from pa_shared import get_settings, ok
from pa_shared.security import encrypt_secret
from pydantic import Field
from sqlalchemy import text

from ..database import Database
from .common import ClientRoute, Input, User, unavailable

router = APIRouter(prefix="/push", route_class=ClientRoute)
DeviceToken = Annotated[str, Field(min_length=20, max_length=4096, pattern=r"^[!-~]+$")]


class TokenBody(Input):
    token: DeviceToken


class DeviceBody(TokenBody):
    platform: Literal["android", "ios"]
    previousToken: DeviceToken | None = None


def fingerprint(token: str) -> str:
    return sha256(token.encode()).hexdigest()


@router.get("/status")
async def status(user: User):
    return ok({"available": get_settings().fcm_enabled})


@router.post("/device/status")
async def device_status(body: TokenBody, db: Database, user: User):
    registered = await db.scalar(text("""
        SELECT EXISTS (SELECT 1 FROM push_device WHERE token_hash = :token AND user_id = :user)
    """), {"token": fingerprint(body.token), "user": user["id"]})
    return ok({"registered": registered})


@router.put("/device")
async def register(body: DeviceBody, db: Database, user: User):
    settings = get_settings()
    if not settings.fcm_enabled:
        raise unavailable("Mobile push is disabled")
    params = {
        "user": user["id"], "token": fingerprint(body.token), "platform": body.platform,
        "ciphertext": encrypt_secret(body.token, settings.key_encrypt_master),
    }
    if body.previousToken and body.previousToken != body.token:
        await db.execute(text("""
            DELETE FROM push_device WHERE user_id = :user AND token_hash = :previous
        """), {"user": user["id"], "previous": fingerprint(body.previousToken)})
    await db.execute(text("""
        INSERT INTO push_device (token_hash, user_id, token_ciphertext, platform)
        VALUES (:token, :user, :ciphertext, :platform)
        ON CONFLICT (token_hash) DO UPDATE SET user_id = excluded.user_id,
            token_ciphertext = excluded.token_ciphertext, platform = excluded.platform,
            updated_at = now()
    """), params)
    await db.execute(text("""
        DELETE FROM push_delivery d USING user_notification n
        WHERE d.token_hash = :token AND n.id = d.notification_id AND n.user_id <> :user
    """), params)
    await db.commit()
    return ok({"registered": True})


@router.delete("/device")
async def unregister(body: TokenBody, db: Database, user: User):
    await db.execute(text("""
        DELETE FROM push_device WHERE token_hash = :token AND user_id = :user
    """), {"token": fingerprint(body.token), "user": user["id"]})
    await db.commit()
    return ok({"registered": False})
