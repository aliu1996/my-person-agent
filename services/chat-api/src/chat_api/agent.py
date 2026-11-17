import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Response
from pa_shared import ErrorCode, ok
from pa_shared.schedule import ScheduleSpec
from pa_shared.storage import MissingObject, get_object_bytes, object_operation, presign
from pydantic import Field
from sqlalchemy import text

from .client.common import Cache, ClientError, ClientRoute, Id, Input, User, active_user, not_found
from .client.files import MAX_BYTES, upload_name
from .client.sessions import owned_session
from .database import Database
from .turns import internal_access, lock_session

router = APIRouter(
    prefix="/internal/agent",
    dependencies=[Depends(internal_access)],
    route_class=ClientRoute,
    include_in_schema=False,
)
public = APIRouter(prefix="/api/chat", route_class=ClientRoute)
CONFIRM_TTL = 600
ARTIFACT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


async def live_turn(db: Database, redis: Cache, turn_id: UUID, *, write: bool = False):
    turn = (
        (
            await db.execute(
                text("""
        SELECT t.* FROM chat_turn t JOIN sys_user u ON u.id = t.user_id
        JOIN chat_session s ON (s.user_id, s.id) = (t.user_id, t.session_id)
        WHERE t.id = :id AND t.status = 'running' AND u.status = 'active'
          AND u.token_version = t.token_version AND s.deleted_at IS NULL
    """),
                {"id": turn_id},
            )
        )
        .mappings()
        .first()
    )
    if not turn:
        raise not_found("Active turn not found")
    if write:
        await active_user(db, turn["user_id"], turn["token_version"], write=True)
        await lock_session(db, turn["user_id"], turn["session_id"])
        status = await db.scalar(
            text("SELECT status FROM chat_turn WHERE id = :id FOR UPDATE"),
            {"id": turn_id},
        )
        if status != "running":
            raise not_found("Active turn not found")
    lock, stopped = await redis.mget(f"chat:lock:{turn['session_id']}", f"chat:stop:{turn_id}")
    if lock != str(turn_id).encode() or stopped:
        raise ClientError("Turn is no longer active", ErrorCode.SESSION_CONFLICT, 409)
    return turn


def confirm_key(id: UUID) -> str:
    return f"chat:confirm:{id}"


def remembered(user_id: int, session_id: int) -> str:
    return f"chat:confirm-schedule:{user_id}:{session_id}"


class Decision(Input):
    confirmId: UUID
    decision: Literal["allow", "deny"]
    rememberInSession: bool = False
    schedule: ScheduleSpec | None = None


@public.post("/confirm")
async def decide(body: Decision, db: Database, user: User, redis: Cache):
    key = confirm_key(body.confirmId)
    raw = await redis.get(key)
    if not raw:
        raise not_found("Confirmation expired")
    record = json.loads(raw)
    if record["userId"] != user["id"]:
        raise not_found("Confirmation expired")
    await owned_session(db, user["id"], record["sessionId"])
    if record["decision"] != "pending":
        if record["decision"] == body.decision:
            return ok({"decision": record["decision"]})
        raise ClientError("Confirmation is already decided", ErrorCode.SESSION_CONFLICT, 409)
    if body.schedule and body.schedule.next_run(datetime.now(UTC)) is None:
        raise ClientError("Choose a future time")
    record["decision"] = body.decision
    if body.schedule and body.decision == "allow":
        record["schedule"] = body.schedule.model_dump(mode="json")
    changed = await redis.eval(
        """
        if redis.call('GET', KEYS[1]) ~= ARGV[1]
            or redis.call('GET', KEYS[2]) ~= ARGV[3]
            or redis.call('EXISTS', KEYS[3]) ~= 0 then return 0 end
        redis.call('SET', KEYS[1], ARGV[2], 'KEEPTTL')
        if ARGV[4] == '1' then redis.call('SET', KEYS[4], 'allow', 'EX', 1800) end
        return 1
    """,
        4,
        key,
        f"chat:lock:{record['sessionId']}",
        f"chat:stop:{record['turnId']}",
        remembered(user["id"], record["sessionId"]),
        raw,
        json.dumps(record),
        record["turnId"],
        "1" if body.decision == "allow" and body.rememberInSession else "0",
    )
    if not changed:
        raise ClientError("Confirmation is no longer pending", ErrorCode.SESSION_CONFLICT, 409)
    return ok({"decision": body.decision})


