import logging
from collections.abc import AsyncIterator
from typing import Annotated

import jwt
from fastapi import Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pa_shared import ErrorCode, fail, get_settings
from pa_shared.storage import StorageError
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ..database import Database

logger = logging.getLogger(__name__)
Id = Annotated[int, Field(gt=0, lt=2**63)]
bearer = HTTPBearer(auto_error=False, scheme_name="User JWT")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientError(Exception):
    def __init__(self, message: str, code: ErrorCode = ErrorCode.BAD_REQUEST, status: int = 400):
        self.message = message
        self.code = code
        self.status = status


def unauthorized() -> ClientError:
    return ClientError("Invalid user credentials or token", ErrorCode.UNAUTHORIZED, 401)


def not_found(message: str) -> ClientError:
    return ClientError(message, status=404)


def unavailable(message: str) -> ClientError:
    return ClientError(message, ErrorCode.INTERNAL_ERROR, 503)


async def redis_connection() -> AsyncIterator[Redis]:
    async with Redis.from_url(
        get_settings().redis_url, socket_timeout=3, socket_connect_timeout=3
    ) as redis:
        yield redis


Cache = Annotated[Redis, Depends(redis_connection)]


def user_claims(token: str, role: str) -> tuple[int, int]:
    try:
        claims = jwt.decode(
            token,
            get_settings().user_jwt_secret,
            algorithms=["HS256"],
            options={"require": ["exp", "iat", "sub", "ver"]},
        )
        user_id = int(claims["sub"])
        version = claims["ver"]
        if claims.get("role") == role and 0 < user_id < 2**63 and type(version) is int:
            return user_id, version
    except (jwt.InvalidTokenError, TypeError, ValueError):
        pass
    raise unauthorized()


async def active_user(db: Database, user_id: int, version: int, *, write: bool) -> RowMapping:
    lock = "FOR UPDATE" if write else "FOR SHARE"
    user = (
        (
            await db.execute(
                text(
                    "SELECT * FROM sys_user WHERE id = :id AND status = 'active' "
                    f"AND token_version = :version {lock}"
                ),
                {"id": user_id, "version": version},
            )
        )
        .mappings()
        .first()
    )
    if user is None:
        raise unauthorized()
    return user


async def require_user(
    request: Request,
    db: Database,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> RowMapping:
    if credentials is None:
        raise unauthorized()
    user_id, version = user_claims(credentials.credentials, "user")
    return await active_user(db, user_id, version, write=request.method != "GET")


User = Annotated[RowMapping, Depends(require_user)]


class ClientRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request: Request):
            try:
                response = await handler(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except RequestValidationError:
                error = ClientError("Invalid request fields")
            except RedisError:
                error = unavailable("Authentication or job queue is temporarily unavailable")
            except StorageError:
                error = unavailable("Object storage is temporarily unavailable")
            except IntegrityError:
                error = ClientError("Request conflicts with existing data")
            except SQLAlchemyError as exc:
                logger.warning("Client API database request failed (%s)", type(exc).__name__)
                error = unavailable("Database is temporarily unavailable")
            except ClientError as exc:
                error = exc
            return JSONResponse(
                status_code=error.status,
                content=fail(error.code, error.message).model_dump(),
                headers={"Cache-Control": "no-store"},
            )

        return wrapped
