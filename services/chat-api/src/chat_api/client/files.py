import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter
from pa_shared import ok
from pa_shared.storage import MissingObject, object_operation, presign
from pydantic import Field
from sqlalchemy import text

from ..database import Database
from .common import ClientError, ClientRoute, Id, Input, User, not_found
from .sessions import owned_session

router = APIRouter(prefix="/file", route_class=ClientRoute)
MAX_BYTES = 50 * 1024 * 1024
UPLOAD_TTL = 600
MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/heic",
    "image/heif",
    "image/bmp",
    "image/tiff",
    "application/pdf",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


class UploadSpec(Input):
    sessionId: Id | None = None
    fileName: Annotated[str, Field(min_length=1, max_length=255)]
    mimeType: Annotated[str, Field(min_length=1, max_length=150)]
    fileSize: Annotated[int, Field(strict=True, gt=0, le=MAX_BYTES)]


class ConfirmUpload(Input):
    fileId: Id


def upload_name(value: str) -> str:
    # Object keys never contain client-provided directory components.
    name = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = re.sub(r"[^\w.-]", "_", name).strip(".")
    if not name:
        raise ClientError("File name is invalid")
    return name[:170]


@router.post("/presignUpload")
async def presign_upload(body: UploadSpec, db: Database, user: User):
    mime = body.mimeType.lower().strip()
    if mime not in MIME_TYPES:
        raise ClientError("File type is not supported")
    user_id = user["id"]
    if body.sessionId is not None:
        session = await owned_session(db, user_id, body.sessionId)
        if session["isTemporary"]:
            raise ClientError("Temporary sessions cannot persist uploaded files")
    name = upload_name(body.fileName)
    file_id = await db.scalar(text("SELECT nextval(pg_get_serial_sequence('user_file', 'id'))"))
    date_path = datetime.now(UTC).strftime("%Y-%m")
    key = f"uploads/{user_id}/{date_path}/{file_id}_{name}"
    await db.execute(
        text("""
        INSERT INTO file_upload (id, user_id, session_id, file_name, mime_type, file_size,
                                 s3_key, expires_at)
        VALUES (:id, :user, :session, :name, :mime, :size, :key,
                now() + make_interval(secs => CAST(:ttl AS double precision)))
    """),
        {
            "id": file_id,
            "user": user_id,
            "session": body.sessionId,
            "name": name,
            "mime": mime,
            "size": body.fileSize,
            "key": key,
            "ttl": UPLOAD_TTL,
        },
    )
    url = presign(
        "put_object",
        key,
        UPLOAD_TTL,
        ContentType=mime,
        ContentLength=body.fileSize,
        IfNoneMatch="*",
    )
    await db.commit()
    return ok(
        {
            "fileId": file_id,
            "uploadUrl": url,
            "method": "PUT",
            "expiresIn": UPLOAD_TTL,
            "headers": {
                "Content-Type": mime,
                "If-None-Match": "*",
                "Content-Length": str(body.fileSize),
            },
        }
    )


@router.post("/confirmUpload")
async def confirm_upload(body: ConfirmUpload, db: Database, user: User):
    params = {"id": body.fileId, "user": user["id"]}
    existing = await db.scalar(
        text("""
        SELECT id FROM user_file WHERE id = :id AND user_id = :user AND deleted_at IS NULL
    """),
        params,
    )
    if existing is not None:
        return ok({"fileId": existing})
    upload = (
        (
            await db.execute(
                text("""
        SELECT * FROM file_upload
        WHERE id = :id AND user_id = :user AND expires_at > now() FOR UPDATE
    """),
                params,
            )
        )
        .mappings()
        .first()
    )
    if upload is None:
        raise not_found("Upload not found or expired")
    if upload["session_id"] is not None:
        await owned_session(db, user["id"], upload["session_id"])
    try:
        metadata = await object_operation("head_object", upload["s3_key"])
    except MissingObject:
        raise ClientError("Upload has not reached object storage") from None
    if (metadata["ContentLength"], metadata.get("ContentType")) != (
        upload["file_size"],
        upload["mime_type"],
    ):
        raise ClientError("Object metadata does not match the upload")
    await db.execute(
        text("""
        INSERT INTO user_file (id, user_id, session_id, file_name, mime_type, file_size, s3_key)
        SELECT id, user_id, session_id, file_name, mime_type, file_size, s3_key FROM file_upload
        WHERE id = :id AND user_id = :user
    """),
        params,
    )
    await db.execute(text("DELETE FROM file_upload WHERE id = :id AND user_id = :user"), params)
    await db.commit()
    return ok({"fileId": body.fileId})
