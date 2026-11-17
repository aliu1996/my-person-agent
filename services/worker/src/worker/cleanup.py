from pa_shared.points import refresh_point_account
from pa_shared.storage import MissingObject, StorageError, object_operation
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

BATCH_SIZE = 100


async def refresh_daily_points(ctx: dict) -> None:
    while True:
        async with ctx["db"].begin() as db:
            users = (
                (
                    await db.execute(
                        text("""
                SELECT a.user_id FROM point_account a JOIN sys_user u ON u.id = a.user_id
                WHERE u.status = 'active' AND EXISTS (
                    SELECT 1 FROM point_bucket b
                    WHERE b.user_id = a.user_id AND b.expire_at <= now()
                        AND (b.bucket_type = 'daily_free' OR b.balance > 0)
                )
                ORDER BY a.user_id LIMIT :limit FOR UPDATE OF a SKIP LOCKED
            """),
                        {"limit": BATCH_SIZE},
                    )
                )
                .scalars()
                .all()
            )
            for user_id in users:
                await refresh_point_account(db, user_id)
        if len(users) < BATCH_SIZE:
            return


async def queue_objects(db: AsyncSession, query: str, params: dict) -> None:
    await db.execute(
        text("INSERT INTO storage_deletion (s3_key) " + query + " ON CONFLICT (s3_key) DO NOTHING"),
        params,
    )


async def purge_due_accounts(ctx: dict) -> None:
    for _ in range(BATCH_SIZE):
        async with ctx["db"].begin() as db:
            user_id = await db.scalar(
                text("""
                SELECT id FROM sys_user WHERE status = 'deleting' AND delete_at <= now()
                ORDER BY delete_at LIMIT 1 FOR UPDATE SKIP LOCKED
            """)
            )
            if user_id is None:
                return
            await purge_user(db, user_id)


async def purge_user(db: AsyncSession, user_id: int) -> None:
    params = {"user": user_id}
    await queue_objects(
        db,
        """
        SELECT s3_key FROM user_file WHERE user_id = :user
        UNION SELECT s3_key FROM file_upload WHERE user_id = :user
        UNION SELECT s3_key FROM session_artifact WHERE user_id = :user
        UNION SELECT s3_key FROM user_data_export WHERE user_id = :user
        UNION SELECT snapshot_s3_key FROM chat_share WHERE user_id = :user
        UNION SELECT s3_key FROM user_skill WHERE owner_id = :user
    """,
        params,
    )
    await queue_session_objects(db, params)
    await db.execute(
        text("""
        DELETE FROM chat_schedule_log WHERE task_id IN
            (SELECT id FROM chat_schedule_task WHERE user_id = :user)
    """),
        params,
    )
    await db.execute(
        text("""
        DELETE FROM billing_payment WHERE order_id IN
            (SELECT id FROM billing_order WHERE user_id = :user)
    """),
        params,
    )
    await db.execute(
        text("""
        DELETE FROM audit_log WHERE (actor_type = 'user' AND actor_id = :user)
            OR (target_type = 'sys_user' AND target_id = CAST(CAST(:user AS bigint) AS text))
    """),
        params,
    )
    for table in (
        "chat_share",
        "file_upload",
        "user_file",
        "session_artifact",
        "chat_schedule_task",
        "chat_feedback",
        "user_notification",
        "user_data_export",
        "user_memory",
        "moderation_log",
        "billing_order",
        "point_ledger",
        "point_bucket",
        "point_account",
    ):
        await db.execute(text(f"DELETE FROM {table} WHERE user_id = :user"), params)

    await delete_session_messages(db, params)
    await db.execute(text("DELETE FROM chat_session WHERE user_id = :user"), params)
    for table, column, target, owner in (
        ("chat_session", "bot_id", "ai_bot", "owner_id"),
        ("chat_schedule_task", "bot_id", "ai_bot", "owner_id"),
        ("chat_session", "group_id", "chat_group", "user_id"),
    ):
        await db.execute(
            text(
                f"UPDATE {table} SET {column} = NULL WHERE {column} IN "
                f"(SELECT id FROM {target} WHERE {owner} = :user)"
            ),
            params,
        )
    for table, column in (
        ("ai_bot", "owner_id"),
        ("user_skill", "owner_id"),
        ("chat_group", "user_id"),
        ("sys_user", "id"),
    ):
        await db.execute(text(f"DELETE FROM {table} WHERE {column} = :user"), params)


async def queue_session_objects(db: AsyncSession, params: dict) -> None:
    scope = "c.user_id = :user" + (" AND c.id = :session" if "session" in params else "")
    await queue_objects(
        db,
        f"""
        SELECT m.s3_event_key FROM chat_session c JOIN chat_message m ON m.session_id = c.id
        WHERE {scope} AND m.s3_event_key IS NOT NULL
        UNION SELECT 'sessions/' || c.user_id || '/' || c.id || '/events.jsonl'
        FROM chat_session c WHERE {scope}
        UNION SELECT t.archive_key FROM chat_session c JOIN chat_turn t
            ON (t.user_id, t.session_id) = (c.user_id, c.id)
        WHERE {scope} AND t.archive_key IS NOT NULL
    """,
        params,
    )


