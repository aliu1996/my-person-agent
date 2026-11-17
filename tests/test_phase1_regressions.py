import io
import json
import re
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from chat_api.main import app
from pa_shared import get_settings, higress
from pa_shared.security import create_jwt, encrypt_secret, verify_password
from pa_shared.settings import Settings


def test_phase1_offline_migration(monkeypatch):
    monkeypatch.setenv("INIT_ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("INIT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()
    try:
        output = io.StringIO()
        config = Path(__file__).resolve().parents[1] / "services/chat-api/alembic.ini"
        command.upgrade(Config(str(config), output_buffer=output), "head", sql=True)
    finally:
        get_settings.cache_clear()
    sql = output.getvalue()
    assert len(re.findall(r"CREATE TABLE chat_session_p[0-9]+ ", sql)) == 16
    assert len(re.findall(r"CREATE TABLE chat_message_[0-9]{4}_[0-9]{2} ", sql)) == 4
    assert "USING hnsw (embedding vector_cosine_ops)" in sql
    assert "turn_id UUID NOT NULL UNIQUE" in sql
    assert "('daily_free_amount', 1000)" in sql
    password_hash = re.search(r"VALUES \('test-admin', '([^']+)'\)", sql).group(1)
    assert "test-password" not in sql
    assert verify_password("test-password", password_hash)


@pytest.mark.parametrize("role", ["user", "admin_totp_setup"])
async def test_admin_requires_completed_admin_authentication(role):
    settings = get_settings()
    secret = settings.user_jwt_secret if role == "user" else settings.admin_jwt_secret
    token = create_jwt("1", secret, role)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app"
    ) as client:
        response = await client.get(
            "/api/admin/provider/list", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 401
    assert response.json()["code"] == 4010


async def test_console_uploads_key_and_routes_offline_model_to_fallback(monkeypatch):
    settings = Settings(
        higress_console_url="http://console",
        higress_console_username="operator",
        higress_console_password="test-password",
        key_encrypt_master="test-master",
    )
    console_calls = []

    def console(request):
        method = request.method
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        console_calls.append((method, path, body))
        if method == "GET":
            data = [{"name": "pa-model-99"}, {"name": "other"}] if path == higress.ROUTES else []
            return httpx.Response(200, json={"success": True, "data": data})
        return httpx.Response(200, json={"success": True})

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        higress.httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(console), **kwargs),
    )
    provider = {
        "id": 1,
        "protocol": "openai",
        "base_url": "https://upstream.example/v1",
        "timeout_ms": 5000,
    }
    key = {
        "provider_id": 1,
        "weight": 1,
        "key_ciphertext": encrypt_secret("sk-fake-test-credential", settings.key_encrypt_master),
    }
    old_model = {
        "id": 1,
        "provider_id": 1,
        "model_code": "old",
        "upstream_model": "old-upstream",
        "status": "offline",
        "fallback_model_code": "new",
    }
    new_model = {
        "id": 2,
        "provider_id": 1,
        "model_code": "new",
        "upstream_model": "new-upstream",
        "status": "online",
        "fallback_model_code": None,
    }
    await higress.publish_console(settings, [provider], [key], [old_model, new_model])
    updates = [
        body
        for method, path, body in console_calls
        if method == "POST" and path.startswith("/v1/ai/")
    ]
    assert updates[0]["tokens"] == ["sk-fake-test-credential"]
    assert updates[1]["upstreams"][0]["modelMapping"] == {"old": "new-upstream"}
    assert "sk-fake-test-credential" not in str(updates[1:])
    assert ("DELETE", "/v1/ai/routes/pa-model-99", None) in console_calls
    assert ("DELETE", "/v1/ai/routes/other", None) not in console_calls
