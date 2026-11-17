from typing import Annotated, Literal

from fastapi import APIRouter
from pa_shared import ok
from pa_shared.higress import sync_gateway
from pydantic import Field
from sqlalchemy import text

from .common import Admin, AdminRoute, Database, audit, session_factory

router = APIRouter(route_class=AdminRoute)
Rule = Literal["daily_free_amount", "search_cost", "sandbox_10s_cost", "image_cost"]
RuleValue = Annotated[int, Field(strict=True, ge=0, lt=2**31)]


async def point_rules(db: Database) -> dict[str, int]:
    return dict((await db.execute(text("SELECT rule_key, rule_value FROM point_rule"))).all())


@router.get("/points/rules")
async def get_rules(db: Database, admin: Admin):
    return ok(await point_rules(db))


@router.put("/points/rules")
async def update_rules(body: dict[Rule, RuleValue], db: Database, admin: Admin):
    for key, value in sorted(body.items()):
        await db.execute(
            text("""
            INSERT INTO point_rule (rule_key, rule_value) VALUES (:key, :value)
            ON CONFLICT (rule_key) DO UPDATE
            SET rule_value = excluded.rule_value, updated_at = now()
        """),
            {"key": key, "value": value},
        )
    await audit(db, admin, "points.rules", "point_rule", rules=body)
    rules = await point_rules(db)
    await db.commit()
    return ok(rules)


@router.get("/auditLog/page")
async def audit_page(
    db: Database,
    admin: Admin,
    page: Annotated[int, Field(ge=1, le=10000)] = 1,
    pageSize: Annotated[int, Field(ge=1, le=100)] = 20,
):
    rows = (
        await db.execute(
            text("""
        SELECT id, actor_type AS "actorType", actor_id AS "actorId", action,
               target_type AS "targetType", target_id AS "targetId", diff_json AS "diff",
               created_at AS "createdAt"
        FROM audit_log ORDER BY created_at DESC, id DESC LIMIT :size OFFSET :offset
    """),
            {"size": pageSize, "offset": (page - 1) * pageSize},
        )
    ).mappings()
    return ok({"items": [dict(row) for row in rows], "page": page, "pageSize": pageSize})


@router.get("/gateway/syncStatus")
async def gateway_status(db: Database, admin: Admin):
    pending = await db.scalar(text("SELECT revision <> synced_revision FROM gateway_sync_state"))
    return ok({"syncStatus": "pending_retry" if pending else "synced"})


@router.post("/gateway/sync")
async def gateway_sync(db: Database, admin: Admin):
    await db.execute(text("UPDATE gateway_sync_state SET revision = revision + 1 WHERE id"))
    await audit(db, admin, "gateway.sync", "gateway")
    await db.commit()
    return ok({"syncStatus": await sync_gateway(session_factory())})
