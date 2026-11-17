import json
import logging
from typing import Annotated, Any

import jwt
from fastapi import Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pa_shared import ErrorCode, fail, get_settings
from pa_shared.security import decode_jwt
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import Database, session_factory

logger = logging.getLogger(__name__)


class AdminError(Exception):
    def __init__(self, message: str, code: ErrorCode = ErrorCode.BAD_REQUEST, status: int = 400):
        self.message = message
        self.code = code
        self.status = status


def unauthorized() -> AdminError:
    return AdminError("Invalid administrator credentials or token", ErrorCode.UNAUTHORIZED, 401)


bearer = HTTPBearer(auto_error=False, scheme_name="Admin JWT")


async def require_admin(
    request: Request,
    db: Database,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> int:
    if credentials is None:
        raise unauthorized()
    try:
        claims = decode_jwt(credentials.credentials, get_settings().admin_jwt_secret, "admin")
        admin_id = int(claims["sub"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise unauthorized() from None
    if not 0 < admin_id < 2**63:
        raise unauthorized()
    active = await db.scalar(
        text("SELECT id FROM admin_user WHERE id = :id AND status = 'active'"), {"id": admin_id}
    )
    if active is None:
        raise unauthorized()
    request.state.admin_id = admin_id
    return admin_id


Admin = Annotated[int, Depends(require_admin)]


async def audit(
    db: AsyncSession,
    actor: int | None,
    action: str,
    target_type: str,
    target_id: int | str | None = None,
    **diff: Any,
) -> None:
    await db.execute(
        text(
            """
            INSERT INTO audit_log (actor_type, actor_id, action, target_type, target_id, diff_json)
            VALUES ('admin', :actor, :action, :type, :target, CAST(:diff AS jsonb))
            """
        ),
        {
            "actor": actor,
            "action": action,
            "type": target_type,
            "target": str(target_id) if target_id is not None else None,
            "diff": json.dumps(diff, default=str),
        },
    )


class AdminRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                error = AdminError("Invalid request fields")
            except IntegrityError:
                error = AdminError("Configuration conflicts with existing data")
            except AdminError as exc:
                error = exc
            actor = getattr(request.state, "admin_id", None)
            if (
                actor is not None
                and 400 <= error.status < 500
                and request.method
                in {
                    "POST",
                    "PUT",
                    "DELETE",
                }
            ):
                try:
                    async with session_factory().begin() as db:
                        await audit(
                            db,
                            actor,
                            self.name,
                            "admin_request",
                            self.path,
                            outcome="rejected",
                            code=int(error.code),
                        )
                except SQLAlchemyError as exc:
                    logger.warning("Could not write rejected admin audit (%s)", type(exc).__name__)
            return JSONResponse(
                status_code=error.status, content=fail(error.code, error.message).model_dump()
            )

        return wrapped
