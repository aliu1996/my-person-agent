import json
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pa_shared import ErrorCode, ok
from pydantic import BaseModel, Field, StringConstraints
from redis.exceptions import RedisError
from sqlalchemy import text

from ..client.common import Cache, Id
from .common import Admin, AdminError, AdminRoute, Database, audit

router = APIRouter(prefix="/moderation", route_class=AdminRoute)
TERMS_KEY = "chat:moderation:terms"


class Terms(BaseModel):
    items: Annotated[
        list[
            Annotated[
                str,
                StringConstraints(
                    strip_whitespace=True,
                    min_length=1,
                    max_length=120,
                ),
            ]
        ],
        Field(max_length=5000),
    ]


class Review(BaseModel):
    decision: Literal["false_positive", "confirmed"]


@router.get("/terms")
async def read_terms(admin: Admin, redis: Cache):
    try:
        return ok({"items": json.loads(await redis.get(TERMS_KEY) or "[]")})
    except (RedisError, ValueError):
        raise AdminError("Moderation terms unavailable", ErrorCode.INTERNAL_ERROR, 503) from None


@router.put("/terms")
async def put_terms(body: Terms, admin: Admin, db: Database, redis: Cache):
    items = list(dict.fromkeys(body.items))
    try:
        async with redis.pipeline(transaction=True) as tx:
            tx.set(TERMS_KEY, json.dumps(items, ensure_ascii=False))
            tx.publish(TERMS_KEY, "reload")
            await tx.execute()
    except RedisError:
        raise AdminError("Moderation terms unavailable", ErrorCode.INTERNAL_ERROR, 503) from None
    await audit(db, admin, "moderation.terms", "moderation", termCount=len(items))
    await db.commit()
    return ok({"items": items})


@router.get("/page")
async def page(
    admin: Admin,
    db: Database,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    status: Literal["pending", "false_positive", "confirmed"] = "pending",
):
    rows = (
        await db.execute(
            text("""
        SELECT id, user_id AS "userId", turn_id AS "turnId", direction, hit_layer AS "hitLayer",
               snippet, review_status AS "reviewStatus", created_at AS "createdAt"
        FROM moderation_log WHERE review_status = :status
        ORDER BY created_at DESC, id DESC LIMIT 20 OFFSET :offset
    """),
            {"status": status, "offset": (page - 1) * 20},
        )
    ).mappings()
    return ok({"items": [dict(row) for row in rows], "page": page})


@router.post("/{id}/review")
async def review(id: Id, body: Review, admin: Admin, db: Database):
    updated = await db.scalar(
        text("""
        UPDATE moderation_log SET review_status = :status, reviewer_id = :reviewer
        WHERE id = :id RETURNING id
    """),
        {"id": id, "status": body.decision, "reviewer": admin},
    )
    if updated is None:
        raise AdminError("Moderation entry not found", status=404)
    await audit(db, admin, "moderation.review", "moderation_log", id, decision=body.decision)
    await db.commit()
    return ok({"reviewed": True})
