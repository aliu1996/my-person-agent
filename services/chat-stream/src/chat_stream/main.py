import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pa_shared import ErrorCode, R, fail, format_sse_frame, get_settings, ok
from pa_shared.internal import INTERNAL_TOKEN_HEADER, valid_internal_token
from pa_shared.telemetry import instrument
from pa_shared.turns import Id, TurnInput
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .events import TurnEvents
from .runner import Runner, Turn, TurnError

logger = logging.getLogger(__name__)
CURSOR = re.compile(r"[0-9]{1,19}-[0-9]{1,19}")
Authorization = Annotated[str, Header(max_length=8192)]
EXPIRED_CURSOR = "Event expired; reload session history"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with (
        httpx.AsyncClient(trust_env=False) as http,
        Redis.from_url(
            get_settings().redis_url,
            decode_responses=True,
            socket_timeout=18,
            socket_connect_timeout=3,
        ) as redis,
    ):
        app.state.runner = Runner(http)
        app.state.redis = redis
        app.state.jobs = set()
        try:
            yield
        finally:
            jobs = tuple(app.state.jobs)
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)


app = FastAPI(title="chat-stream", version="0.1.0", lifespan=lifespan)
instrument(app, "chat-stream")


def json_error(code: int, message: str, status: int):
    return JSONResponse(
        status_code=status,
        content=fail(code, message).model_dump(),
        headers={"Cache-Control": "no-store"},
    )


@app.exception_handler(TurnError)
async def turn_error(request: Request, error: TurnError):
    return json_error(error.code, error.message, error.status)


@app.exception_handler(RedisError)
async def redis_error(request: Request, error: RedisError):
    return json_error(ErrorCode.INTERNAL_ERROR, "Chat stream is unavailable", 503)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError):
    return json_error(ErrorCode.BAD_REQUEST, "Invalid request fields", 400)


def stream_response(frames):
    return StreamingResponse(
        frames,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def read_stream(events: TurnEvents, cursor: str):
    try:
        async for frame in events.read(cursor):
            yield frame
    except RedisError:
        yield events.interrupted()


async def access(request: Request, session: int, token: str):
    if not token.startswith(("Bearer ", "bearer ")):
        raise TurnError(ErrorCode.UNAUTHORIZED, "Missing user token", 401)
    await request.app.state.runner.api(f"access/{session}", token=token)


@app.post("/api/chat/stream/send")
async def send(body: TurnInput, request: Request, authorization: Authorization = ""):
    return await start_turn(body, request, authorization, uuid4())


@app.post("/internal/scheduled/send/{turn_id}", include_in_schema=False)
async def scheduled_send(
    turn_id: UUID,
    body: TurnInput,
    request: Request,
    authorization: Authorization = "",
    token: Annotated[str, Header(alias=INTERNAL_TOKEN_HEADER)] = "",
):
    if not valid_internal_token(token):
        raise TurnError(ErrorCode.UNAUTHORIZED, "Invalid service credentials", 401)
    return await start_turn(body, request, authorization, turn_id, scheduled=True)


async def start_turn(
    body: TurnInput,
    request: Request,
    authorization: str,
    turn_id: UUID,
    *,
    scheduled=False,
):
    await access(request, body.sessionId, authorization)
    events = TurnEvents(request.app.state.redis, body.sessionId, str(turn_id))
    cursor = await events.claim()
    if cursor is None:
        raise TurnError(ErrorCode.SESSION_CONFLICT, "Session already has an active turn", 409)
    turn = Turn(body, authorization, events, scheduled=scheduled)
    task = asyncio.create_task(request.app.state.runner.run(turn))
    jobs = request.app.state.jobs

    def finished(job):
        jobs.discard(job)
        if not job.cancelled() and (failure := job.exception()):
            logger.warning("Turn task ended unexpectedly (%s)", type(failure).__name__)

    jobs.add(task)
    task.add_done_callback(finished)
    return stream_response(read_stream(events, cursor))


@app.get("/api/chat/stream/resume")
async def resume(
    request: Request,
    sessionId: Id,
    lastEventId: Annotated[str | None, Query(max_length=39)] = None,
    last_event_header: Annotated[str | None, Header(alias="Last-Event-ID", max_length=39)] = None,
    authorization: Authorization = "",
):
    await access(request, sessionId, authorization)
    redis = request.app.state.redis
    cursor = lastEventId or last_event_header
    if cursor is not None and not CURSOR.fullmatch(cursor):
        raise TurnError(ErrorCode.BAD_REQUEST, "Invalid event cursor", 400)
    events = TurnEvents(redis, sessionId, "")
    head = await redis.hgetall(events.head)
    if not head:
        raise TurnError(ErrorCode.BAD_REQUEST, "No resumable turn", 404)
    if cursor and cursor != "0-0":
        rows = await redis.xrange(events.stream, cursor, cursor, count=1)
        if not rows:
            raise TurnError(ErrorCode.SESSION_CONFLICT, EXPIRED_CURSOR, 409)
        _, fields = rows[0]
        events = TurnEvents(redis, sessionId, fields["turn"])
        if fields["event"] in ("done", "error") or fields.get("late") == "1":
            return stream_response(late_memory(events, cursor))
    else:
        cursor = head["cursor"]
        events = TurnEvents(redis, sessionId, head["turn"])
        first = await redis.xrange(events.stream, "-", "+", count=1)
        if (
            first
            and first[0][1]["turn"] == events.turn
            and first[0][1]["event"]
            not in (
                "meta",
                "error",
            )
        ):
            raise TurnError(ErrorCode.SESSION_CONFLICT, EXPIRED_CURSOR, 409)
    return stream_response(read_stream(events, cursor))


async def late_memory(events: TurnEvents, cursor: str):
    for event_id, fields in await events.redis.xrange(events.stream, f"({cursor}", "+"):
        if fields["turn"] != events.turn:
            return
        if fields["event"] == "memory":
            yield format_sse_frame("memory", json.loads(fields["data"]), event_id)


class StopRequest(BaseModel):
    sessionId: Id | None = None
    turnId: UUID | None = None


@app.post("/api/chat/stream/stop")
async def stop(body: StopRequest, request: Request, authorization: Authorization = ""):
    redis = request.app.state.redis
    turn = str(body.turnId) if body.turnId else None
    session = body.sessionId
    if session is None:
        if turn is None:
            raise TurnError(ErrorCode.BAD_REQUEST, "Specify a session or turn to stop", 400)
        found = await redis.get(f"chat:turn-session:{turn}")
        if found is None:
            raise TurnError(ErrorCode.BAD_REQUEST, "Turn not found", 404)
        session = int(found)
    await access(request, session, authorization)
    turn = turn or await redis.get(f"chat:lock:{session}")
    stopped = await TurnEvents(redis, session, turn).stop() if turn else False
    return ok({"stopped": stopped, "turnId": turn})


@app.get("/healthz", response_model=R[dict[str, str]])
async def healthz() -> R[dict[str, str]]:
    return ok({"service": "chat-stream", "status": "healthy"})
