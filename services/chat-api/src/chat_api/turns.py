import json
import math
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from arq.connections import RedisSettings, create_pool
from fastapi import APIRouter, Depends, Header
from pa_shared import ErrorCode, get_settings, ok
from pa_shared.internal import INTERNAL_TOKEN_HEADER, valid_internal_token
from pa_shared.points import check_points_threshold, settle_turn_points
from pa_shared.turns import FinishTurn, Id, PrepareTurn
from pydantic import BaseModel, Field
from redis.exceptions import RedisError
from sqlalchemy import text

from .client.common import ClientError, ClientRoute, User, active_user, not_found, unavailable
from .client.sessions import owned_session
from .database import Database


def internal_access(token: Annotated[str, Header(alias=INTERNAL_TOKEN_HEADER)] = "") -> None:
    if not valid_internal_token(token):
        raise ClientError("Invalid service credentials", ErrorCode.UNAUTHORIZED, 401)


router = APIRouter(
    prefix="/internal/chat",
    dependencies=[Depends(internal_access)],
    route_class=ClientRoute,
    include_in_schema=False,
)

AVAILABLE = """
    p.is_enabled AND (m.status = 'online' OR
        (m.status = 'canary' AND mod(CAST(:user AS bigint), 100) < m.canary_ratio))
    AND (m.visible_scope = 'all' OR m.visible_scope = :scope)
"""


async def bound_models(db: Database, purposes: list[str]) -> dict[str, str]:
    return dict(
        (
            await db.execute(
                text("""
        SELECT b.purpose, m.model_code FROM ai_model_binding b
        JOIN ai_model m ON m.model_code = b.model_code JOIN ai_provider p ON p.id = m.provider_id
        WHERE b.purpose = ANY(CAST(:purposes AS text[])) AND m.status <> 'offline' AND p.is_enabled
    """),
                {"purposes": purposes},
            )
        ).all()
    )


@router.get("/access/{session_id}")
async def access(session_id: Id, db: Database, user: User):
    await owned_session(db, user["id"], session_id)
    return ok()


async def get_model(db: Database, code: str | None, user):
    return (
        (
            await db.execute(
                text(f"""
        SELECT m.model_code, m.max_output, m.context_length, m.fallback_model_code,
               m.rate_in, m.rate_cache, m.rate_out, ({AVAILABLE}) AS available
        FROM ai_model m JOIN ai_provider p ON p.id = m.provider_id
        WHERE m.model_code = :code AND (m.visible_scope = 'all' OR m.visible_scope = :scope)
    """),
                {
                    "code": code,
                    "user": user["id"],
                    "scope": "org" if user["user_type"] == "enterprise" else "internet",
                },
            )
        )
        .mappings()
        .first()
    )


async def recent_messages(db: Database, session: int, leaf: int | None, budget: int) -> list:
    if leaf is None:
        return []
    rows = (
        await db.execute(
            text("""
        WITH RECURSIVE branch AS (
            SELECT id, parent_id, role, content, status, 0 AS depth FROM chat_message
            WHERE session_id = :session AND id = :leaf AND deleted_at IS NULL
            UNION ALL
            SELECT p.id, p.parent_id, p.role, p.content, p.status, b.depth + 1
            FROM branch b JOIN chat_message p ON p.id = b.parent_id
            WHERE p.session_id = :session AND p.deleted_at IS NULL AND b.depth < 23
        )
        SELECT id, role, status, left(content, :budget) AS content
        FROM branch ORDER BY depth
    """),
            {"session": session, "leaf": leaf, "budget": budget},
        )
    ).all()
    if not rows:
        raise not_found("Parent message not found in this session")
    messages = []
    for row in rows:
        if not row.content or row.role not in ("user", "assistant"):
            continue
        if row.status not in ("done", "stopped"):
            continue
        if budget <= 0:
            break
        messages.append({"role": row.role, "content": row.content[:budget]})
        budget -= len(row.content)
    return list(reversed(messages))


