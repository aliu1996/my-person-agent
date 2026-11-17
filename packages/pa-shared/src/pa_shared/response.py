from enum import IntEnum
from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ErrorCode(IntEnum):
    """统一业务错误码，与接口协议文档保持一致。"""

    OK = 0
    BAD_REQUEST = 4001
    UNAUTHORIZED = 4010
    INSUFFICIENT_POINTS = 4020
    MODERATION_BLOCKED = 4031
    SESSION_CONFLICT = 4091
    RATE_LIMITED = 4290
    INTERNAL_ERROR = 5000


class R(BaseModel, Generic[T]):
    """统一 REST 响应信封。"""

    code: int = ErrorCode.OK
    message: str = "ok"
    data: T | None = None
    traceId: str = ""


def ok(data: T | None = None, message: str = "ok", trace_id: str = "") -> R[T]:
    return R(code=ErrorCode.OK, message=message, data=data, traceId=trace_id)


def fail(code: ErrorCode | int, message: str, trace_id: str = "") -> R[None]:
    return R(code=int(code), message=message, data=None, traceId=trace_id)
