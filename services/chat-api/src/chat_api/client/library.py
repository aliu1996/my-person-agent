from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pa_shared import ok
from pa_shared.storage import presign
from sqlalchemy import text

from ..database import Database
from .common import ClientError, ClientRoute, User, not_found
from .files import upload_name

router = APIRouter(prefix="/library", route_class=ClientRoute)
Category = Literal["all", "media", "files"]
ASSETS = """
    SELECT 'file' AS kind, id, file_name AS title, mime_type, file_size, session_id, s3_key,
           created_at, CASE WHEN mime_type LIKE 'image/%' THEN 'media' ELSE 'files' END AS category
    FROM user_file WHERE user_id = :user AND deleted_at IS NULL
    UNION ALL
    SELECT 'artifact', id, title, mime_type, file_size, session_id, s3_key, created_at, category
    FROM session_artifact WHERE user_id = :user AND deleted_at IS NULL
"""


def asset_identifier(value: str) -> tuple[str, int]:
    kind, _, code = value.partition("-")
    if kind not in ("file", "artifact") or not code.isascii() or not code.isdecimal():
        raise ClientError("Invalid asset identifier")
    if len(code) > 19 or not 0 < int(code) < 2**63:
        raise ClientError("Invalid asset identifier")
    return kind, int(code)


@router.get("/page")
async def page(
    db: Database,
    user: User,
    category: Category = "all",
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=100)] = 24,
):
    rows = (
        (
            await db.execute(
                text(f"""
        WITH assets AS ({ASSETS}), local_time AS (
            SELECT now() AT TIME ZONE 'Asia/Shanghai' AS current
        )
        SELECT kind || '-' || id AS id, kind, title, mime_type AS "mimeType", file_size AS size,
            session_id AS "sessionId", category, created_at AS "createdAt",
            CASE
                WHEN (created_at AT TIME ZONE 'Asia/Shanghai')::date = current::date THEN 'Today'
                WHEN created_at AT TIME ZONE 'Asia/Shanghai' >= date_trunc('week', current)
                THEN 'This week'
                ELSE 'Older'
            END AS "group"
        FROM assets CROSS JOIN local_time WHERE :category = 'all' OR category = :category
        ORDER BY created_at DESC, kind, id DESC LIMIT :limit OFFSET :offset
    """),
                {
                    "user": user["id"],
                    "category": category,
                    "limit": pageSize + 1,
                    "offset": (page - 1) * pageSize,
                },
            )
        )
        .mappings()
        .all()
    )
    return ok({"items": [dict(row) for row in rows[:pageSize]], "hasMore": len(rows) > pageSize})


@router.get("/{id}/preview")
async def preview(id: str, db: Database, user: User, download: bool = False):
    kind, number = asset_identifier(id)
    row = (
        (
            await db.execute(
                text(f"""
        WITH assets AS ({ASSETS}) SELECT * FROM assets WHERE kind = :kind AND id = :id
    """),
                {"user": user["id"], "kind": kind, "id": number},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise not_found("Asset not found")
    disposition = "attachment" if download or row["category"] != "media" else "inline"
    url = presign(
        "get_object",
        row["s3_key"],
        900,
        ResponseContentDisposition=(
            f"{disposition}; filename*=UTF-8''{encode_filename(row['title'])}"
        ),
    )
    return ok({"url": url, "expiresIn": 900, "title": row["title"], "mimeType": row["mime_type"]})


def encode_filename(title: str) -> str:
    from urllib.parse import quote

    return quote(upload_name(title), safe="")


@router.delete("/{id}")
async def delete(id: str, db: Database, user: User):
    kind, number = asset_identifier(id)
    table = "user_file" if kind == "file" else "session_artifact"
    removed = await db.scalar(
        text(f"""
        WITH removed AS (
            UPDATE {table} SET deleted_at = now()
            WHERE id = :id AND user_id = :user AND deleted_at IS NULL RETURNING s3_key
        ), queued AS (
            INSERT INTO storage_deletion (s3_key) SELECT s3_key FROM removed
            ON CONFLICT (s3_key) DO NOTHING
        )
        SELECT s3_key FROM removed
    """),
        {"id": number, "user": user["id"]},
    )
    if not removed:
        raise not_found("Asset not found")
    await db.commit()
    return ok({"deleted": True})
