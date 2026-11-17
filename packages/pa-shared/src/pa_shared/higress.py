import asyncio
import json
import logging
import re
from collections import defaultdict
from typing import Any

import httpx
from cryptography.exceptions import InvalidTag
from redis.asyncio import Redis
from redis.exceptions import LockError, RedisError
from sqlalchemy import BigInteger, Boolean, column, select, table
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pa_shared.security import decrypt_secret
from pa_shared.settings import Settings, get_settings

logger = logging.getLogger(__name__)
PROVIDERS = "/v1/ai/providers"
ROUTES = "/v1/ai/routes"
LOCK_KEY = "pa:higress:sync_lock"
CACHE_KEY = "pa:model:cache"
BINDINGS_KEY = "pa:model:bindings"
sync_state = table(
    "gateway_sync_state",
    column("id", Boolean),
    column("revision", BigInteger),
    column("synced_revision", BigInteger),
)
provider_rows = table(
    "ai_provider",
    column("id"),
    column("protocol"),
    column("base_url"),
    column("timeout_ms"),
    column("is_enabled", Boolean),
)
key_rows = table(
    "ai_provider_key",
    column("id"),
    column("provider_id"),
    column("key_ciphertext"),
    column("weight"),
    column("is_enabled", Boolean),
)
model_rows = table(
    "ai_model",
    *(
        column(name)
        for name in (
            "id",
            "model_code",
            "provider_id",
            "upstream_model",
            "display_name",
            "capabilities",
            "context_length",
            "max_output",
            "rate_in",
            "rate_cache",
            "rate_out",
            "fallback_model_code",
            "visible_scope",
            "status",
            "canary_ratio",
            "sort_order",
        )
    ),
)
binding_rows = table("ai_model_binding", column("purpose"), column("model_code"))


class ConsoleError(RuntimeError):
    pass


def console_data(response: httpx.Response) -> Any:
    if not response.is_success:
        raise ConsoleError(f"Console HTTP {response.status_code}")
    if not response.content:
        return None
    data = response.json()
    if not isinstance(data, dict) or data.get("success") is False:
        raise ConsoleError("Console operation rejected")
    return data.get("data")


async def list_resources(client: httpx.AsyncClient, path: str, prefix: str) -> dict:
    resources = console_data(await client.get(path))
    if not isinstance(resources, list):
        raise ConsoleError("Unexpected console resource list")
    return {row["name"]: row for row in resources if re.fullmatch(prefix + r"[0-9]+", row["name"])}


async def upsert_resource(client: httpx.AsyncClient, path: str, payload: dict, existing: dict):
    name = payload["name"]
    if name in existing:
        details = console_data(await client.get(f"{path}/{name}"))
        if "version" in details:
            payload = {**payload, "version": details["version"]}
        console_data(await client.put(f"{path}/{name}", json=payload))
    else:
        console_data(await client.post(path, json=payload))


def provider_payload(provider, keys: list[str]) -> dict:
    anthropic = provider["protocol"] == "anthropic"
    raw = (
        {"claudeCustomUrl": provider["base_url"] + "/messages", "claudeVersion": "2023-06-01"}
        if anthropic
        else {"openaiCustomUrl": provider["base_url"]}
    )
    return {
        "name": f"pa-provider-{provider['id']}",
        "type": "claude" if anthropic else "openai",
        "protocol": "openai/v1",
        "tokens": keys,
        "rawConfigs": {**raw, "timeout": provider["timeout_ms"]},
    }


def route_payload(model, target, fallback, settings: Settings) -> dict:
    auth = {"enabled": False}
    if settings.higress_gateway_consumer:
        auth = {
            "enabled": True,
            "allowedCredentialTypes": ["key-auth"],
            "allowedConsumers": [settings.higress_gateway_consumer],
        }
    route = {
        "name": f"pa-model-{model['id']}",
        "domains": [],
        "pathPredicate": {"matchType": "PRE", "matchValue": "/v1/", "caseSensitive": True},
        "modelPredicates": [{"matchType": "EQUAL", "matchValue": model["model_code"]}],
        "upstreams": [
            {
                "provider": f"pa-provider-{target['provider_id']}",
                "weight": 100,
                "modelMapping": {model["model_code"]: target["upstream_model"]},
            }
        ],
        "authConfig": auth,
        "fallbackConfig": {"enabled": False},
    }
    if fallback is not None and model["status"] != "offline":
        route["fallbackConfig"] = {
            "enabled": True,
            "fallbackStrategy": "SEQUENCE",
            "responseCodes": ["429", "500", "502", "503", "504"],
            "upstreams": [
                {
                    "provider": f"pa-provider-{fallback['provider_id']}",
                    "weight": 100,
                    "modelMapping": {model["model_code"]: fallback["upstream_model"]},
                }
            ],
        }
    return route


