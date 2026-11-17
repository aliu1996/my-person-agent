import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import jwt
from pa_shared import ErrorCode, get_settings
from pa_shared.internal import internal_headers
from pa_shared.schedule import next_daily
from sqlalchemy import text


async def dispatch_schedules(ctx: dict) -> None:
    now = datetime.now(UTC)
    async with ctx["db"].begin() as db:
        due = (
            await db.execute(
                text("""
            SELECT t.id, t.cron_expr, t.next_run_at FROM chat_schedule_task t
            WHERE t.status = 'active' AND t.deleted_at IS NULL AND t.next_run_at <= :now
            AND NOT EXISTS (SELECT 1 FROM chat_schedule_log l WHERE l.task_id = t.id
                AND l.status IN ('queued', 'running'))
            ORDER BY t.next_run_at, t.id LIMIT 100 FOR UPDATE OF t SKIP LOCKED
        """),
                {"now": now},
            )
        ).all()
        for task in due:
            await db.execute(
                text("""
                INSERT INTO chat_schedule_log (task_id, scheduled_at, status)
                VALUES (:task, :occurrence, 'queued') ON CONFLICT DO NOTHING
            """),
                {"task": task.id, "occurrence": task.next_run_at},
            )
            await db.execute(
                text("""
                UPDATE chat_schedule_task SET next_run_at = :next WHERE id = :id
            """),
                {"id": task.id, "next": next_daily(task.cron_expr, now)},
            )
        jobs = (
            await db.scalars(
                text("""
            SELECT id FROM chat_schedule_log
            WHERE status = 'queued' OR (status = 'running' AND lease_until <= :now)
            ORDER BY id LIMIT 100
        """),
                {"now": now},
            )
        ).all()
    for id in jobs:
        await ctx["redis"].enqueue_job("run_schedule", id, _job_id=f"schedule:{id}")


async def claim(ctx: dict, id: int) -> dict | None:
    now = datetime.now(UTC)
    async with ctx["db"].begin() as db:
        user = (
            await db.execute(
                text("""
            SELECT u.id, u.status, u.token_version FROM chat_schedule_log l
            JOIN chat_schedule_task t ON t.id = l.task_id
            JOIN sys_user u ON u.id = t.user_id
            WHERE l.id = :id FOR UPDATE OF u
        """),
                {"id": id},
            )
        ).first()
        if not user:
            return None
        row = (
            (
                await db.execute(
                    text("""
            SELECT t.id AS task_id, t.title, t.prompt, t.model_code, t.target_session_id,
                t.web_search, t.status AS task_status, t.deleted_at, l.turn_id, l.scheduled_at
            FROM chat_schedule_log l JOIN chat_schedule_task t ON t.id = l.task_id
            WHERE l.id = :id AND (l.status = 'queued'
                OR (l.status = 'running' AND l.lease_until <= :now))
            FOR UPDATE OF t, l
        """),
                    {"id": id, "now": now},
                )
            )
            .mappings()
            .first()
        )
        if not row:
            return None
        if (
            user.status != "active"
            or row["deleted_at"]
            or (row["scheduled_at"] is not None and row["task_status"] != "active")
        ):
            await db.execute(
                text("""
                UPDATE chat_schedule_log SET status = 'cancelled', finished_at = now()
                WHERE id = :id
            """),
                {"id": id},
            )
            return None
        if row["turn_id"]:
            receipt = await db.scalar(
                text("SELECT result FROM chat_turn WHERE id = :turn"), {"turn": row["turn_id"]}
            )
            if receipt:
                return {**row, "receipt": receipt}
        session = await db.scalar(
            text("""
            SELECT id FROM chat_session WHERE user_id = :user AND id = :id
                AND deleted_at IS NULL AND NOT is_temporary
        """),
            {"user": user.id, "id": row["target_session_id"]},
        )
        if session is None:
            session = await db.scalar(
                text("""
                INSERT INTO chat_session (user_id, title) VALUES (:user, :title) RETURNING id
            """),
                {"user": user.id, "title": row["title"]},
            )
            await db.execute(
                text("""
                UPDATE chat_schedule_task SET target_session_id = :session WHERE id = :task
            """),
                {"session": session, "task": row["task_id"]},
            )
        turn_id = uuid4()
        await db.execute(
            text("""
            UPDATE chat_schedule_log SET status = 'running', lease_until = :lease, turn_id = :turn
            WHERE id = :id
        """),
            {"id": id, "lease": now + timedelta(minutes=15), "turn": turn_id},
        )
        return {
            **row,
            "turn_id": turn_id,
            "target_session_id": session,
            "user_id": user.id,
            "version": user.token_version,
        }


