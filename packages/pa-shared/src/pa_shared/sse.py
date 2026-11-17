import json
from enum import StrEnum
from typing import Any


class SseEventType(StrEnum):
    """统一 SSE 流式协议的 11 种标准事件。"""

    META = "meta"
    THINKING = "thinking"
    TOOL = "tool"
    SOURCE = "source"
    DELTA = "delta"
    CONFIRM = "confirm"
    MEMORY = "memory"
    ARTIFACT = "artifact"
    DONE = "done"
    ERROR = "error"
    PING = "ping"


def format_sse_frame(event: SseEventType | str, data: dict[str, Any], event_id: str = "") -> str:
    """序列化标准 SSE 帧，供 chat-stream 推送与断点续传。"""
    lines: list[str] = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {str(event)}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"
