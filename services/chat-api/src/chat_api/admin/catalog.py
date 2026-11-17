import asyncio
import json
from decimal import Decimal
from time import perf_counter
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter
from pa_shared import ErrorCode, get_settings, ok
from pa_shared.higress import sync_gateway
from pa_shared.security import decrypt_secret, encrypt_secret, mask_secret
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, field_validator
from sqlalchemy import text

from .common import Admin, AdminError, AdminRoute, Database, audit, session_factory

router = APIRouter(route_class=AdminRoute)
Id = Annotated[int, Field(gt=0, lt=2**63)]
ModelCode = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")]
Name = Annotated[str, Field(min_length=1, max_length=160)]
Rate = Annotated[Decimal, Field(ge=0, max_digits=8, decimal_places=4)]
Purpose = Literal[
    "fast_chat", "agent_chat", "summary", "memory_extract", "router", "vision", "moderation",
    "embedding",
]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class KeyInput(Input):
    id: Id | None = None
    apiKey: Annotated[SecretStr, Field(min_length=1, max_length=4096)] | None = None
    weight: Annotated[int, Field(ge=1, le=100)] = 1
    isEnabled: bool = True


class Provider(Input):
    name: Name
    providerType: Literal["cloud", "vllm", "sglang"]
    protocol: Literal["openai", "anthropic"]
    baseUrl: HttpUrl
    timeoutMs: Annotated[int, Field(ge=1, le=120000)] = 30000
    rpmLimit: Annotated[int, Field(ge=0, lt=2**31)] | None = None
    isEnabled: bool = True
    keys: Annotated[list[KeyInput], Field(max_length=32)] | None = None

    @field_validator("baseUrl")
    @classmethod
    def safe_base_url(cls, value: HttpUrl) -> HttpUrl:
        if (
            value.username
            or value.password
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError("Base URL must not contain credentials, query or fragment")
        return value


class ProviderEdit(Provider):
    id: Id


class Model(Input):
    modelCode: ModelCode
    providerId: Id
    upstreamModel: Name
    displayName: Name
    capabilities: list[Literal["vision", "reasoning", "tool", "long_ctx"]] = Field(
        default_factory=list, max_length=4
    )
    contextLength: Annotated[int, Field(gt=0, lt=2**31)] | None = None
    maxOutput: Annotated[int, Field(gt=0, lt=2**31)] | None = None
    rateIn: Rate
    rateCache: Rate
    rateOut: Rate
    fallbackModelCode: ModelCode | None = None
    visibleScope: Literal["all", "internet", "org"] = "all"
    status: Literal["online", "canary", "offline"] = "online"
    canaryRatio: Annotated[int, Field(ge=0, le=100)] = 0
    sortOrder: Annotated[int, Field(ge=0, lt=2**31)] = 0


class TestChat(Input):
    modelCode: ModelCode
    prompt: Annotated[str, Field(min_length=1, max_length=12000)]


PROVIDER_FIELDS = """
id, name, provider_type AS "providerType", protocol, base_url AS "baseUrl",
timeout_ms AS "timeoutMs", rpm_limit AS "rpmLimit", is_enabled AS "isEnabled",
updated_at AS "updatedAt"
"""
MODEL_FIELDS = """
id, model_code AS "modelCode", provider_id AS "providerId", upstream_model AS "upstreamModel",
display_name AS "displayName", capabilities, context_length AS "contextLength",
max_output AS "maxOutput", rate_in AS "rateIn", rate_cache AS "rateCache", rate_out AS "rateOut",
fallback_model_code AS "fallbackModelCode", visible_scope AS "visibleScope", status,
canary_ratio AS "canaryRatio", sort_order AS "sortOrder"
"""
MODEL_COLUMNS = {
    "modelCode": "model_code",
    "providerId": "provider_id",
    "upstreamModel": "upstream_model",
    "displayName": "display_name",
    "capabilities": "capabilities",
    "contextLength": "context_length",
    "maxOutput": "max_output",
    "rateIn": "rate_in",
    "rateCache": "rate_cache",
    "rateOut": "rate_out",
    "fallbackModelCode": "fallback_model_code",
    "visibleScope": "visible_scope",
    "status": "status",
    "canaryRatio": "canary_ratio",
    "sortOrder": "sort_order",
}
PROVIDER_COLUMNS = {
    "name": "name",
    "providerType": "provider_type",
    "protocol": "protocol",
    "baseUrl": "base_url",
    "timeoutMs": "timeout_ms",
    "rpmLimit": "rpm_limit",
    "isEnabled": "is_enabled",
}


async def lock_catalog(db: Database) -> None:
    await db.execute(text("SELECT id FROM gateway_sync_state WHERE id FOR UPDATE"))


async def commit_catalog(db: Database) -> str:
    await db.execute(text("UPDATE gateway_sync_state SET revision = revision + 1 WHERE id"))
    await db.commit()
    return await sync_gateway(session_factory())


async def providers(db: Database, provider_id: int | None = None):
    query = "SELECT " + PROVIDER_FIELDS + " FROM ai_provider"
    key_query = """
        SELECT id, provider_id, key_mask AS "keyMask", weight,
               is_enabled AS "isEnabled", fail_count AS "failCount"
        FROM ai_provider_key
    """
    if provider_id is not None:
        query += " WHERE id = :id"
        key_query += " WHERE provider_id = :id"
    params = {"id": provider_id}
    rows = (await db.execute(text(query + " ORDER BY id"), params)).mappings()
    data = {row["id"]: {**row, "keys": []} for row in rows}
    keys = (await db.execute(text(key_query + " ORDER BY id"), params)).mappings()
    for key in keys:
        provider = data.get(key["provider_id"])
        if provider:
            provider["keys"].append({k: v for k, v in key.items() if k != "provider_id"})
    return list(data.values())


def insert_sql(table: str, columns: dict[str, str]) -> str:
    values = ", ".join(f":{field}" for field in columns)
    return f"INSERT INTO {table} ({', '.join(columns.values())}) VALUES ({values})"


def update_sql(table: str, columns: dict[str, str]) -> str:
    return f"UPDATE {table} SET " + ", ".join(
        f"{column} = :{field}" for field, column in columns.items()
    )


async def save_keys(db: Database, provider_id: int, keys: list[KeyInput]) -> None:
    existing = (
        (
            await db.execute(
                text("SELECT id FROM ai_provider_key WHERE provider_id = :id"), {"id": provider_id}
            )
        )
        .scalars()
        .all()
    )
    kept = set()
    for key in keys:
        params = {"provider": provider_id, "weight": key.weight, "enabled": key.isEnabled}
        if key.id is not None and (key.id not in existing or key.id in kept):
            raise AdminError("Invalid or duplicate provider key ID")
        if key.apiKey is not None:
            plain = key.apiKey.get_secret_value()
            if not plain.strip():
                raise AdminError("API key must not be blank")
            params.update(
                cipher=encrypt_secret(plain, get_settings().key_encrypt_master),
                mask=mask_secret(plain),
            )
        if key.id is None:
            if key.apiKey is None:
                raise AdminError("New keys require apiKey")
            await db.execute(
                text(
                    "INSERT INTO ai_provider_key (provider_id, weight, is_enabled, "
                    "key_ciphertext, key_mask) "
                    "VALUES (:provider, :weight, :enabled, :cipher, :mask)"
                ),
                params,
            )
        else:
            kept.add(key.id)
            params["id"] = key.id
            credential = (
                ", key_ciphertext = :cipher, key_mask = :mask, fail_count = 0"
                if (key.apiKey is not None)
                else ""
            )
            await db.execute(
                text(
                    "UPDATE ai_provider_key SET weight = :weight, "
                    f"is_enabled = :enabled{credential} WHERE id = :id"
                ),
                params,
            )
    for key_id in set(existing) - kept:
        await db.execute(text("DELETE FROM ai_provider_key WHERE id = :id"), {"id": key_id})


async def save_provider(db: Database, admin: int, body: Provider, provider_id: int | None):
    await lock_catalog(db)
    params = body.model_dump(exclude={"keys", "id"})
    params["baseUrl"] = str(body.baseUrl).rstrip("/")
    if provider_id is None:
        provider_id = await db.scalar(
            text(insert_sql("ai_provider", PROVIDER_COLUMNS) + " RETURNING id"), params
        )
        action = "provider.add"
    else:
        if not body.isEnabled:
            active = await db.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM ai_model "
                    "WHERE provider_id = :id AND status <> 'offline')"
                ),
                {"id": provider_id},
            )
            if active:
                raise AdminError("Take the provider's active models offline before disabling it")
        updated = await db.scalar(
            text(
                update_sql("ai_provider", PROVIDER_COLUMNS)
                + ", updated_at = now() WHERE id = :id RETURNING id"
            ),
            {**params, "id": provider_id},
        )
        if updated is None:
            raise AdminError("Provider not found", status=404)
        action = "provider.edit"
    if body.keys is not None:
        await save_keys(db, provider_id, body.keys)
    await audit(
        db,
        admin,
        action,
        "ai_provider",
        provider_id,
        providerType=body.providerType,
        protocol=body.protocol,
        timeoutMs=body.timeoutMs,
        rpmLimit=body.rpmLimit,
        isEnabled=body.isEnabled,
        keysUpdated=body.keys is not None,
    )
    data = (await providers(db, provider_id))[0]
    return ok({**data, "syncStatus": await commit_catalog(db)})


