import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from chat_stream.events import TurnEvents
from chat_stream.moderation import TERMS_KEY, TermMatcher
from chat_stream.runner import Runner, Turn
from pa_shared.turns import TurnInput
from search_service import main as search

TURN_ID = "50d699bf-afdf-466b-96c7-eb827767c884"


class LocalEvents:
    """Capture frames emitted by the producer without running external services."""

    session = 1
    turn = TURN_ID
    lock = "chat:lock:1"

    def __init__(self, terms=()):
        self.redis = self
        self.frames = []
        self.terms = terms
        self.delta_emitted = asyncio.Event()

    async def get(self, key):
        return json.dumps(self.terms) if key == TERMS_KEY else self.turn

    async def eval(self, *args):
        return 1

    async def append(self, event, data):
        self.frames.append((event, data))
        if event == "delta":
            self.delta_emitted.set()


class StoppableStream(httpx.AsyncByteStream):
    def __init__(self, first_event):
        self.first_event = first_event

    async def __aiter__(self):
        yield self.first_event.encode()
        await asyncio.Future()


def upstream(monkeypatch, events, *, blocked_direction=None, broken_stream=False, streaming=False):
    finished = []

    async def handler(request):
        data = json.loads(request.content)
        if request.url.path.endswith("/prepare"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "modelCode": "demo",
                        "messageId": 8,
                        "fallbackFrom": None,
                        "maxTokens": 100,
                        "history": [],
                        "bindings": {"moderation": "mod"},
                        "memoryEnabled": False,
                        "customInstructions": None,
                        "botInstructions": None,
                        "summary": None,
                    },
                },
            )
        if request.url.path.endswith("/finish"):
            finished.append(data)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "status": data["status"],
                        "pointCost": 1 if data["status"] == "done" else 0,
                        "balanceAfter": 999,
                        "usage": data["usage"],
                    },
                },
            )
        if data["model"] == "mod":
            content = data["messages"][-1]["content"]
            direction = "input" if content == "你好" else "output"
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps({"blocked": blocked_direction == direction}),
                            }
                        }
                    ]
                },
            )
        assert data["stream_options"] == {"include_usage": True}
        chunks = [
            {"choices": [{"delta": {"content": "答"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "复！"}, "finish_reason": None}]},
        ]
        if not broken_stream:
            chunks.append(
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 72,
                        "completion_tokens": 3,
                        "prompt_tokens_details": {"cached_tokens": 12},
                    },
                }
            )
        payload = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        if streaming:
            return httpx.Response(200, stream=StoppableStream(payload))
        return httpx.Response(200, text=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    runner = Runner(client)

    async def wait_for_stop(_):
        await asyncio.Future()

    monkeypatch.setattr(runner, "watch", wait_for_stop)
    return (
        client,
        runner,
        Turn(TurnInput(sessionId=1, content="你好"), "Bearer jwt", events),
        finished,
    )


@pytest.mark.parametrize("direction", ["input", "output"])
async def test_blocked_turn_neither_emits_rejected_text_nor_settles_as_success(
    monkeypatch, direction
):
    events = LocalEvents()
    client, runner, turn, finished = upstream(monkeypatch, events, blocked_direction=direction)
    async with client:
        await runner.run(turn)
    assert [event for event, _ in events.frames if event == "delta"] == []
    assert events.frames[-1][0] == "error"
    assert events.frames[-1][1]["code"] == 4031
    assert events.frames[-1][1]["replaceOutput"]
    assert [row["status"] for row in finished] == ["blocked"]
    assert finished[0]["hit"]["direction"] == direction


@pytest.mark.parametrize("interrupted", [False, True])
async def test_incomplete_model_stream_does_not_commit_success(monkeypatch, interrupted):
    events = LocalEvents()
    client, runner, turn, finished = upstream(monkeypatch, events, broken_stream=interrupted)
    async with client:
        await runner.run(turn)
    verb = "failed" if interrupted else "done"
    assert [row["status"] for row in finished] == [verb]
    assert events.frames[-1][0] == ("error" if interrupted else "done")
    if interrupted:
        assert finished[0]["output"] == ""
        assert events.frames[-1][1]["replaceOutput"]
    else:
        assert finished[0]["usage"] == {
            "inputTokens": 72,
            "cachedTokens": 12,
            "outputTokens": 3,
            "estimated": False,
        }
        assert finished[0]["output"] == "答复！"


async def test_stop_keeps_approved_output_and_settles_estimated_usage(monkeypatch):
    events = LocalEvents()
    client, runner, turn, finished = upstream(
        monkeypatch, events, broken_stream=True, streaming=True,
    )

    async def stop_after_first_delta(_):
        await events.delta_emitted.wait()
        return True

    monkeypatch.setattr(runner, "watch", stop_after_first_delta)
    async with client:
        await runner.run(turn)
    assert finished[0]["status"] == "stopped"
    assert finished[0]["output"] == "答复！"
    assert finished[0]["usage"]["estimated"]
    assert finished[0]["usage"]["outputTokens"] > 0
    assert events.frames[-1][0] == "done"


async def test_resume_only_reads_events_after_cursor_and_terminates_on_done():
    class RedisRead:
        async def xread(self, keys, *, count, block):
            assert keys == {"chat:stream:1": "12-0"}
            assert block == 15000
            return [
                (
                    "chat:stream:1",
                    [
                        ("13-0", {"event": "delta", "turn": TURN_ID, "data": '{"text":"B"}'}),
                        ("14-0", {"event": "done", "turn": TURN_ID, "data": "{}"}),
                        ("15-0", {"event": "delta", "turn": "other", "data": '{"text":"C"}'}),
                    ],
                )
            ]

    frames = [frame async for frame in TurnEvents(RedisRead(), 1, TURN_ID).read("12-0")]
    assert len(frames) == 2
    assert "id: 13-0\nevent: delta" in frames[0]
    assert "id: 14-0\nevent: done" in frames[1]


def test_keyword_matcher_normalizes_text_and_keeps_suffix_fallback():
    matcher = TermMatcher(["ＳＫ－secret", "bc", "abcd"])
    assert matcher.matches("SK-secret")
    assert matcher.matches("abc")
    assert not matcher.matches("acbd")


async def test_search_falls_back_and_reuses_cached_provider_result(monkeypatch):
    calls = []
    cache = {}

    async def adapter(http, provider, query):
        calls.append((provider, query))
        if provider == "tavily":
            return [
                search.Source(
                    title="Article",
                    url="https://example.com/",
                    domain="example.com",
                    snippet="News",
                )
            ]

    async def get(key):
        return cache.get(key)

    async def set_cache(key, value, *, ex):
        assert ex == 900
        cache[key] = value

    monkeypatch.setattr(search, "provider_search", adapter)
    monkeypatch.setattr(search, "valid_internal_token", lambda value: value == "service")
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                redis=SimpleNamespace(get=get, set=set_cache),
                http=object(),
            )
        )
    )
    body = search.SearchRequest(query="上海新闻")
    first = await search.search(body, request, "service")
    second = await search.search(body, request, "service")
    assert calls == [("bocha", "上海新闻"), ("tavily", "上海新闻")]
    assert first.data == second.data
    assert first.data["provider"] == "tavily"