@router.post("/{turn_id}/confirm")
async def request_confirmation(turn_id: UUID, body: ScheduleSpec, db: Database, redis: Cache):
    turn = await live_turn(db, redis, turn_id)
    if turn["is_temporary"]:
        raise ClientError("Temporary sessions cannot create schedules")
    if body.next_run(datetime.now(UTC)) is None:
        raise ClientError("Choose a future time")
    confirm_id = uuid4()
    record = {
        "confirmId": str(confirm_id),
        "turnId": str(turn_id),
        "userId": turn["user_id"],
        "sessionId": turn["session_id"],
        "kind": "schedule",
        "decision": "pending",
        "schedule": body.model_dump(mode="json"),
    }
    if await redis.get(remembered(turn["user_id"], turn["session_id"])) == b"allow":
        record["decision"] = "allow"
    await redis.set(confirm_key(confirm_id), json.dumps(record), ex=CONFIRM_TTL)
    return ok(record)


@router.get("/{turn_id}/confirm/{confirm_id}")
async def decision(turn_id: UUID, confirm_id: UUID, redis: Cache):
    raw = await redis.get(confirm_key(confirm_id))
    if not raw:
        return ok({"decision": "deny"})
    record = json.loads(raw)
    if record["turnId"] != str(turn_id):
        raise not_found("Confirmation expired")
    return ok({"decision": record["decision"]})


@router.post("/{turn_id}/schedule/{confirm_id}")
async def create_schedule(turn_id: UUID, confirm_id: UUID, db: Database, redis: Cache):
    turn = await live_turn(db, redis, turn_id, write=True)
    raw = await redis.get(confirm_key(confirm_id))
    record = json.loads(raw) if raw else {}
    if turn["is_temporary"] or record.get("turnId") != str(turn_id):
        raise not_found("Confirmation expired")
    if record["decision"] != "allow":
        raise ClientError("Schedule was not approved")
    params = {"id": confirm_id, "user": turn["user_id"]}
    existing = (
        (
            await db.execute(
                text("""
        SELECT id, next_run_at AS "nextRunAt", target_session_id AS "sessionId"
        FROM chat_schedule_task WHERE confirmation_id = :id AND user_id = :user
    """),
                params,
            )
        )
        .mappings()
        .first()
    )
    if existing:
        return ok(dict(existing))
    spec = ScheduleSpec.model_validate(record["schedule"])
    next_run_at = spec.next_run(datetime.now(UTC))
    if next_run_at is None:
        raise ClientError("Choose a future time")
    session = await db.scalar(
        text("""
        INSERT INTO chat_session (user_id, title) VALUES (:user, :title) RETURNING id
    """),
        {**params, "title": spec.title},
    )
    row = (
        (
            await db.execute(
                text("""
        INSERT INTO chat_schedule_task (confirmation_id, user_id, target_session_id, model_code,
            title, prompt, cron_expr, run_once_at, next_run_at, web_search)
        VALUES (:id, :user, :session, :model, :title, :prompt, :cron, :once, :next, :search)
        RETURNING id, next_run_at AS "nextRunAt", target_session_id AS "sessionId"
    """),
                {
                    **params,
                    "session": session,
                    "model": turn["model_code"],
                    "title": spec.title,
                    "prompt": spec.prompt,
                    "cron": spec.cron,
                    "once": spec.runAt,
                    "next": next_run_at,
                    "search": spec.webSearch,
                },
            )
        )
        .mappings()
        .one()
    )
    await db.commit()
    return ok(dict(row))


@router.get("/{turn_id}/attachments")
async def attachments(turn_id: UUID, db: Database, redis: Cache):
    turn = await live_turn(db, redis, turn_id)
    rows = (
        (
            await db.execute(
                text("""
        SELECT id, file_name AS name, mime_type AS "mimeType", file_size AS size
        FROM user_file WHERE user_id = :user AND id = ANY(CAST(:files AS bigint[]))
          AND deleted_at IS NULL ORDER BY id
    """),
                {"user": turn["user_id"], "files": turn["file_ids"]},
            )
        )
        .mappings()
        .all()
    )
    return ok({"items": [dict(row) for row in rows]})


