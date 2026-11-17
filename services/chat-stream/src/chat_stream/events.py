import json
from dataclasses import dataclass

from pa_shared import ErrorCode, format_sse_frame
from redis.asyncio import Redis

TTL = 600
LEASE = 120
LIMIT = 500

CLAIM = """
if not redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then return false end
local tail = redis.call('XREVRANGE', KEYS[2], '+', '-', 'COUNT', 1)
local cursor = '0-0'
if #tail > 0 then cursor = tail[1][1] end
redis.call('HSET', KEYS[3], 'turn', ARGV[1], 'cursor', cursor)
redis.call('EXPIRE', KEYS[3], ARGV[3])
redis.call('SET', KEYS[4], ARGV[4], 'EX', ARGV[3])
return cursor
"""

APPEND = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return false end
local tail = redis.call('XREVRANGE', KEYS[2], '+', '-', 'COUNT', 1)
local previous = '0-0'
if #tail > 0 then previous = tail[1][1] end
local id = redis.call('XADD', KEYS[2], 'MAXLEN', ARGV[2], '*',
    'turn', ARGV[1], 'event', ARGV[3], 'data', ARGV[4], 'previous', previous)
redis.call('EXPIRE', KEYS[2], ARGV[5])
redis.call('EXPIRE', KEYS[3], ARGV[5])
return id
"""

RENEW = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('EXPIRE', KEYS[1], ARGV[2])
"""

RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""

STOP = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[2], 'stop', 'EX', ARGV[2])
redis.call('PUBLISH', KEYS[3], 'stop')
return 1
"""


def error_data(turn: str | None, code: int, message: str, *, retryable: bool = False) -> dict:
    return {"turnId": turn, "code": int(code), "message": message, "retryable": retryable}


class LeaseLost(Exception):
    pass


@dataclass(frozen=True)
class TurnEvents:
    redis: Redis
    session: int
    turn: str

    @property
    def stream(self) -> str:
        return f"chat:stream:{self.session}"

    @property
    def head(self) -> str:
        return f"chat:head:{self.session}"

    @property
    def lock(self) -> str:
        return f"chat:lock:{self.session}"

    @property
    def stop_key(self) -> str:
        return f"chat:stop:{self.turn}"

    @property
    def channel(self) -> str:
        return f"chat:ctrl:{self.turn}"

    async def claim(self) -> str | None:
        return await self.redis.eval(
            CLAIM,
            4,
            self.lock,
            self.stream,
            self.head,
            f"chat:turn-session:{self.turn}",
            self.turn,
            LEASE,
            TTL,
            self.session,
        )

    async def append(self, event: str, data: dict) -> None:
        payload = json.dumps({"turnId": self.turn, **data}, ensure_ascii=False)
        if not await self.redis.eval(
            APPEND,
            3,
            self.lock,
            self.stream,
            self.head,
            self.turn,
            LIMIT,
            event,
            payload,
            TTL,
        ):
            raise LeaseLost

    async def stop(self) -> bool:
        return bool(
            await self.redis.eval(
                STOP,
                3,
                self.lock,
                self.stop_key,
                self.channel,
                self.turn,
                TTL,
            )
        )

    async def read(self, cursor: str):
        while True:
            batches = await self.redis.xread({self.stream: cursor}, count=100, block=15000)
            if not batches:
                if await self.redis.get(self.lock) != self.turn:
                    # Recheck after observing release, as the final event might have just landed.
                    batches = await self.redis.xread({self.stream: cursor}, count=100)
                    if not batches:
                        yield self.interrupted()
                        return
                else:
                    yield format_sse_frame("ping", {})
                    continue
            for _, entries in batches:
                for event_id, fields in entries:
                    if fields["turn"] != self.turn:
                        yield self.interrupted()
                        return
                    if fields.get("previous", cursor) != cursor:
                        yield format_sse_frame(
                            "error", error_data(
                                self.turn, ErrorCode.SESSION_CONFLICT,
                                "Event expired; reload session history",
                            ),
                        )
                        return
                    cursor = event_id
                    event = fields["event"]
                    data = json.loads(fields["data"])
                    yield format_sse_frame(event, data, event_id)
                    if event in ("done", "error"):
                        return

    def interrupted(self) -> str:
        return format_sse_frame(
            "error",
            error_data(
                self.turn,
                ErrorCode.INTERNAL_ERROR,
                "Turn interrupted",
                retryable=True,
            ),
        )