async def lock_session(db: Database, user: int, session: int):
    row = (
        (
            await db.execute(
                text("""
        SELECT s.*, b.default_model_code, b.system_prompt
        FROM chat_session s LEFT JOIN ai_bot b ON b.id = s.bot_id AND b.deleted_at IS NULL
        WHERE s.user_id = :user AND s.id = :session AND s.deleted_at IS NULL FOR UPDATE OF s
    """),
                {"user": user, "session": session},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Session not found")
    return row


@router.post("/prepare")
async def prepare(body: PrepareTurn, db: Database, user: User):
    session = await lock_session(db, user["id"], body.sessionId)
    if not await check_points_threshold(db, user["id"]):
        await db.commit()
        raise ClientError("Insufficient points", ErrorCode.INSUFFICIENT_POINTS, 402)
    if body.fileIds:
        files = set(
            (
                await db.scalars(
                    text("""
            SELECT id FROM user_file WHERE user_id = :user AND id = ANY(CAST(:ids AS bigint[]))
              AND (session_id IS NULL OR session_id = :session) AND deleted_at IS NULL
        """),
                    {"user": user["id"], "ids": body.fileIds, "session": body.sessionId},
                )
            ).all()
        )
        if files != set(body.fileIds):
            raise not_found("Attachment not found in this session")

    bindings = await bound_models(db, ["fast_chat", "moderation", "embedding"])
    code = (
        body.modelCode
        or session["model_code"]
        or session["default_model_code"]
        or bindings.get("fast_chat")
    )
    if not code:
        raise unavailable("Configure a fast chat model in M18")
    model = await get_model(db, code, user)
    if not model:
        raise not_found("Model not found")
    fallback_from = None
    output_limit = min(model["max_output"] or 4096, 8192)
    if not model["available"]:
        model = await get_model(db, model["fallback_model_code"], user)
        if not model or not model["available"]:
            raise unavailable("Requested model is unavailable")
        fallback_from = code
        output_limit = min(model["max_output"] or 4096, 8192)

    leaf = session["active_leaf_msg_id"]
    parent = body.parentMsgId or leaf
    budget = min(model["context_length"] or 16000, 24000)
    history = await recent_messages(db, body.sessionId, parent, budget)
    temporary = session["is_temporary"] or body.isTemporary
    if temporary and body.fileIds:
        raise ClientError("Temporary sessions cannot use persisted attachments")
    message_id = await db.scalar(
        text("SELECT nextval(pg_get_serial_sequence('chat_message', 'id'))")
    )
    cost = await db.scalar(text("SELECT rule_value FROM point_rule WHERE rule_key = 'search_cost'"))
    costs = {name: str(model[name]) for name in ("rate_in", "rate_cache", "rate_out")}
    costs["search"] = cost or 0
    costs["sandbox"] = await db.scalar(
        text("SELECT rule_value FROM point_rule WHERE rule_key = 'sandbox_10s_cost'")
    ) or 0
    # An expired Redis lease cannot allow the earlier producer to finalize an old turn.
    await db.execute(
        text("""
        UPDATE chat_turn SET status = 'superseded', content = NULL
        WHERE user_id = :user AND session_id = :session AND status = 'running'
    """),
        {"user": user["id"], "session": body.sessionId},
    )
    await db.execute(
        text("""
        INSERT INTO chat_turn (id, user_id, session_id, token_version, base_leaf_id, parent_id,
                               message_id, content, model_code, costs, is_temporary, file_ids)
        VALUES (:id, :user, :session, :version, :leaf, :parent, :message, :content, :model,
                CAST(:costs AS jsonb), :temporary, CAST(:files AS bigint[]))
    """),
        {
            "id": body.turnId,
            "user": user["id"],
            "session": body.sessionId,
            "version": user["token_version"],
            "leaf": leaf,
            "parent": parent,
            "message": message_id,
            "content": None if temporary else body.content,
            "model": model["model_code"],
            "costs": json.dumps(costs),
            "temporary": temporary,
            "files": body.fileIds,
        },
    )
    await db.commit()
    return ok(
        {
            "messageId": message_id,
            "modelCode": model["model_code"],
            "fallbackFrom": fallback_from,
            "maxTokens": output_limit,
            "history": history,
            "bindings": bindings,
            "memoryEnabled": user["memory_enabled"] and not temporary,
            "customInstructions": user["custom_instructions"],
            "botInstructions": session["system_prompt"],
            "summary": session["summary"] if parent == leaf else None,
            "userId": user["id"],
        }
    )


class MemoryQuery(BaseModel):
    embedding: Annotated[list[float], Field(min_length=1536, max_length=1536)]


@router.post("/{turn_id}/memoryContext")
async def memory_context(turn_id: UUID, body: MemoryQuery, db: Database, user: User):
    if not any(body.embedding) or not all(
        math.isfinite(value) and abs(value) <= 3e38 for value in body.embedding
    ):
        raise ClientError("Invalid embedding")
    result = await db.scalars(
        text("""
        SELECT m.content FROM user_memory m
        JOIN chat_turn t ON t.user_id = m.user_id AND t.id = :turn
        WHERE m.user_id = :user AND m.status = 'active' AND m.embedding IS NOT NULL
          AND NOT t.is_temporary AND t.status = 'running'
        ORDER BY m.embedding <=> CAST(:embedding AS vector) LIMIT 5
    """),
        {"turn": turn_id, "user": user["id"], "embedding": json.dumps(body.embedding)},
    )
    return ok(result.all() if user["memory_enabled"] else [])


@router.post("/{turn_id}/finish")
async def finish(turn_id: UUID, body: FinishTurn, db: Database):
    locator = (
        await db.execute(
            text("""
        SELECT user_id, token_version, session_id FROM chat_turn WHERE id = :id
    """),
            {"id": turn_id},
        )
    ).first()
    if not locator:
        raise not_found("Turn not found")
    user = await active_user(db, locator.user_id, locator.token_version, write=True)
    session = await lock_session(db, locator.user_id, locator.session_id)
    turn = (
        (
            await db.execute(
                text("""
        SELECT * FROM chat_turn WHERE id = :id FOR UPDATE
    """),
                {"id": turn_id},
            )
        )
        .mappings()
        .one()
    )
    if turn["result"] is not None:
        return ok(turn["result"])
    if turn["status"] != "running" or session["active_leaf_msg_id"] != turn["base_leaf_id"]:
        raise ClientError("Session has advanced", ErrorCode.SESSION_CONFLICT, 409)
    if (body.status == "blocked") != (body.hit is not None):
        raise ClientError("Moderation result is required for a blocked turn")

    result = {"status": body.status, "messageId": turn["message_id"], "memoryPending": False}
    if body.hit:
        await db.execute(
            text("""
            INSERT INTO moderation_log (user_id, turn_id, direction, hit_layer, snippet)
            VALUES (:user, :id, :direction, :layer, :snippet)
        """),
            {
                **body.hit.model_dump(),
                "id": turn_id,
                "user": locator.user_id,
                "snippet": None if turn["is_temporary"] else body.hit.snippet,
            },
        )
    if body.status in ("done", "stopped"):
        result.update(await complete(db, turn, body))
        if body.status == "done" and not turn["is_temporary"] and user["memory_enabled"]:
            bindings = await bound_models(db, ["memory_extract", "embedding"])
            result["memoryPending"] = len(bindings) == 2
    await db.execute(
        text("""
        UPDATE chat_turn SET status = :status, content = NULL, result = CAST(:result AS jsonb),
                             memory_pending = :pending WHERE id = :id
    """),
        {
            "status": body.status,
            "id": turn_id,
            "result": json.dumps(result),
            "pending": result["memoryPending"],
        },
    )
    await db.commit()
    if result["memoryPending"]:
        settings = RedisSettings.from_dsn(get_settings().redis_url)
        settings.conn_timeout = 2
        settings.conn_retries = 0
        try:
            async with await create_pool(settings) as queue:
                await queue.enqueue_job("extract_memory", str(turn_id), _job_id=f"memory:{turn_id}")
        except (RedisError, OSError):
            pass  # The worker recovers pending turns if immediate dispatch fails.
    return ok(result)


async def complete(db: Database, turn, body: FinishTurn) -> dict:
    usage = body.usage
    cached = min(usage.inputTokens, usage.cachedTokens)
    rates = turn["costs"]
    cost = (
        math.ceil(
            (
                (usage.inputTokens - cached) * Decimal(rates["rate_in"])
                + cached * Decimal(rates["rate_cache"])
                + usage.outputTokens * Decimal(rates["rate_out"])
            )
            / 1000
        )
        + body.searchCalls * rates["search"]
        + ((body.sandboxMs + 9999) // 10000) * rates.get("sandbox", 0)
    )
    detail = {
        **usage.model_dump(),
        "cachedTokens": cached,
        "modelCode": turn["model_code"],
        "searchCalls": body.searchCalls,
        "sandboxMs": body.sandboxMs,
        "rates": rates,
        "calculatedCost": cost,
    }
    settlement = await settle_turn_points(db, turn["user_id"], turn["id"], cost, detail)
    result = {
        "usage": {**usage.model_dump(), "cachedTokens": cached},
        "pointCost": settlement.charged,
        "balanceAfter": settlement.balance_after,
    }
    if turn["is_temporary"]:
        return result
    params = {
        "turn": turn["id"],
        "session": turn["session_id"],
        "user": turn["user_id"],
        "parent": turn["parent_id"],
        "input": turn["content"],
        "message": turn["message_id"],
        "model": turn["model_code"],
        "output": body.output,
        "status": body.status,
        "cost": settlement.charged,
        "route": body.routeType,
    }
    user_message = await db.scalar(
        text("""
        INSERT INTO chat_message (session_id, parent_id, turn_id, role, content, status)
        VALUES (:session, :parent, :turn, 'user', :input, 'done') RETURNING id
    """),
        params,
    )
    await db.execute(
        text("""
        INSERT INTO chat_message (id, session_id, parent_id, turn_id, role, content, model_code,
                                  status, point_cost, route_type)
        VALUES (:message, :session, :user_message, :turn, 'assistant', :output, :model,
                :status, :cost, :route)
    """),
        {**params, "user_message": user_message},
    )
    await db.execute(
        text("""
        UPDATE chat_session SET active_leaf_msg_id = :message, last_msg_at = now(),
                                model_code = :model WHERE id = :session AND user_id = :user
    """),
        params,
    )
    return result