async def delete_session_messages(db: AsyncSession, params: dict) -> None:
    scope = "c.user_id = :user" + (" AND c.id = :session" if "session" in params else "")
    await db.execute(
        text(f"""
        DELETE FROM chat_feedback f USING chat_message m, chat_session c
        WHERE f.message_id = m.id AND m.session_id = c.id AND {scope}
    """),
        params,
    )
    await db.execute(
        text(f"""
        DELETE FROM chat_message m USING chat_session c WHERE m.session_id = c.id AND {scope}
    """),
        params,
    )


async def purge_recycle_bin(ctx: dict) -> None:
    for _ in range(BATCH_SIZE):
        async with ctx["db"].begin() as db:
            session = (
                await db.execute(
                    text("""
                SELECT s.user_id, s.id FROM chat_session s JOIN sys_user u ON u.id = s.user_id
                WHERE s.deleted_at < now() - interval '30 days'
                ORDER BY s.deleted_at, s.id LIMIT 1 FOR UPDATE OF u SKIP LOCKED
            """)
                )
            ).first()
            if session is None:
                return
            params = {"user": session.user_id, "session": session.id}
            still_deleted = await db.scalar(
                text("""
                SELECT id FROM chat_session WHERE user_id = :user AND id = :session
                    AND deleted_at < now() - interval '30 days'
                FOR UPDATE
            """),
                params,
            )
            if still_deleted is None:
                continue
            await queue_session_objects(db, params)
            await queue_objects(
                db,
                """
                SELECT snapshot_s3_key FROM chat_share
                WHERE user_id = :user AND session_id = :session
            """,
                params,
            )
            for table, column in (
                ("user_file", "session_id"),
                ("session_artifact", "session_id"),
                ("file_upload", "session_id"),
                ("chat_schedule_task", "target_session_id"),
            ):
                await db.execute(
                    text(
                        f"UPDATE {table} SET {column} = NULL "
                        f"WHERE user_id = :user AND {column} = :session"
                    ),
                    params,
                )
            await delete_session_messages(db, params)
            for table, column in (
                ("chat_share", "session_id"),
                ("chat_session", "id"),
            ):
                await db.execute(
                    text(f"DELETE FROM {table} WHERE user_id = :user AND {column} = :session"),
                    params,
                )


async def queue_expired_objects(ctx: dict) -> None:
    async with ctx["db"].begin() as db:
        for table, key, condition, change in (
            (
                "file_upload",
                "s3_key",
                "expires_at < now() - interval '10 minutes'",
                "DELETE FROM file_upload WHERE id IN (SELECT id FROM due)",
            ),
            (
                "chat_share",
                "snapshot_s3_key",
                "((NOT snapshot_ready AND created_at < now() - interval '1 hour') "
                "OR (expire_at < now() AND snapshot_ready)) "
                "AND NOT EXISTS (SELECT 1 FROM chat_share_report r "
                "WHERE r.share_id = chat_share.id "
                "AND r.review_status = 'pending')",
                "DELETE FROM chat_share WHERE id IN (SELECT id FROM due)",
            ),
            (
                "user_data_export",
                "s3_key",
                "status = 'ready' AND expires_at <= now()",
                "UPDATE user_data_export SET status = 'expired' WHERE id IN (SELECT id FROM due)",
            ),
        ):
            await db.execute(
                text(f"""
                WITH due AS (
                    SELECT id FROM {table} WHERE {condition}
                    ORDER BY id LIMIT :limit FOR UPDATE SKIP LOCKED
                ), expired AS ({change} RETURNING {key})
                INSERT INTO storage_deletion (s3_key) SELECT {key} FROM expired
                ON CONFLICT (s3_key) DO NOTHING
            """),
                {"limit": BATCH_SIZE},
            )


async def delete_queued_objects(ctx: dict) -> None:
    for _ in range(BATCH_SIZE):
        async with ctx["db"].begin() as db:
            key = await db.scalar(
                text("""
                SELECT s3_key FROM storage_deletion WHERE run_after <= now()
                ORDER BY run_after, s3_key LIMIT 1 FOR UPDATE SKIP LOCKED
            """)
            )
            if key is None:
                return
            try:
                await object_operation("delete_object", key)
            except MissingObject:
                pass
            except StorageError:
                await db.execute(
                    text("""
                    UPDATE storage_deletion SET run_after = now() + make_interval(
                        secs => (1 << LEAST(attempts, 10))
                    ), attempts = LEAST(attempts + 1, 11) WHERE s3_key = :key
                """),
                    {"key": key},
                )
                continue
            await db.execute(text("DELETE FROM storage_deletion WHERE s3_key = :key"), {"key": key})
