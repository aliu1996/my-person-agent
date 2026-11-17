import json
import math
from collections.abc import AsyncIterator

import httpx

from .settings import get_settings


class GatewayError(Exception):
    pass


def gateway_request(path: str) -> tuple[str, dict[str, str]]:
    settings = get_settings()
    headers = {}
    if settings.higress_gateway_key:
        headers["Authorization"] = f"Bearer {settings.higress_gateway_key.get_secret_value()}"
    return settings.higress_gateway_url.rstrip("/") + "/v1/" + path, headers


async def model_json(
    http: httpx.AsyncClient, model: str, system: str, content: str, *, max_tokens: int
) -> dict:
    url, headers = gateway_request("chat/completions")
    response = await http.post(
        url,
        headers=headers,
        json={
            "model": model,
            "stream": False,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
        },
        timeout=8,
    )
    response.raise_for_status()
    try:
        value = json.loads(response.json()["choices"][0]["message"]["content"])
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise GatewayError("Invalid model JSON response") from exc
    if not isinstance(value, dict):
        raise GatewayError("Invalid model JSON response")
    return value


async def embedding(http: httpx.AsyncClient, model: str, content: str) -> str:
    url, headers = gateway_request("embeddings")
    response = await http.post(
        url, headers=headers, json={"model": model, "input": content}, timeout=8
    )
    response.raise_for_status()
    try:
        values = response.json()["data"][0]["embedding"]
        if (
            len(values) != 1536
            or not any(values)
            or any(
                type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 3e38
                for value in values
            )
        ):
            raise ValueError
        return json.dumps(values, separators=(",", ":"))
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
        raise GatewayError("Embedding must be a finite, nonzero 1536-dimensional vector") from exc


async def sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """Read complete SSE data records, including a final record at EOF."""
    data: list[str] = []
    size = 0
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield "\n".join(data)
                data.clear()
                size = 0
        elif line.startswith("data:"):
            value = line[5:].removeprefix(" ")
            size += len(value)
            if size > 1_000_000:
                raise GatewayError("Oversized upstream SSE event")
            data.append(value)
    if data:
        yield "\n".join(data)