async def generate(job: dict) -> tuple[str, int]:
    settings = get_settings()
    now = datetime.now(UTC)
    jwt_token = jwt.encode(
        {
            "sub": str(job["user_id"]),
            "role": "user",
            "ver": job["version"],
            "iat": now,
            "exp": now + timedelta(minutes=20),
        },
        settings.user_jwt_secret,
        algorithm="HS256",
    )
    url = settings.chat_stream_url.rstrip("/") + f"/internal/scheduled/send/{job['turn_id']}"
    headers = {**internal_headers(), "Authorization": f"Bearer {jwt_token}"}
    payload = {
        "sessionId": job["target_session_id"],
        "content": job["prompt"],
        "webSearch": job["web_search"],
        "modelCode": job["model_code"],
    }
    async with httpx.AsyncClient(trust_env=False, timeout=35) as http:
        async with http.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code == 402:
                return "skipped_no_points", 0
            response.raise_for_status()
            event = ""
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event = line.removeprefix("event:").strip()
                elif line.startswith("data:") and event in ("done", "error"):
                    result = json.loads(line.removeprefix("data:"))
                    if event == "done" and result.get("status") == "done":
                        return "done", result.get("pointCost", 0)
                    if result.get("code") == ErrorCode.INSUFFICIENT_POINTS:
                        return "skipped_no_points", 0
                    return "failed", 0
    return "failed", 0


async def run_schedule(ctx: dict, id: int) -> None:
    job = await claim(ctx, id)
    if job is None:
        return
    if receipt := job.get("receipt"):
        status = "done" if receipt["status"] == "done" else "failed"
        charge = receipt.get("pointCost", 0)
    else:
        try:
            status, charge = await generate(job)
        except (httpx.HTTPError, ValueError):
            # A lost response can still have a durable settlement.
            async with ctx["db"]() as db:
                row = (
                    await db.execute(
                        text("""
                    SELECT status, result FROM chat_turn WHERE id = :turn
                """),
                        {"turn": job["turn_id"]},
                    )
                ).first()
            if row and row.status == "running":
                return
            receipt = row.result if row else None
            status = "done" if receipt and receipt["status"] == "done" else "failed"
            charge = receipt.get("pointCost", 0) if receipt else 0
    await record_outcome(ctx, id, job, status, charge)


async def record_outcome(ctx: dict, id: int, job: dict, status: str, charge: int) -> None:
    async with ctx["db"].begin() as db:
        task = (
            await db.execute(
                text("""
            SELECT id, user_id, fail_streak, status, cron_expr, next_run_at, target_session_id
            FROM chat_schedule_task WHERE id = :id FOR UPDATE
        """),
                {"id": job["task_id"]},
            )
        ).first()
        if task is None:
            return
        settled = await db.scalar(
            text("""
            UPDATE chat_schedule_log SET status = :status, point_cost = :cost,
                lease_until = NULL, finished_at = now(),
                error_msg = CASE WHEN :status = 'failed' THEN 'Generation failed' ELSE NULL END
            WHERE id = :id AND turn_id = :turn AND status = 'running' RETURNING id
        """),
            {"status": status, "cost": charge, "id": id, "turn": job["turn_id"]},
        )
        if settled is None or task.status == "deleted":
            return
        streak = task.fail_streak + 1 if status == "failed" else 0
        state = task.status
        if state == "active":
            if streak >= 3:
                state = "auto_paused"
            elif task.cron_expr is None and task.next_run_at is None:
                state = "completed"
        await db.execute(
            text("""
            UPDATE chat_schedule_task SET fail_streak = :streak, status = :status WHERE id = :id
        """),
            {"streak": streak, "status": state, "id": task.id},
        )
        descriptions = {
            "done": "结果已写入会话",
            "failed": "生成失败",
            "skipped_no_points": "积分不足，已跳过",
        }
        notice_id = await db.scalar(
            text("""
            INSERT INTO user_notification (user_id, type, title, body, target_url)
            VALUES (:user, :type, :title, :body, :url)
            RETURNING id
        """),
            {
                "user": task.user_id,
                "type": "schedule_" + status,
                "title": job["title"],
                "body": "连续失败三次，已自动暂停"
                if state == "auto_paused"
                else descriptions[status],
                "url": f"/chat/{task.target_session_id}" if task.target_session_id else None,
            },
        )
        if get_settings().fcm_enabled:
            await db.execute(text("""
                INSERT INTO push_delivery (notification_id, token_hash)
                SELECT :notice, token_hash FROM push_device WHERE user_id = :user
                ON CONFLICT DO NOTHING
            """), {"notice": notice_id, "user": task.user_id})
