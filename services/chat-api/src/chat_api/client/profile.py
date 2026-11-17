from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from arq.connections import RedisSettings, create_pool
from fastapi import APIRouter
from pa_shared import get_settings, ok
from pa_shared.points import refresh_point_account
from pa_shared.security import decrypt_secret
from pa_shared.storage import presign
from pydantic import Field
from redis.exceptions import RedisError
from sqlalchemy import text

from ..database import Database
from .common import ClientRoute, Input, User, not_found

router = APIRouter(route_class=ClientRoute)


class ProfileEdit(Input):
    nickname: Annotated[str | None, Field(max_length=80)] = None
    customInstructions: Annotated[str | None, Field(max_length=8000)] = None
    memoryEnabled: bool | None = None


def profile(user, balance: int) -> dict:
    phone = decrypt_secret(user["phone_ciphertext"], get_settings().key_encrypt_master)
    return {
        "id": user["id"],
        "phone": f"{phone[:3]}****{phone[-4:]}",
        "nickname": user["nickname"],
        "avatarUrl": user["avatar_url"],
        "userType": user["user_type"],
        "customInstructions": user["custom_instructions"],
        "memoryEnabled": user["memory_enabled"],
        "pointsBalance": balance,
    }


@router.get("/user/profile")
async def get_profile(db: Database, user: User):
    balance = await refresh_point_account(db, user["id"])
    await db.commit()
    return ok(profile(user, balance))


@router.put("/user/profile")
async def edit_profile(body: ProfileEdit, db: Database, user: User):
    values = body.model_dump(exclude_unset=True)
    account = (
        (
            await db.execute(
                text("""
        UPDATE sys_user SET nickname = :nickname, custom_instructions = :instructions,
            memory_enabled = :memory WHERE id = :id RETURNING *
    """),
                {
                    "nickname": values.get("nickname", user["nickname"]),
                    "instructions": values.get("customInstructions", user["custom_instructions"]),
                    "memory": user["memory_enabled"]
                    if body.memoryEnabled is None
                    else body.memoryEnabled,
                    "id": user["id"],
                },
            )
        )
        .mappings()
        .one()
    )
    balance = await refresh_point_account(db, user["id"])
    await db.commit()
    return ok(profile(account, balance))


@router.get("/points/summary")
async def summary(db: Database, user: User):
    user_id = {"id": user["id"]}
    balance = await refresh_point_account(db, user["id"])
    used = await db.scalar(
        text("""
        SELECT COALESCE(-sum(delta), 0) FROM point_ledger
        WHERE user_id = :id AND delta < 0 AND created_at >= (
            date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai')
    """),
        user_id,
    )
    await db.commit()
    return ok({"balance": balance, "usedToday": used})


@router.post("/user/deleteAccount")
async def delete_account(db: Database, user: User):
    due = await db.scalar(
        text("""
        UPDATE sys_user SET status = 'deleting', delete_at = now() + interval '7 days',
            token_version = token_version + 1 WHERE id = :id RETURNING delete_at
    """),
        {"id": user["id"]},
    )
    await db.commit()
    return ok({"deleteAt": due})


async def enqueue_export(export_id: UUID) -> None:
    settings = RedisSettings.from_dsn(get_settings().redis_url)
    settings.conn_timeout = 2
    settings.conn_retries = 0
    try:
        async with await create_pool(settings) as queue:
            await queue.enqueue_job(
                "export_user_data", str(export_id), _job_id=f"export:{export_id}"
            )
    except (RedisError, OSError):
        # The periodic worker also picks up queued rows if immediate dispatch fails.
        pass


@router.post("/user/exportData")
async def request_export(db: Database, user: User):
    user_id = user["id"]
    export_id = await db.scalar(
        text("""
        SELECT id FROM user_data_export WHERE user_id = :id AND status = 'queued'
    """),
        {"id": user_id},
    )
    if export_id is None:
        export_id = uuid4()
        await db.execute(
            text("""
            INSERT INTO user_data_export (id, user_id, s3_key) VALUES (:id, :user, :key)
        """),
            {"id": export_id, "user": user_id, "key": f"exports/{user_id}/{export_id}.zip"},
        )
    await db.commit()
    await enqueue_export(export_id)
    return ok({"exportId": export_id, "status": "queued"})


@router.get("/user/exportData/{export_id}")
async def export_status(export_id: UUID, db: Database, user: User):
    row = (
        (
            await db.execute(
                text("""
        SELECT status, s3_key, expires_at FROM user_data_export WHERE id = :id AND user_id = :user
    """),
                {"id": export_id, "user": user["id"]},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Export not found")
    result = {"exportId": export_id, "status": row["status"]}
    if row["status"] == "ready":
        ttl = int((row["expires_at"] - datetime.now(UTC)).total_seconds())
        if ttl > 0:
            result.update(
                downloadUrl=presign("get_object", row["s3_key"], min(ttl, 86400)),
                expiresAt=row["expires_at"],
            )
        else:
            result["status"] = "expired"
    return ok(result)