async def publish_console(settings: Settings, providers, keys, models) -> None:
    if not settings.higress_console_username or not settings.higress_console_password:
        raise ConsoleError("Console credentials unavailable")
    pool = defaultdict(list)
    for key in keys:
        plain = decrypt_secret(key["key_ciphertext"], settings.key_encrypt_master)
        pool[key["provider_id"]].extend([plain] * key["weight"])
    provider_payloads = [provider_payload(p, pool[p["id"]]) for p in providers]
    active = {row["model_code"]: row for row in models if row["status"] != "offline"}
    routes = []
    for model in models:
        fallback = active.get(model["fallback_model_code"])
        target = active.get(model["model_code"]) or fallback
        if target:
            routes.append(route_payload(model, target, fallback, settings))
    async with httpx.AsyncClient(
        base_url=settings.higress_console_url, timeout=5, trust_env=False
    ) as client:
        console_data(
            await client.post(
                "/session/login",
                json={
                    "username": settings.higress_console_username,
                    "password": settings.higress_console_password.get_secret_value(),
                },
            )
        )
        existing_providers = await list_resources(client, PROVIDERS, "pa-provider-")
        existing_routes = await list_resources(client, ROUTES, "pa-model-")
        for provider in provider_payloads:
            await upsert_resource(client, PROVIDERS, provider, existing_providers)
        for route in routes:
            await upsert_resource(client, ROUTES, route, existing_routes)
        for name in sorted(existing_routes.keys() - {route["name"] for route in routes}):
            response = await client.delete(f"{ROUTES}/{name}")
            if response.status_code != 404:
                console_data(response)
        for name in sorted(existing_providers.keys() - {p["name"] for p in provider_payloads}):
            response = await client.delete(f"{PROVIDERS}/{name}")
            if response.status_code != 404:
                console_data(response)


async def publish_pending(factory: async_sessionmaker[AsyncSession], redis: Redis) -> str:
    async with factory() as db:
        await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        revision, synced = (
            await db.execute(select(sync_state.c.revision, sync_state.c.synced_revision))
        ).one()
        if revision == synced and await redis.exists(CACHE_KEY, BINDINGS_KEY) == 2:
            return "synced"
        providers = (
            (
                await db.execute(
                    select(provider_rows)
                    .where(provider_rows.c.is_enabled)
                    .order_by(provider_rows.c.id)
                )
            )
            .mappings()
            .all()
        )
        keys = (
            (
                await db.execute(
                    select(key_rows.c.provider_id, key_rows.c.key_ciphertext, key_rows.c.weight)
                    .join(provider_rows, provider_rows.c.id == key_rows.c.provider_id)
                    .where(provider_rows.c.is_enabled, key_rows.c.is_enabled)
                    .order_by(key_rows.c.id)
                )
            )
            .mappings()
            .all()
        )
        models = (await db.execute(select(model_rows).order_by(model_rows.c.id))).mappings().all()
        bindings = dict((await db.execute(select(binding_rows))).all())
        await db.commit()
        await redis.set(BINDINGS_KEY, json.dumps(bindings))
        if revision != synced:
            await publish_console(get_settings(), providers, keys, models)
        await redis.set(CACHE_KEY, json.dumps([dict(row) for row in models], default=str))
        current = await db.scalar(
            sync_state.update()
            .where(sync_state.c.id)
            .values(
                synced_revision=revision,
            )
            .returning(sync_state.c.revision)
        )
        await db.commit()
        return "synced" if current == revision else "pending_retry"


async def sync_gateway(factory: async_sessionmaker[AsyncSession]) -> str:
    settings = get_settings()
    try:
        async with (
            asyncio.timeout(40),
            Redis.from_url(settings.redis_url, socket_timeout=3, socket_connect_timeout=3) as redis,
        ):
            async with redis.lock(LOCK_KEY, timeout=60, blocking_timeout=0.1):
                return await publish_pending(factory, redis)
    except LockError:
        pass
    except (
        RedisError,
        SQLAlchemyError,
        TimeoutError,
        httpx.HTTPError,
        ConsoleError,
        InvalidTag,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
    ) as exc:
        # Console failures can contain upstream credentials in their response bodies.
        logger.warning("Higress synchronization pending (%s)", type(exc).__name__)
    return "pending_retry"
