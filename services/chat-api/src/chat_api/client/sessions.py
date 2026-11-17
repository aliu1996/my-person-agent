from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pa_shared import ok
from pydantic import Field
from sqlalchemy import text

from ..database import Database
from .common import ClientError, ClientRoute, Id, Input, User, not_found

router = APIRouter(prefix="/chat", route_class=ClientRoute)
Title = Annotated[str, Field(min_length=1, max_length=160)]
SESSION_FIELDS = """
id, bot_id AS "botId", group_id AS "groupId", title, model_code AS "modelCode",
is_pinned AS "isPinned", is_temporary AS "isTemporary", active_leaf_msg_id AS "activeLeafMsgId",
created_at AS "createdAt", last_msg_at AS "lastMsgAt", deleted_at AS "deletedAt"
"""
GROUP_FIELDS = 'id, name, sort_order AS "sortOrder", created_at AS "createdAt"'


class CreateSession(Input):
    botId: Id | None = None
    groupId: Id | None = None
    title: Title = "新会话"
    isTemporary: bool = False


class EditSession(Input):
    id: Id
    title: Title | None = None
    groupId: Id | None = None
    isPinned: bool | None = None


class CreateGroup(Input):
    name: Title
    sortOrder: Annotated[int, Field(ge=0, lt=2**31)] = 0


class EditGroup(CreateGroup):
    id: Id


class Feedback(Input):
    sessionId: Id
    messageId: Id
    rating: Literal["up", "down"]
    reason: Annotated[str | None, Field(max_length=1000)] = None


def clean_title(title: str) -> str:
    value = title.strip()
    if not value:
        raise ClientError("Name must not be blank")
    return value


async def require_group(db: Database, user_id: int, group_id: int | None) -> None:
    if group_id is not None and not await db.scalar(
        text("""
        SELECT id FROM chat_group WHERE id = :id AND user_id = :user AND deleted_at IS NULL
    """),
        {"id": group_id, "user": user_id},
    ):
        raise not_found("Group not found")


