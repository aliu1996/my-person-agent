import json
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response
from pa_shared import ErrorCode, ok
from pa_shared.storage import MissingObject, StorageError, get_object_bytes
from pydantic import BaseModel, Field
from sqlalchemy import text

from .common import Admin, AdminError, AdminRoute, Database, audit

router = APIRouter(prefix="/share", route_class=AdminRoute)
ShareId = Annotated[int, Field(gt=0, lt=2**63)]


class ShareReview(BaseModel):
    decision: Literal["confirm", "dismiss"]


@router.get("/reports")
async def reports(
    db: Database,
    admin: Admin,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
):
    rows = await db.execute(
        text("""
        SELECT r.id, r.share_id AS "shareId", r.reason, r.created_at AS "createdAt",
               s.review_blocked AS "reviewBlocked", c.title
        FROM chat_share_report r JOIN chat_share s ON s.id = r.share_id
        JOIN chat_session c ON (c.user_id, c.id) = (s.user_id, s.session_id)
        WHERE r.review_status = 'pending' ORDER BY r.created_at DESC, r.id DESC
        LIMIT 20 OFFSET :offset
    """),
        {"offset": (page - 1) * 20},
    )
    return ok({"items": [dict(row) for row in rows.mappings()], "page": page})


@router.put("/{share_id}/review")
async def review(share_id: ShareId, body: ShareReview, db: Database, admin: Admin):
    confirm = body.decision == "confirm"
    updated = await db.scalar(
        text("""
        UPDATE chat_share SET review_blocked = FALSE, is_revoked = is_revoked OR :confirm
        WHERE id = :id RETURNING id
    """),
        {"id": share_id, "confirm": confirm},
    )
    if updated is None:
        raise AdminError("Share not found", status=404)
    await db.execute(
        text("""
        UPDATE chat_share_report SET review_status = :status
        WHERE share_id = :id AND review_status = 'pending'
    """),
        {"id": share_id, "status": "confirmed" if confirm else "dismissed"},
    )
    await audit(db, admin, "share.review", "chat_share", share_id, decision=body.decision)
    await db.commit()
    return ok({"reviewed": True})


@router.get("/{share_id}/snapshot")
async def snapshot(share_id: ShareId, db: Database, admin: Admin, response: Response):
    key = await db.scalar(
        text("SELECT snapshot_s3_key FROM chat_share WHERE id = :id AND snapshot_ready"),
        {"id": share_id},
    )
    if key is None:
        raise AdminError("Share snapshot not found", status=404)
    try:
        document = json.loads(await get_object_bytes(key))
    except MissingObject:
        raise AdminError("Share snapshot not found", status=404) from None
    except (StorageError, ValueError):
        raise AdminError("Share snapshot unavailable", ErrorCode.INTERNAL_ERROR, 503) from None
    response.headers["Cache-Control"] = "no-store"
    return ok(document)
