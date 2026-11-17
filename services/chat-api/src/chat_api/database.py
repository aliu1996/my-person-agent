from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from pa_shared.db import create_engine, create_session_factory
from sqlalchemy.ext.asyncio import AsyncSession


@lru_cache
def session_factory():
    return create_session_factory(create_engine())


async def database() -> AsyncIterator[AsyncSession]:
    async with session_factory()() as session:
        yield session


Database = Annotated[AsyncSession, Depends(database)]
