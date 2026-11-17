from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pa_shared import ok
from pa_shared.schedule import ScheduleSpec, next_daily
from sqlalchemy import text

from ..database import Database
from .common import ClientError, ClientRoute, Id, Input, User, not_found

router = APIRouter(route_class=ClientRoute)
FIELDS = """
    id, title, prompt, cron_expr AS "cronExpr", run_once_at AS "runAt", web_search AS "webSearch",
    target_session_id AS "sessionId", status, next_run_at AS "nextRunAt",
    fail_streak AS "failStreak", created_at AS "createdAt"
"""


@router.get("/schedule/list")
async def schedules(db: Database, user: User):
    return ok(
        {
            "items": [
                dict(row)
                for row in (
                    await db.execute(
                        text(f"""
        SELECT {FIELDS} FROM chat_schedule_task WHERE user_id = :user AND deleted_at IS NULL
        ORDER BY created_at DESC, id DESC LIMIT 200
    """),
                        {"user": user["id"]},
                    )
                ).mappings()
            ]
        }
    )


class ScheduleEdit(Input):
    id: Id
    schedule: ScheduleSpec | None = None
    paused: bool | None = None


@router.put("/schedule/edit")
async def edit(body: ScheduleEdit, db: Database, user: User):
    params = {"id": body.id, "user": user["id"]}
    task = (
        (
            await db.execute(
                text("""
        SELECT * FROM chat_schedule_task WHERE id = :id AND user_id = :user
        AND deleted_at IS NULL FOR UPDATE
    """),
                params,
            )
        )
        .mappings()
        .first()
    )
    if not task:
        raise not_found("Schedule not found")
    changes = {
        key: task[key]
        for key in (
            "title",
            "prompt",
            "cron_expr",
            "run_once_at",
            "web_search",
            "status",
            "fail_streak",
            "next_run_at",
        )
    }
    now = datetime.now(UTC)
    if spec := body.schedule:
        next_run_at = spec.next_run(now)
        if next_run_at is None:
            raise ClientError("Schedule time must be in the future")
        changes.update(
            title=spec.title,
            prompt=spec.prompt,
            cron_expr=spec.cron,
            run_once_at=spec.runAt,
            web_search=spec.webSearch,
            next_run_at=next_run_at,
        )
    if body.paused is not None:
        changes["status"] = "paused" if body.paused else "active"
        if not body.paused:
            changes["fail_streak"] = 0
            once = changes["run_once_at"]
            changes["next_run_at"] = next_daily(changes["cron_expr"], now)
            if once and once > now:
                changes["next_run_at"] = once
            if not changes["next_run_at"]:
                raise ClientError("Choose a future time to resume this schedule")
    row = (
        (
            await db.execute(
                text(f"""
        UPDATE chat_schedule_task SET title = :title, prompt = :prompt, cron_expr = :cron_expr,
            run_once_at = :run_once_at, web_search = :web_search, status = :status,
            fail_streak = :fail_streak, next_run_at = :next_run_at WHERE id = :id RETURNING {FIELDS}
    """),
                {**params, **changes},
            )
        )
        .mappings()
        .one()
    )
    await db.commit()
    return ok(dict(row))


@router.post("/schedule/{id}/runNow")
async def run_now(id: Id, db: Database, user: User):
    task = await db.scalar(
        text("""
        SELECT id FROM chat_schedule_task WHERE id = :id AND user_id = :user
            AND deleted_at IS NULL FOR UPDATE
    """),
        {"id": id, "user": user["id"]},
    )
    if task is None:
        raise not_found("Schedule not found")
    run_id = await db.scalar(
        text("""
        SELECT id FROM chat_schedule_log WHERE task_id = :id AND status IN ('queued', 'running')
        ORDER BY id LIMIT 1
    """),
        {"id": id},
    )
    if run_id is None:
        run_id = await db.scalar(
            text("""
            INSERT INTO chat_schedule_log (task_id, status) VALUES (:id, 'queued') RETURNING id
        """),
            {"id": id},
        )
    await db.commit()
    return ok({"runId": run_id})


@router.delete("/schedule/{id}")
async def delete(id: Id, db: Database, user: User):
    deleted = await db.scalar(
        text("""
        UPDATE chat_schedule_task SET deleted_at = now(), status = 'deleted', next_run_at = NULL
        WHERE id = :id AND user_id = :user AND deleted_at IS NULL RETURNING id
    """),
        {"id": id, "user": user["id"]},
    )
    if deleted is None:
        raise not_found("Schedule not found")
    await db.commit()
    return ok({"deleted": True})


@router.get("/activity/page")
async def activity(
    db: Database,
    user: User,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=100)] = 20,
):
    rows = (
        (
            await db.execute(
                text("""
        SELECT id, type, title, body, target_url AS "targetUrl", is_read AS "isRead",
               created_at AS "createdAt"
        FROM user_notification WHERE user_id = :user ORDER BY created_at DESC, id DESC
        LIMIT :limit OFFSET :offset
    """),
                {"user": user["id"], "limit": pageSize + 1, "offset": (page - 1) * pageSize},
            )
        )
        .mappings()
        .all()
    )
    return ok({"items": [dict(row) for row in rows[:pageSize]], "hasMore": len(rows) > pageSize})


@router.post("/activity/{id}/read")
async def mark_read(id: Id, db: Database, user: User):
    found = await db.scalar(
        text("""
        UPDATE user_notification SET is_read = TRUE WHERE id = :id AND user_id = :user RETURNING id
    """),
        {"id": id, "user": user["id"]},
    )
    if found is None:
        raise not_found("Activity item not found")
    await db.commit()
    return ok({"isRead": True})