@router.get("/provider/list")
async def list_providers(db: Database, admin: Admin):
    return ok(await providers(db))


@router.post("/provider/add")
async def add_provider(body: Provider, db: Database, admin: Admin):
    return await save_provider(db, admin, body, None)


@router.put("/provider/edit")
async def edit_provider(body: ProviderEdit, db: Database, admin: Admin):
    return await save_provider(db, admin, body, body.id)


@router.delete("/provider/{provider_id}")
async def delete_provider(provider_id: Id, db: Database, admin: Admin):
    await lock_catalog(db)
    deleted = await db.scalar(
        text("DELETE FROM ai_provider WHERE id = :id RETURNING id"), {"id": provider_id}
    )
    if deleted is None:
        raise AdminError("Provider not found", status=404)
    await audit(db, admin, "provider.delete", "ai_provider", provider_id)
    return ok({"syncStatus": await commit_catalog(db)})


@router.post("/provider/{provider_id}/test")
async def test_provider(provider_id: Id, db: Database, admin: Admin):
    provider = (
        (
            await db.execute(
                text("SELECT base_url, protocol FROM ai_provider WHERE id = :id"),
                {"id": provider_id},
            )
        )
        .mappings()
        .first()
    )
    if provider is None:
        raise AdminError("Provider not found", status=404)
    key = (
        (
            await db.execute(
                text(
                    "SELECT id, key_ciphertext FROM ai_provider_key "
                    "WHERE provider_id = :id AND is_enabled "
                    "ORDER BY fail_count, weight DESC, id LIMIT 1"
                ),
                {"id": provider_id},
            )
        )
        .mappings()
        .first()
    )
    await db.commit()
    plain = decrypt_secret(key["key_ciphertext"], get_settings().key_encrypt_master) if key else ""
    headers = {}
    if provider["protocol"] == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if plain:
            headers["x-api-key"] = plain
    elif plain:
        headers["Authorization"] = f"Bearer {plain}"
    started = perf_counter()
    models = None
    try:
        async with asyncio.timeout(5), httpx.AsyncClient(trust_env=False, timeout=5) as client:
            response = await client.get(provider["base_url"] + "/models", headers=headers)
            response.raise_for_status()
            models = [
                item["id"]
                for item in response.json()["data"]
                if isinstance(item["id"], str) and (not plain or plain not in item["id"])
            ]
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
        pass
    elapsed = round((perf_counter() - started) * 1000)
    if key:
        await db.execute(
            text(
                "UPDATE ai_provider_key SET fail_count = CASE WHEN :success THEN 0 "
                "ELSE LEAST(fail_count, 2147483646) + 1 END "
                "WHERE id = :id AND key_ciphertext = :key_ciphertext"
            ),
            {**key, "success": models is not None},
        )
    await audit(
        db,
        admin,
        "provider.test",
        "ai_provider",
        provider_id,
        outcome="success" if models is not None else "failed",
        latencyMs=elapsed,
    )
    await db.commit()
    if models is None:
        raise AdminError("Provider connectivity test failed", ErrorCode.INTERNAL_ERROR, 502)
    return ok({"latencyMs": elapsed, "availableModels": models})


