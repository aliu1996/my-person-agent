import jwt
import pytest
from chat_api.main import app as chat_api_app
from chat_stream.main import app as chat_stream_app
from fastapi.testclient import TestClient
from pa_shared import ErrorCode, SseEventType, format_sse_frame, ok
from pa_shared.security import (
    create_jwt,
    decode_jwt,
    decrypt_secret,
    encrypt_secret,
    mask_secret,
)
from search_service.main import app as search_service_app


def test_response_envelope() -> None:
    resp = ok({"hello": "world"}, trace_id="t1")
    assert resp.code == ErrorCode.OK
    assert resp.data == {"hello": "world"}
    assert resp.traceId == "t1"


def test_aes_encrypt_decrypt_and_mask() -> None:
    master = "test-master-secret"
    plain = "sk-1234567890abcdef"
    token = encrypt_secret(plain, master)
    assert token != plain
    assert decrypt_secret(token, master) == plain
    assert mask_secret(plain) == "sk-****cdef"


def test_dual_jwt_isolation() -> None:
    user_token = create_jwt("u_1", "user-secret", role="user")
    assert decode_jwt(user_token, "user-secret", expected_role="user")["sub"] == "u_1"
    with pytest.raises(jwt.InvalidTokenError):
        decode_jwt(user_token, "admin-secret", expected_role="admin")


def test_sse_frame_format() -> None:
    frame = format_sse_frame(SseEventType.MEMORY, {"content": "你在上海"}, event_id="1-0")
    assert "id: 1-0\n" in frame
    assert "event: memory\n" in frame
    assert '"你在上海"' in frame
    assert frame.endswith("\n\n")


@pytest.mark.parametrize(
    ("fastapi_app", "service_name"),
    [
        (chat_api_app, "chat-api"),
        (chat_stream_app, "chat-stream"),
        (search_service_app, "search-service"),
    ],
)
def test_services_healthz(fastapi_app, service_name: str) -> None:
    client = TestClient(fastapi_app)
    res = client.get("/healthz")
    assert res.status_code == 200
    body = res.json()
    assert body["code"] == 0
    assert body["data"] == {"service": service_name, "status": "healthy"}
