import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import sha256
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pa_shared import ErrorCode, R, fail, get_settings, ok
from pa_shared.internal import INTERNAL_TOKEN_HEADER, valid_internal_token
from pa_shared.telemetry import instrument
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

Provider = Literal["bocha", "tavily"]
HAS_HAN = re.compile(r"[\u3400-\u9fff\U00020000-\U000323af]")


class SearchRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: Annotated[str, Field(min_length=1, max_length=1000)]
    lang: Literal["zh", "en"] | None = None


class Source(BaseModel):
    title: str
    url: str
    domain: str
    snippet: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with (
        httpx.AsyncClient(trust_env=False) as http,
        Redis.from_url(
            get_settings().redis_url, socket_timeout=1, socket_connect_timeout=1
        ) as redis,
    ):
        app.state.http = http
        app.state.redis = redis
        yield


app = FastAPI(title="search-service", version="0.1.0", lifespan=lifespan)
instrument(app, "search-service")


async def provider_search(http: httpx.AsyncClient, provider: Provider, query: str):
    settings = get_settings()
    key = settings.bocha_api_key if provider == "bocha" else settings.tavily_api_key
    if not key or not key.get_secret_value():
        return None
    headers = {"Authorization": f"Bearer {key.get_secret_value()}"}
    try:
        async with asyncio.timeout(4):
            if provider == "bocha":
                response = await http.post(
                    "https://api.bochaai.com/v1/web-search",
                    headers=headers,
                    json={"query": query, "count": 5, "summary": True},
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") != 200:
                    return None
                entries = payload["data"].get("webPages", {}).get("value", [])
            else:
                response = await http.post(
                    "https://api.tavily.com/search",
                    headers=headers,
                    json={
                        "query": query,
                        "max_results": 5,
                        "search_depth": "basic",
                    },
                )
                response.raise_for_status()
                entries = response.json()["results"]
            if not isinstance(entries, list):
                return None
            items = []
            seen = set()
            for entry in entries[:10]:
                if not isinstance(entry, dict):
                    continue
                url = entry.get("url")
                if not isinstance(url, str) or len(url) > 2048:
                    continue
                try:
                    parsed = urlsplit(url)
                    domain = parsed.hostname
                except ValueError:
                    continue
                if (
                    parsed.scheme not in {"http", "https"}
                    or not domain
                    or parsed.username
                    or parsed.password
                    or url in seen
                ):
                    continue
                seen.add(url)
                title = entry.get("name") or entry.get("title") or domain
                snippet = entry.get("summary") or entry.get("snippet") or entry.get("content") or ""
                items.append(
                    Source(
                        title=str(title)[:300], url=url, domain=domain, snippet=str(snippet)[:1500]
                    )
                )
            return items[:5]
    except (TimeoutError, httpx.HTTPError, ValueError, KeyError, AttributeError, TypeError):
        return None


@app.post("/internal/search")
async def search(
    body: SearchRequest,
    request: Request,
    token: Annotated[str, Header(alias=INTERNAL_TOKEN_HEADER)] = "",
):
    if not valid_internal_token(token):
        return JSONResponse(status_code=401, content=fail(ErrorCode.UNAUTHORIZED).model_dump())
    query = " ".join(body.query.split())
    cache_key = "search:cache:" + sha256(query.encode()).hexdigest()
    redis = request.app.state.redis
    try:
        cached = await redis.get(cache_key)
        if cached:
            return ok(json.loads(cached))
    except (RedisError, ValueError):
        pass

    providers: tuple[Provider, Provider] = ("tavily", "bocha")
    if body.lang == "zh" or (body.lang is None and HAS_HAN.search(query)):
        providers = ("bocha", "tavily")
    for provider in providers:
        items = await provider_search(request.app.state.http, provider, query)
        if items is None:
            continue
        data = {"items": [item.model_dump() for item in items], "provider": provider}
        try:
            await redis.set(cache_key, json.dumps(data, ensure_ascii=False), ex=900)
        except RedisError:
            pass
        return ok(data)
    return JSONResponse(
        status_code=503,
        content=fail(
            ErrorCode.INTERNAL_ERROR,
            "Search providers are unavailable",
        ).model_dump(),
    )


@app.get("/healthz", response_model=R[dict[str, str]])
async def healthz() -> R[dict[str, str]]:
    return ok({"service": "search-service", "status": "healthy"})