async def impact(db: Database, code: str) -> dict[str, int]:
    return dict(
        (
            await db.execute(
                text("""
        SELECT
            (SELECT count(*) FROM chat_session WHERE model_code = :code AND deleted_at IS NULL
                AND COALESCE(last_msg_at, created_at) >= now() - interval '30 days') AS sessions,
            (SELECT count(*) FROM ai_bot
                WHERE default_model_code = :code AND deleted_at IS NULL) AS bots,
            (SELECT count(*) FROM chat_schedule_task WHERE model_code = :code
                AND status = 'active' AND deleted_at IS NULL) AS schedules,
            (SELECT count(*) FROM ai_model_binding WHERE model_code = :code) AS bindings,
            (SELECT count(*) FROM ai_model
                WHERE fallback_model_code = :code) AS fallbacks
    """),
                {"code": code},
            )
        )
        .mappings()
        .one()
    )


async def save_model(db: Database, admin: int, body: Model, *, create: bool):
    await lock_catalog(db)
    params = body.model_dump()
    enabled = await db.scalar(
        text("SELECT is_enabled FROM ai_provider WHERE id = :id"), {"id": body.providerId}
    )
    if enabled is None or (body.status != "offline" and not enabled):
        raise AdminError("Select an enabled provider for an active model")
    if body.fallbackModelCode:
        fallback = await db.scalar(
            text("SELECT status FROM ai_model WHERE model_code = :code"),
            {"code": body.fallbackModelCode},
        )
        if body.fallbackModelCode == body.modelCode or fallback not in ("online", "canary"):
            raise AdminError("Fallback must be a different active model")
    if body.status == "offline":
        affected = await impact(db, body.modelCode)
        if affected["fallbacks"]:
            raise AdminError("Replace this model in other fallback configurations first")
        if any(affected.values()) and not body.fallbackModelCode:
            raise AdminError("Configure a fallback before taking a referenced model offline")
    if create:
        sql = insert_sql("ai_model", MODEL_COLUMNS)
    else:
        fields = {field: column for field, column in MODEL_COLUMNS.items() if field != "modelCode"}
        sql = update_sql("ai_model", fields) + " WHERE model_code = :modelCode"
    row = (await db.execute(text(sql + " RETURNING " + MODEL_FIELDS), params)).mappings().first()
    if row is None:
        raise AdminError("Model not found", status=404)
    await audit(
        db,
        admin,
        "model.add" if create else "model.edit",
        "ai_model",
        body.modelCode,
        **body.model_dump(mode="json", exclude={"modelCode", "upstreamModel", "displayName"}),
    )
    return ok({**row, "syncStatus": await commit_catalog(db)})


