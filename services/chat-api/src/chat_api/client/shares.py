import json
import secrets
from typing import Annotated

from fastapi import APIRouter, Request
from pa_shared import get_settings, ok
from pa_shared.storage import MissingObject, get_object_bytes, object_operation
from pydantic import Field
from sqlalchemy import text

from ..database import Database
from .auth import private_hash
from .common import ClientError, ClientRoute, Id, Input, User, active_user, not_found
from .sessions import owned_session, read_branch

router = APIRouter(prefix="/chat/share", route_class=ClientRoute)
ShareToken = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{32,128}$")]

AVAILABLE_SHARE = """
FROM chat_share s
JOIN chat_session c ON (c.user_id, c.id) = (s.user_id, s.session_id)
JOIN sys_user u ON u.id = s.user_id
WHERE s.share_token = :token AND s.snapshot_ready AND NOT s.is_revoked
    AND (s.expire_at IS NULL OR s.expire_at > now())
    AND c.deleted_at IS NULL AND u.status = 'active'
"""


class ShareCreate(Input):
    sessionId: Id
    expireInDays: Annotated[int, Field(ge=1, le=30)] = 7


class ShareReport(Input):
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


@router.post("/add")
async def create_share(body: ShareCreate, db: Database, user: User):
    user_id = user["id"]
    session = await owned_session(db, user_id, body.sessionId)
    if session["isTemporary"]:
        raise ClientError("Temporary sessions cannot be shared")
    messages = await read_branch(db, body.sessionId, session["activeLeafMsgId"], public=True)
    snapshot = json.dumps(
        {"title": session["title"], "messages": messages}, ensure_ascii=False, default=str
    ).encode()
    token = secrets.token_urlsafe(32)
    key = f"shares/{token}.json"
    share = (
        await db.execute(
            text("""
        INSERT INTO chat_share (share_token, session_id, user_id, snapshot_s3_key, expire_at,
                                snapshot_ready)
        VALUES (:token, :session, :user, :key, now() + make_interval(days => :days), FALSE)
        RETURNING id, expire_at
    """),
            {
                "token": token,
                "session": body.sessionId,
                "user": user_id,
                "key": key,
                "days": body.expireInDays,
            },
        )
    ).one()
    # Reserve the object key durably before uploading. The worker collects abandoned drafts.
    await db.commit()
    await active_user(db, user_id, user["token_version"], write=True)
    await owned_session(db, user_id, body.sessionId)
    pending = await db.scalar(
        text("""
        SELECT id FROM chat_share WHERE id = :id AND NOT is_revoked FOR UPDATE
    """),
        {"id": share.id},
    )
    if pending is None:
        raise not_found("Share not found")
    await object_operation("put_object", key, Body=snapshot, ContentType="application/json")
    await db.execute(
        text("UPDATE chat_share SET snapshot_ready = TRUE WHERE id = :id"), {"id": share.id}
    )
    await db.commit()
    return ok({"token": token, "url": f"/api/chat/share/{token}", "expiresAt": share.expire_at})


@router.get("/{token}")
async def public_share(token: ShareToken, db: Database):
    key = await db.scalar(
        text(
            "SELECT s.snapshot_s3_key "
            + AVAILABLE_SHARE
            + " AND NOT s.review_blocked FOR SHARE OF s"
        ),
        {"token": token},
    )
    if key is None:
        raise not_found("Share not found or unavailable")
    try:
        return ok(json.loads(await get_object_bytes(key)))
    except MissingObject:
        raise not_found("Share snapshot not found") from None


@router.delete("/{token}")
async def revoke_share(token: ShareToken, db: Database, user: User):
    updated = await db.scalar(
        text("""
        UPDATE chat_share SET is_revoked = TRUE
        WHERE share_token = :token AND user_id = :user RETURNING id
    """),
        {"token": token, "user": user["id"]},
    )
    if updated is None:
        raise not_found("Share not found")
    await db.commit()
    return ok({"revoked": True})


@router.post("/{token}/report")
async def report_share(token: ShareToken, body: ShareReport, request: Request, db: Database):
    reason = body.reason.strip()
    if not reason:
        raise ClientError("Report reason must not be blank")
    share_id = await db.scalar(
        text("SELECT s.id " + AVAILABLE_SHARE + " FOR UPDATE OF s"), {"token": token}
    )
    if share_id is None:
        raise not_found("Share not found or unavailable")
    reporter = private_hash("share-report", request.client.host if request.client else "unknown")
    await db.execute(
        text("""
        INSERT INTO chat_share_report (share_id, reporter_hash, reason)
        VALUES (:id, :reporter, :reason) ON CONFLICT (share_id, reporter_hash) DO NOTHING
    """),
        {"id": share_id, "reporter": reporter, "reason": reason},
    )
    await db.execute(
        text("""
        UPDATE chat_share SET review_blocked = TRUE WHERE id = :id AND
            (SELECT count(*) FROM chat_share_report WHERE share_id = :id
                    AND review_status = 'pending') >= :threshold
    """),
        {"id": share_id, "threshold": get_settings().share_report_threshold},
    )
    await db.commit()
    return ok({"reported": True})
