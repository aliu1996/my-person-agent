"""Low-cardinality metrics and metadata-only trace export."""

import asyncio
import base64
import json
import logging
import re
import secrets
from collections import Counter
from contextvars import ContextVar
from time import perf_counter, time_ns

import httpx
from starlette.responses import PlainTextResponse

from .settings import get_settings

logger = logging.getLogger(__name__)
current_trace: ContextVar[str] = ContextVar("current_trace", default="")
TRACE = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
REQUESTS: Counter[tuple[str, int]] = Counter()
LATENCY: Counter[str] = Counter()
ACTIVE: Counter[str] = Counter()
CHAT: Counter[str] = Counter()
PENDING: set[asyncio.Task] = set()


def trace_headers() -> dict[str, str]:
    trace = current_trace.get()
    return {"traceparent": trace} if trace else {}


def metric(event: str, amount: float = 1) -> None:
    CHAT[event] += amount


async def export_trace(service: str, span: dict, *, langfuse: bool = False) -> None:
    settings = get_settings()
    targets = []
    if settings.otel_exporter_otlp_traces_endpoint and not langfuse:
        targets.append((settings.otel_exporter_otlp_traces_endpoint, {}))
    if (
        langfuse
        and settings.langfuse_host
        and settings.langfuse_public_key
        and settings.langfuse_secret_key
    ):
        value = settings.langfuse_public_key + ":" + settings.langfuse_secret_key.get_secret_value()
        auth = base64.b64encode(value.encode()).decode()
        targets.append(
            (
                settings.langfuse_host.rstrip("/") + "/api/public/otel/v1/traces",
                {"Authorization": "Basic " + auth},
            )
        )
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": service}}]
                },
                "scopeSpans": [{"scope": {"name": "personal-assistant"}, "spans": [span]}],
            }
        ]
    }
    async with httpx.AsyncClient(timeout=3, trust_env=False) as http:
        for url, headers in targets:
            try:
                response = await http.post(url, json=payload, headers=headers)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("Trace export failed (%s)", type(exc).__name__)


def export_if_configured(service: str, span: dict, *, langfuse=False) -> None:
    settings = get_settings()
    endpoint = settings.langfuse_host if langfuse else settings.otel_exporter_otlp_traces_endpoint
    if endpoint and len(PENDING) < 128:
        task = asyncio.create_task(export_trace(service, span, langfuse=langfuse))
        PENDING.add(task)
        task.add_done_callback(PENDING.discard)


def finish_turn_trace(turn: str, status: str, route: str, started: int) -> None:
    context = TRACE.fullmatch(current_trace.get())
    if not context:
        return
    trace_id, parent, _ = context.groups()
    attributes = {"turn.id": turn, "gen_ai.operation.name": route, "turn.status": status}
    span = {
        "traceId": trace_id,
        "spanId": secrets.token_hex(8),
        "parentSpanId": parent,
        "name": "assistant.turn",
        "kind": 1,
        "startTimeUnixNano": str(started),
        "endTimeUnixNano": str(time_ns()),
        "attributes": [
            {"key": key, "value": {"stringValue": value}} for key, value in attributes.items()
        ],
        "status": {"code": 2 if status in ("failed", "blocked") else 1},
    }
    logger.info(
        json.dumps({"trace_id": trace_id, "turn_id": turn, "route": route, "status": status})
    )
    export_if_configured("chat-stream", span)
    if route == "agent" or status != "done" or int(trace_id[:8], 16) % 10 == 0:
        export_if_configured("chat-stream", span, langfuse=True)


class RequestTelemetry:
    def __init__(self, app, service: str):
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/metrics":
            await self.app(scope, receive, send)
            return
        raw = dict(scope["headers"]).get(b"traceparent", b"").decode("ascii", "ignore")
        parent = TRACE.fullmatch(raw)
        if parent and int(parent[1], 16) and int(parent[2], 16):
            trace_id, parent_id, flags = parent.groups()
        else:
            trace_id, parent_id, flags = secrets.token_hex(16), "", "01"
        span_id = secrets.token_hex(8)
        token = current_trace.set(f"00-{trace_id}-{span_id}-{flags}")
        started = time_ns()
        elapsed = perf_counter()
        status = 500
        ACTIVE[self.service] += 1

        async def send_with_trace(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-trace-id", trace_id.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            ACTIVE[self.service] -= 1
            REQUESTS[self.service, status] += 1
            LATENCY[self.service] += perf_counter() - elapsed
            path = getattr(scope.get("route"), "path", "unmatched")
            logger.info(
                json.dumps(
                    {"trace_id": trace_id, "service": self.service, "route": path, "status": status}
                )
            )
            export_if_configured(
                self.service,
                {
                    "traceId": trace_id,
                    "spanId": span_id,
                    **({"parentSpanId": parent_id} if parent_id else {}),
                    "name": path,
                    "kind": 2,
                    "startTimeUnixNano": str(started),
                    "endTimeUnixNano": str(time_ns()),
                    "attributes": [
                        {"key": "http.response.status_code", "value": {"intValue": status}}
                    ],
                    "status": {"code": 2 if status >= 500 else 0},
                },
            )
            current_trace.reset(token)


def instrument(app, service: str) -> None:
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    app.add_middleware(RequestTelemetry, service=service)

    async def metrics():
        lines = [
            "# TYPE pa_http_requests_total counter",
            *(
                f'pa_http_requests_total{{service="{svc}",status="{status}"}} {count}'
                for (svc, status), count in REQUESTS.items()
            ),
            "# TYPE pa_http_requests_active gauge",
            *(
                f'pa_http_requests_active{{service="{svc}"}} {count}'
                for svc, count in ACTIVE.items()
            ),
            "# TYPE pa_http_duration_seconds_total counter",
            *(
                f'pa_http_duration_seconds_total{{service="{svc}"}} {duration}'
                for svc, duration in LATENCY.items()
            ),
            *(f"pa_chat_{name}_total {value}" for name, value in CHAT.items()),
        ]
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    app.add_api_route("/metrics", metrics, include_in_schema=False)
