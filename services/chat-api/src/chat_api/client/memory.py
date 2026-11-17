from typing import Annotated

import httpx
from fastapi import APIRouter, Query
from pa_shared import ok
from pa_shared.model_gateway import GatewayError, embedding
from pydantic import StringConstraints
from sqlalchemy import text

from ..database import Database
from ..turns import AVAILABLE, bound_models
from .common import ClientRoute, Id, Input, User, not_found, unavailable

router = APIRouter(route_class=ClientRoute)
FIELDS = """
    id, content, category, status, source_turn_id AS "sourceTurnId",
    created_at AS "createdAt", updated_at AS "updatedAt"
"""


class MemoryEdit(Input):
    id: Id
    content: Annotated[str, StringConstraints(min_length=1, max_length=500, strip_whitespace=True)]


@router.get("/memory/list")
async def list_memory(
    db: Database,
    user: User,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=100)] = 20,
):
    rows = (
        await db.execute(
            text(f"""
        SELECT {FIELDS} FROM user_memory WHERE user_id = :user
        ORDER BY updated_at DESC, id DESC LIMIT :limit OFFSET :offset
    """),
            {"user": user["id"], "limit": pageSize, "offset": (page - 1) * pageSize},
        )
    ).mappings()
    return ok({"items": [dict(row) for row in rows], "page": page, "pageSize": pageSize})


@router.put("/memory/edit")
async def edit_memory(body: MemoryEdit, db: Database, user: User):
    content = body.content
    params = {"id": body.id, "user": user["id"], "content": content}
    if not await db.scalar(
        text("""
        SELECT id FROM user_memory WHERE id = :id AND user_id = :user
    """),
        params,
    ):
        raise not_found("Memory not found")
    bindings = await bound_models(db, ["embedding"])
    vector = None
    if model := bindings.get("embedding"):
        try:
            async with httpx.AsyncClient(trust_env=False) as http:
                vector = await embedding(http, model, content)
        except (GatewayError, httpx.HTTPError):
            raise unavailable("Embedding model is temporarily unavailable") from None
    row = (
        (
            await db.execute(
                text(f"""
        UPDATE user_memory SET content = :content, embedding = CAST(:embedding AS vector),
                               updated_at = now()
        WHERE id = :id AND user_id = :user RETURNING {FIELDS}
    """),
                {**params, "embedding": vector},
            )
        )
        .mappings()
        .one()
    )
    await db.commit()
    return ok(dict(row))


async def update_status(db: Database, user: int, memory_id: int, status: str):
    row = (
        (
            await db.execute(
                text(f"""
        UPDATE user_memory SET status = :status, updated_at = now()
        WHERE id = :id AND user_id = :user RETURNING {FIELDS}
    """),
                {"id": memory_id, "user": user, "status": status},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Memory not found")
    await db.commit()
    return ok(dict(row))


@router.post("/memory/{id}/undo")
async def undo(id: Id, db: Database, user: User):
    return await update_status(db, user["id"], id, "undone")


@router.post("/memory/{id}/restore")
async def restore(id: Id, db: Database, user: User):
    return await update_status(db, user["id"], id, "active")


@router.delete("/memory/{id}")
async def delete(id: Id, db: Database, user: User):
    removed = await db.scalar(
        text("""
        DELETE FROM user_memory WHERE id = :id AND user_id = :user RETURNING id
    """),
        {"id": id, "user": user["id"]},
    )
    if removed is None:
        raise not_found("Memory not found")
    await db.commit()
    return ok({"deleted": True})


@router.get("/model/listForSelection")
async def models(db: Database, user: User):
    rows = (
        await db.execute(
            text(f"""
        SELECT m.model_code AS "modelCode", m.display_name AS "displayName", m.capabilities,
               m.rate_in AS "rateIn", m.rate_cache AS "rateCache", m.rate_out AS "rateOut"
        FROM ai_model m JOIN ai_provider p ON p.id = m.provider_id
        WHERE {AVAILABLE} ORDER BY m.sort_order, m.id
    """),
            {
                "user": user["id"],
                "scope": "org" if user["user_type"] == "enterprise" else "internet",
            },
        )
    ).mappings()
    return ok({"items": [dict(row) for row in rows]})