@router.get("/model/list")
async def list_models(db: Database, admin: Admin):
    rows = await db.execute(
        text("SELECT " + MODEL_FIELDS + " FROM ai_model ORDER BY sort_order, id")
    )
    return ok([dict(row) for row in rows.mappings()])


@router.post("/model/add")
async def add_model(body: Model, db: Database, admin: Admin):
    return await save_model(db, admin, body, create=True)


@router.put("/model/edit")
async def edit_model(body: Model, db: Database, admin: Admin):
    return await save_model(db, admin, body, create=False)


@router.get("/model/{code}/impact")
async def model_impact(code: ModelCode, db: Database, admin: Admin):
    exists = await db.scalar(
        text("SELECT id FROM ai_model WHERE model_code = :code"), {"code": code}
    )
    if exists is None:
        raise AdminError("Model not found", status=404)
    return ok(await impact(db, code))


@router.delete("/model/{code}")
async def delete_model(code: ModelCode, db: Database, admin: Admin):
    await lock_catalog(db)
    deleted = await db.scalar(
        text("DELETE FROM ai_model WHERE model_code = :code AND status = 'offline' RETURNING id"),
        {"code": code},
    )
    if deleted is None:
        raise AdminError("Only offline models can be deleted")
    await audit(db, admin, "model.delete", "ai_model", code)
    return ok({"syncStatus": await commit_catalog(db)})