async def owned_session(db: Database, user_id: int, session_id: int):
    row = (
        (
            await db.execute(
                text(
                    "SELECT " + SESSION_FIELDS + " FROM chat_session "
                    "WHERE id = :id AND user_id = :user AND deleted_at IS NULL"
                ),
                {"id": session_id, "user": user_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Session not found")
    return row


@router.get("/session/page")
async def session_page(
    db: Database,
    user: User,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=100)] = 20,
    keyword: Annotated[str | None, Query(max_length=160)] = None,
    groupId: Annotated[int | None, Query(gt=0, lt=2**63)] = None,
    ungrouped: bool = False,
    recycled: bool = False,
):
    if groupId is not None and ungrouped:
        raise ClientError("Select a group or ungrouped sessions")
    filters = ["user_id = :user", "NOT is_temporary"]
    filters.append("deleted_at >= now() - interval '30 days'" if recycled else "deleted_at IS NULL")
    params = {"user": user["id"], "limit": pageSize, "offset": (page - 1) * pageSize}
    if keyword and keyword.strip():
        filters.append("strpos(lower(COALESCE(title, '')), lower(:keyword)) > 0")
        params["keyword"] = keyword.strip()
    if groupId is not None:
        filters.append("group_id = :group")
        params["group"] = groupId
    elif ungrouped:
        filters.append("group_id IS NULL")
    where = " FROM chat_session WHERE " + " AND ".join(filters)
    rows = (
        (
            await db.execute(
                text(
                    "SELECT "
                    + SESSION_FIELDS
                    + where
                    + " ORDER BY is_pinned DESC, COALESCE(last_msg_at, created_at) DESC, id DESC "
                    "LIMIT :limit OFFSET :offset"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    total = await db.scalar(text("SELECT count(*)" + where), params)
    return ok(
        {"items": [dict(row) for row in rows], "page": page, "pageSize": pageSize, "total": total}
    )


@router.post("/session/add")
async def create_session(body: CreateSession, db: Database, user: User):
    user_id = user["id"]
    await require_group(db, user_id, body.groupId)
    if body.isTemporary and body.groupId is not None:
        raise ClientError("Temporary sessions cannot be grouped")
    bot = (
        (
            await db.execute(
                text("""
        SELECT id, default_model_code FROM ai_bot
        WHERE deleted_at IS NULL AND
            ((CAST(:bot AS bigint) IS NULL AND is_builtin AND owner_id IS NULL) OR
             (id = :bot AND (owner_id = :user OR (owner_id IS NULL AND is_builtin))))
        ORDER BY id LIMIT 1
    """),
                {"user": user_id, "bot": body.botId},
            )
        )
        .mappings()
        .first()
    )
    if bot is None:
        raise not_found("Bot not found")
    row = (
        (
            await db.execute(
                text(
                    "INSERT INTO chat_session "
                    "(user_id, bot_id, group_id, title, model_code, is_temporary) "
                    "VALUES (:user, :bot, :group, :title, :model, :temporary) RETURNING "
                    + SESSION_FIELDS
                ),
                {
                    "user": user_id,
                    "bot": bot["id"],
                    "group": body.groupId,
                    "title": clean_title(body.title),
                    "model": bot["default_model_code"],
                    "temporary": body.isTemporary,
                },
            )
        )
        .mappings()
        .one()
    )
    await db.commit()
    return ok(dict(row))


@router.put("/session/edit")
async def edit_session(body: EditSession, db: Database, user: User):
    user_id = user["id"]
    session = await owned_session(db, user_id, body.id)
    group = body.groupId if "groupId" in body.model_fields_set else session["groupId"]
    await require_group(db, user_id, group)
    if session["isTemporary"] and (group is not None or body.isPinned):
        raise ClientError("Temporary sessions cannot be grouped or pinned")
    row = (
        (
            await db.execute(
                text(
                    "UPDATE chat_session SET title = :title, group_id = :group, "
                    "is_pinned = :pinned "
                    "WHERE id = :id AND user_id = :user AND deleted_at IS NULL RETURNING "
                    + SESSION_FIELDS
                ),
                {
                    "id": body.id,
                    "user": user_id,
                    "title": clean_title(body.title)
                    if body.title is not None
                    else session["title"],
                    "group": group,
                    "pinned": body.isPinned if body.isPinned is not None else session["isPinned"],
                },
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Session not found")
    await db.commit()
    return ok(dict(row))


@router.delete("/session/{id}")
async def delete_session(id: Id, db: Database, user: User):
    row = await db.scalar(
        text("""
        UPDATE chat_session SET deleted_at = COALESCE(deleted_at, now())
        WHERE id = :id AND user_id = :user RETURNING id
    """),
        {"id": id, "user": user["id"]},
    )
    if row is None:
        raise not_found("Session not found")
    await db.commit()
    return ok({"id": id})


@router.post("/session/{id}/restore")
async def restore_session(id: Id, db: Database, user: User):
    row = await db.scalar(
        text("""
        UPDATE chat_session SET deleted_at = NULL
        WHERE id = :id AND user_id = :user AND deleted_at >= now() - interval '30 days'
            AND NOT is_temporary
        RETURNING id
    """),
        {"id": id, "user": user["id"]},
    )
    if row is None:
        raise not_found("Session not found in recycle bin")
    await db.commit()
    return ok({"id": id})


BRANCH = """
WITH RECURSIVE branch AS (
    SELECT m.id, m.parent_id, m.turn_id, m.role, m.content, m.route_type, m.model_code,
           m.status, m.point_cost, m.created_at, ARRAY[m.id] AS visited, 0 AS depth
    FROM chat_message m WHERE m.session_id = :session AND m.id = :leaf
        AND m.deleted_at IS NULL
    UNION ALL
    SELECT p.id, p.parent_id, p.turn_id, p.role, p.content, p.route_type, p.model_code,
           p.status, p.point_cost, p.created_at, b.visited || p.id, b.depth + 1
    FROM branch b JOIN chat_message p ON p.id = b.parent_id AND p.session_id = :session
    WHERE p.deleted_at IS NULL AND NOT (p.id = ANY(b.visited))
)
"""

MESSAGE_FIELDS = """
b.id, b.parent_id AS "parentId", b.turn_id AS "turnId", b.role, b.content,
b.route_type AS "routeType", b.model_code AS "modelCode", b.status,
b.point_cost AS "pointCost", b.created_at AS "createdAt"
"""


async def read_branch(db: Database, session_id: int, leaf: int | None, *, public: bool = False):
    if leaf is None:
        return []
    if public:
        sql = (
            BRANCH + 'SELECT role, content, created_at AS "createdAt" FROM branch '
            "WHERE role IN ('user', 'assistant') AND status = 'done' AND content IS NOT NULL "
            "ORDER BY depth DESC"
        )
    else:
        sql = (
            BRANCH
            + "SELECT "
            + MESSAGE_FIELDS
            + """
            , cardinality(siblings.ids) AS "siblingCount", siblings.ids AS "siblingIds"
            FROM branch b CROSS JOIN LATERAL (
                SELECT COALESCE(array_agg(s.id ORDER BY s.created_at, s.id), '{}'::bigint[]) AS ids
                FROM chat_message s WHERE s.session_id = :session AND s.deleted_at IS NULL
                  AND (s.parent_id = b.parent_id OR (s.parent_id IS NULL AND b.parent_id IS NULL))
                  AND s.id <> b.id
            ) siblings ORDER BY b.depth DESC
        """
        )
    result = await db.execute(text(sql), {"session": session_id, "leaf": leaf})
    return [dict(row) for row in result.mappings()]


@router.get("/session/{id}/messages")
async def messages(
    id: Id,
    db: Database,
    user: User,
    leafMsgId: Annotated[int | None, Query(gt=0, lt=2**63)] = None,
    branchMsgId: Annotated[int | None, Query(gt=0, lt=2**63)] = None,
):
    if leafMsgId and branchMsgId and leafMsgId != branchMsgId:
        raise ClientError("Branch selectors disagree")
    session = await owned_session(db, user["id"], id)
    leaf = leafMsgId or branchMsgId or session["activeLeafMsgId"]
    items = await read_branch(db, id, leaf)
    if not items and (leafMsgId or branchMsgId):
        raise not_found("Message not found in this session")
    return ok({"sessionId": id, "leafMsgId": leaf if items else None, "items": items})


@router.get("/group/list")
async def list_groups(db: Database, user: User):
    rows = await db.execute(
        text(
            "SELECT "
            + GROUP_FIELDS
            + " FROM chat_group WHERE user_id = :user AND deleted_at IS NULL "
            "ORDER BY sort_order, id"
        ),
        {"user": user["id"]},
    )
    return ok([dict(row) for row in rows.mappings()])


@router.post("/group/add")
async def create_group(body: CreateGroup, db: Database, user: User):
    row = (
        (
            await db.execute(
                text(
                    "INSERT INTO chat_group (user_id, name, sort_order) "
                    "VALUES (:user, :name, :sort) "
                    "RETURNING " + GROUP_FIELDS
                ),
                {"user": user["id"], "name": clean_title(body.name), "sort": body.sortOrder},
            )
        )
        .mappings()
        .one()
    )
    await db.commit()
    return ok(dict(row))


@router.put("/group/edit")
async def edit_group(body: EditGroup, db: Database, user: User):
    row = (
        (
            await db.execute(
                text(
                    "UPDATE chat_group SET name = :name, sort_order = :sort "
                    "WHERE id = :id AND user_id = :user AND deleted_at IS NULL RETURNING "
                    + GROUP_FIELDS
                ),
                {
                    "id": body.id,
                    "user": user["id"],
                    "name": clean_title(body.name),
                    "sort": body.sortOrder,
                },
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Group not found")
    await db.commit()
    return ok(dict(row))


@router.delete("/group/{id}")
async def delete_group(id: Id, db: Database, user: User):
    params = {"id": id, "user": user["id"]}
    updated = await db.scalar(
        text("""
        UPDATE chat_group SET deleted_at = now()
        WHERE id = :id AND user_id = :user AND deleted_at IS NULL RETURNING id
    """),
        params,
    )
    if updated is None:
        raise not_found("Group not found")
    await db.execute(
        text("""
        UPDATE chat_session SET group_id = NULL WHERE user_id = :user AND group_id = :id
    """),
        params,
    )
    await db.commit()
    return ok({"id": id})


@router.post("/feedback")
async def feedback(body: Feedback, db: Database, user: User):
    await owned_session(db, user["id"], body.sessionId)
    message = await db.scalar(
        text("""
        SELECT id FROM chat_message WHERE session_id = :session AND id = :message
            AND role = 'assistant' AND deleted_at IS NULL
    """),
        {"session": body.sessionId, "message": body.messageId},
    )
    if message is None:
        raise not_found("Assistant message not found")
    params = {"user": user["id"], "message": message, "rating": body.rating, "reason": body.reason}
    feedback_id = await db.scalar(
        text("""
        UPDATE chat_feedback SET rating = :rating, reason = :reason
        WHERE user_id = :user AND message_id = :message RETURNING id
    """),
        params,
    )
    if feedback_id is None:
        feedback_id = await db.scalar(
            text("""
            INSERT INTO chat_feedback (user_id, message_id, rating, reason)
            VALUES (:user, :message, :rating, :reason) RETURNING id
        """),
            params,
        )
    await db.commit()
    return ok({"id": feedback_id})
