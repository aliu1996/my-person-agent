from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pa_shared import get_settings, ok
from pa_shared.points import refresh_point_account
from pa_shared.security import decrypt_secret
from pydantic import BaseModel, Field
from sqlalchemy import text

from .common import Admin, AdminError, AdminRoute, Database, audit

router = APIRouter(prefix="/user", route_class=AdminRoute)
UserId = Annotated[int, Field(gt=0, lt=2**63)]


@router.get("/page")
async def users(
    admin: Admin,
    db: Database,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    userId: UserId | None = None,
):
    rows = (
        await db.execute(
            text("""
        SELECT u.id, u.phone_ciphertext, u.nickname, u.status, u.user_type AS "userType",
               u.created_at AS "createdAt", COALESCE(a.total_balance, 0) AS "balance"
        FROM sys_user u LEFT JOIN point_account a ON a.user_id = u.id
        WHERE CAST(:user AS bigint) IS NULL OR u.id = :user
        ORDER BY u.id DESC LIMIT 20 OFFSET :offset
    """),
            {"offset": (page - 1) * 20, "user": userId},
        )
    ).mappings()
    items = []
    for row in rows:
        item = dict(row)
        phone = decrypt_secret(item.pop("phone_ciphertext"), get_settings().key_encrypt_master)
        item["phone"] = f"{phone[:3]}****{phone[-4:]}"
        items.append(item)
    return ok({"items": items, "page": page})


class Status(BaseModel):
    status: Literal["active", "banned"]


@router.post("/{id}/ban")
async def set_status(id: UserId, body: Status, admin: Admin, db: Database):
    state = await db.scalar(
        text("""
        UPDATE sys_user SET status = :status,
            token_version = token_version + CASE WHEN status <> :status THEN 1 ELSE 0 END
        WHERE id = :id AND status IN ('active', 'banned') RETURNING status
    """),
        {"status": body.status, "id": id},
    )
    if state is None:
        raise AdminError("User not available", status=404)
    await audit(db, admin, "user.status", "sys_user", id, status=state)
    await db.commit()
    return ok({"status": state})


class Grant(BaseModel):
    requestId: UUID
    points: Annotated[int, Field(strict=True, ge=1, le=100000)]


@router.post("/{id}/points")
async def grant_points(id: UserId, body: Grant, admin: Admin, db: Database):
    if not await db.scalar(
        text("""
        SELECT id FROM sys_user WHERE id = :id AND status = 'active' FOR UPDATE
    """),
        {"id": id},
    ):
        raise AdminError("Active user not found", status=404)
    before = await refresh_point_account(db, id)
    params = {
        "id": id,
        "receipt": body.requestId,
        "points": body.points,
        "balance": before + body.points,
    }
    inserted = await db.scalar(
        text("""
        INSERT INTO point_ledger (user_id, turn_id, delta, balance_after, detail_json)
        VALUES (:id, :receipt, :points, :balance, '{"kind":"promo_grant"}'::jsonb)
        ON CONFLICT (turn_id) DO NOTHING RETURNING id
    """),
        params,
    )
    if inserted:
        await db.execute(
            text("""
            INSERT INTO point_bucket (user_id, bucket_type, priority, balance)
            VALUES (:id, 'promo', 2, :points)
        """),
            params,
        )
        balance = await refresh_point_account(db, id)
        await audit(db, admin, "user.grant", "sys_user", id, points=body.points)
    else:
        result = (
            await db.execute(
                text("""
            SELECT user_id, delta, detail_json, balance_after FROM point_ledger
            WHERE turn_id = :receipt
        """),
                params,
            )
        ).one()
        if (
            result.user_id != id
            or result.delta != body.points
            or result.detail_json.get("kind") != "promo_grant"
        ):
            raise AdminError("Request id is already used", status=409)
        balance = result.balance_after
    await db.commit()
    return ok({"balance": balance, "replayed": inserted is None})