@router.get("/model/bindings")
async def list_bindings(db: Database, admin: Admin):
    return ok(
        dict(
            (
                await db.execute(
                    text("SELECT purpose, model_code FROM ai_model_binding ORDER BY purpose")
                )
            ).all()
        )
    )


@router.put("/model/bindings")
async def edit_bindings(body: dict[Purpose, ModelCode | None], db: Database, admin: Admin):
    await lock_catalog(db)
    for purpose, code in sorted(body.items()):
        if code is None:
            await db.execute(
                text("DELETE FROM ai_model_binding WHERE purpose = :purpose"), {"purpose": purpose}
            )
            continue
        status = await db.scalar(
            text("SELECT status FROM ai_model WHERE model_code = :code"), {"code": code}
        )
        if status not in ("online", "canary"):
            raise AdminError("Bind only active models")
        await db.execute(
            text("""
            INSERT INTO ai_model_binding (purpose, model_code) VALUES (:purpose, :code)
            ON CONFLICT (purpose) DO UPDATE SET model_code = excluded.model_code, updated_at = now()
        """),
            {"purpose": purpose, "code": code},
        )
    await audit(db, admin, "model.bindings", "ai_model_binding", bindings=body)
    status = await commit_catalog(db)
    return ok({"bindings": (await list_bindings(db, admin)).data, "syncStatus": status})


@router.post("/model/testChat")
async def test_chat(body: TestChat, db: Database, admin: Admin):
    target = (
        (
            await db.execute(
                text("""
        SELECT p.timeout_ms, m.max_output FROM ai_model m
        JOIN ai_provider p ON p.id = m.provider_id
        WHERE m.model_code = :code AND m.status <> 'offline' AND p.is_enabled
    """),
                {"code": body.modelCode},
            )
        )
        .mappings()
        .first()
    )
    if target is None:
        raise AdminError("Choose an active model for online testing")
    await db.commit()
    settings = get_settings()
    headers = {}
    if settings.higress_gateway_key:
        headers["Authorization"] = f"Bearer {settings.higress_gateway_key.get_secret_value()}"
    payload = {
        "model": body.modelCode,
        "messages": [{"role": "user", "content": body.prompt}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": min(target["max_output"] or 512, 512),
    }
    timeout = target["timeout_ms"] / 1000
    started = perf_counter()
    ttft = None
    usage = None
    parts = []
    completed = False
    try:
        async with (
            asyncio.timeout(timeout),
            httpx.AsyncClient(trust_env=False, timeout=timeout) as client,
            client.stream(
                "POST",
                settings.higress_gateway_url.rstrip("/") + "/v1/chat/completions",
                headers=headers,
                json=payload,
            ) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    completed = True
                    break
                event = json.loads(chunk)
                if event.get("error"):
                    break
                if isinstance(event.get("usage"), dict):
                    usage = {
                        k: v
                        for k, v in event["usage"].items()
                        if k in ("prompt_tokens", "completion_tokens", "total_tokens")
                        and type(v) is int
                    }
                for choice in event.get("choices", []):
                    content = choice.get("delta", {}).get("content")
                    if isinstance(content, str) and content:
                        if ttft is None:
                            ttft = round((perf_counter() - started) * 1000)
                        parts.append(content)
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError, AttributeError):
        completed = False
    success = completed and ttft is not None
    await audit(
        db,
        admin,
        "model.test_chat",
        "ai_model",
        body.modelCode,
        outcome="success" if success else "failed",
        ttftMs=ttft,
    )
    await db.commit()
    if not success:
        raise AdminError("Higress chat request failed", ErrorCode.INTERNAL_ERROR, 502)
    return ok({"ttftMs": ttft, "text": "".join(parts), "usage": usage})
