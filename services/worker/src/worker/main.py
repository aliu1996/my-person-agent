from datetime import UTC, datetime
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from arq.worker import func
from pa_shared import get_settings
from pa_shared.db import create_engine, create_session_factory
from pa_shared.higress import sync_gateway
from sqlalchemy import text

from .cleanup import (
    delete_queued_objects,
    purge_due_accounts,
    purge_recycle_bin,
    queue_expired_objects,
    refresh_daily_points,
)
from .exports import export_user_data, recover_exports
from .memory import extract_memory, recover_memory
from .schedules import dispatch_schedules, run_schedule


async def ping_task(ctx: dict[str, Any]) -> str:
    return "pong"


async def startup(ctx: dict[str, Any]) -> None:
    ctx["engine"] = create_engine()
    ctx["db"] = create_session_factory(ctx["engine"])


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["engine"].dispose()


async def retry_gateway(ctx: dict[str, Any]) -> str:
    return await sync_gateway(ctx["db"])


async def extend_message_partitions(ctx: dict[str, Any]) -> None:
    now = datetime.now(UTC)
    async with ctx["db"].begin() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(683024015)"))
        for offset in range(4):
            year, month = divmod(now.year * 12 + now.month - 1 + offset, 12)
            next_year, next_month = divmod(year * 12 + month + 1, 12)
            await db.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS chat_message_{year}_{month + 1:02} "
                    f"PARTITION OF chat_message "
                    f"FOR VALUES FROM ('{year}-{month + 1:02}-01 00:00:00+00') "
                    f"TO ('{next_year}-{next_month + 1:02}-01 00:00:00+00')"
                )
            )


class WorkerSettings:
    functions = [
        ping_task,
        func(export_user_data, keep_result=0, timeout=600),
        func(extract_memory, keep_result=0, timeout=60),
        func(run_schedule, keep_result=0, timeout=850),
    ]
    cron_jobs = [
        cron(retry_gateway, second=0, run_at_startup=True, timeout=50),
        cron(extend_message_partitions, day=25, hour=1, minute=0, second=0, run_at_startup=True),
        cron(recover_exports, second=5, run_at_startup=True),
        cron(recover_memory, second={8, 23, 38, 53}, run_at_startup=True),
        cron(dispatch_schedules, second={12, 27, 42, 57}, run_at_startup=True),
        cron(refresh_daily_points, hour=19, minute=0, second=0, timeout=3600),
        cron(purge_due_accounts, minute={0, 15, 30, 45}, second=10, timeout=120),
        cron(purge_recycle_bin, hour=20, minute=0, second=0, timeout=120),
        cron(queue_expired_objects, second=20),
        cron(delete_queued_objects, second=30, timeout=120),
    ]
    on_startup = startup
    on_shutdown = shutdown
    timezone = UTC
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
