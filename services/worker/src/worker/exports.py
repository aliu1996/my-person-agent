import asyncio
import json
from tempfile import SpooledTemporaryFile
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from arq import Retry
from pa_shared.storage import StorageError, object_operation
from sqlalchemy import text

EXPORT_QUERIES = {
    "sessions.jsonl": """
        SELECT id, bot_id, group_id, title, summary, model_code, active_leaf_msg_id,
               created_at, last_msg_at, deleted_at
        FROM chat_session WHERE user_id = :user AND NOT is_temporary ORDER BY id
    """,
    "messages.jsonl": """
        SELECT m.id, m.session_id, m.parent_id, m.role, m.content, m.created_at
        FROM chat_session c JOIN chat_message m ON m.session_id = c.id
        WHERE c.user_id = :user AND NOT c.is_temporary AND m.deleted_at IS NULL
            AND m.role IN ('user', 'assistant') ORDER BY m.session_id, m.created_at, m.id
    """,
    "memories.jsonl": """
        SELECT id, content, category, created_at, updated_at FROM user_memory
        WHERE user_id = :user AND status = 'active' ORDER BY id
    """,
    "files.jsonl": """
        SELECT id, session_id, file_name, mime_type, file_size, created_at FROM user_file
        WHERE user_id = :user AND deleted_at IS NULL ORDER BY id
    """,
}


async def export_user_data(ctx: dict, export_id: str) -> None:
    try:
        await build_export(ctx, UUID(export_id))
    except StorageError:
        raise Retry(defer=30) from None


async def build_export(ctx: dict, export_id: UUID) -> None:
    async with ctx["db"].begin() as db:
        params = {"id": export_id}
        user_id = await db.scalar(
            text("""
            SELECT u.id FROM user_data_export e JOIN sys_user u ON u.id = e.user_id
            WHERE e.id = :id AND u.status = 'active' FOR SHARE OF u
        """),
            params,
        )
        if user_id is None:
            return
        key = await db.scalar(
            text("""
            SELECT s3_key FROM user_data_export WHERE id = :id AND status = 'queued'
            FOR UPDATE
        """),
            params,
        )
        if key is None:
            return
        with SpooledTemporaryFile(max_size=8 * 1024 * 1024) as archive:
            with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zipped:
                for name, query in EXPORT_QUERIES.items():
                    with zipped.open(name, "w", force_zip64=True) as entry:
                        async with db.stream(text(query), {"user": user_id}) as result:
                            async for row in result.mappings():
                                line = json.dumps(dict(row), ensure_ascii=False, default=str)
                                entry.write((line + "\n").encode())
            size = archive.tell()
            archive.seek(0)
            upload = asyncio.create_task(
                object_operation(
                    "put_object",
                    key,
                    Body=archive,
                    ContentLength=size,
                    ContentType="application/zip",
                )
            )
            try:
                await asyncio.shield(upload)
            except asyncio.CancelledError:
                # The upload runs in a thread and still owns the file handle.
                await asyncio.gather(upload, return_exceptions=True)
                raise
        await db.execute(
            text("""
            UPDATE user_data_export SET status = 'ready',
                expires_at = clock_timestamp() + interval '24 hours' WHERE id = :id
        """),
            params,
        )
        await db.execute(
            text("""
            INSERT INTO user_notification (user_id, type, title, body, target_url)
            VALUES (:user, 'system', '数据导出已完成', '下载链接有效期为 24 小时。', :url)
        """),
            {"user": user_id, "url": f"/api/user/exportData/{export_id}"},
        )


async def recover_exports(ctx: dict) -> None:
    async with ctx["db"]() as db:
        queued = (
            (
                await db.execute(
                    text("""
            SELECT e.id FROM user_data_export e JOIN sys_user u ON u.id = e.user_id
            WHERE e.status = 'queued' AND u.status = 'active'
            ORDER BY e.created_at LIMIT 100
        """)
                )
            )
            .scalars()
            .all()
        )
    for export_id in queued:
        await ctx["redis"].enqueue_job(
            "export_user_data", str(export_id), _job_id=f"export:{export_id}"
        )