@router.get("/{turn_id}/attachments/{file_id}")
async def attachment(turn_id: UUID, file_id: Id, db: Database, redis: Cache):
    turn = await live_turn(db, redis, turn_id)
    if file_id not in turn["file_ids"]:
        raise not_found("Attachment not found")
    key = await db.scalar(
        text("""
        SELECT s3_key FROM user_file WHERE user_id = :user AND id = :id AND deleted_at IS NULL
    """),
        {"user": turn["user_id"], "id": file_id},
    )
    if not key:
        raise not_found("Attachment not found")
    return Response(
        await get_object_bytes(key, max_bytes=MAX_BYTES), media_type="application/octet-stream"
    )


class Artifact(Input):
    name: Annotated[str, Field(min_length=1, max_length=255)]
    content: Annotated[str, Field(min_length=1, max_length=(MAX_BYTES + 2) // 3 * 4)]


@router.post("/{turn_id}/artifacts")
async def add_artifact(turn_id: UUID, body: Artifact, db: Database, redis: Cache):
    turn = await live_turn(db, redis, turn_id, write=True)
    if turn["is_temporary"]:
        raise ClientError("Temporary sessions cannot save artifacts")
    title = upload_name(body.name)
    mime = ARTIFACT_TYPES.get(PurePosixPath(title).suffix.lower())
    if not mime:
        raise ClientError("Unsupported output file type")
    try:
        content = base64.b64decode(body.content, validate=True)
    except ValueError:
        raise ClientError("Invalid artifact encoding") from None
    if not 0 < len(content) <= MAX_BYTES:
        raise ClientError("Artifact exceeds the size limit")
    digest = hashlib.sha256(content).hexdigest()
    key = f"artifacts/{turn['user_id']}/{turn_id}/{digest}/{title}"
    params = {
        "user": turn["user_id"],
        "session": turn["session_id"],
        "turn": turn_id,
        "message": turn["message_id"],
        "title": title,
        "mime": mime,
        "size": len(content),
        "category": "media" if mime.startswith("image/") else "files",
        "key": key,
    }
    artifact_id = await db.scalar(
        text("""
        SELECT id FROM session_artifact WHERE turn_id = :turn AND s3_key = :key
          AND deleted_at IS NULL
    """),
        params,
    )
    if artifact_id is None:
        await object_operation("put_object", key, Body=content, ContentType=mime)
        artifact_id = await db.scalar(
            text("""
            INSERT INTO session_artifact (user_id, session_id, turn_id, message_id,
                                          title, mime_type, file_size, category, s3_key)
            VALUES (:user, :session, :turn, :message, :title, :mime, :size, :category, :key)
            RETURNING id
        """),
            params,
        )
        await db.commit()
    return ok(
        {
            "artifactId": artifact_id,
            "assetId": f"artifact-{artifact_id}",
            "title": title,
            "category": params["category"],
            "mimeType": mime,
            "size": len(content),
            "previewUrl": presign("get_object", key, 900),
        }
    )


class ArchivedEvent(Input):
    event: Literal["tool", "artifact"]
    title: Annotated[str, Field(max_length=255)]
    status: Literal["done", "failed"] = "done"


class Archive(Input):
    events: Annotated[list[ArchivedEvent], Field(max_length=100)]


@router.post("/{turn_id}/archive")
async def archive(turn_id: UUID, body: Archive, db: Database, redis: Cache):
    turn = await live_turn(db, redis, turn_id, write=True)
    if turn["is_temporary"] or not body.events:
        return ok()
    key = f"sessions/{turn['user_id']}/{turn['session_id']}/events/{turn_id}.jsonl"
    content = "".join(event.model_dump_json() + "\n" for event in body.events).encode()
    await object_operation("put_object", key, Body=content, ContentType="application/x-ndjson")
    await db.execute(
        text("UPDATE chat_turn SET archive_key = :key WHERE id = :id"), {"key": key, "id": turn_id}
    )
    await db.commit()
    return ok()


@router.get("/{turn_id}/archive")
async def history(turn_id: UUID, db: Database, redis: Cache):
    turn = await live_turn(db, redis, turn_id)
    if turn["is_temporary"]:
        return ok({"items": []})
    key = await db.scalar(
        text("""
        SELECT archive_key FROM chat_turn WHERE user_id = :user AND session_id = :session
          AND id <> :id AND status = 'done' AND archive_key IS NOT NULL
        ORDER BY created_at DESC LIMIT 1
    """),
        {"user": turn["user_id"], "session": turn["session_id"], "id": turn_id},
    )
    try:
        data = await get_object_bytes(key, max_bytes=100000) if key else b""
    except MissingObject:
        data = b""
    return ok({"items": [json.loads(line) for line in data.splitlines() if line]})
