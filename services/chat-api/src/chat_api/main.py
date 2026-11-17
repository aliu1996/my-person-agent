from fastapi import FastAPI
from pa_shared import R, ok
from pa_shared.telemetry import instrument

from . import agent, turns
from .admin import auth, catalog, moderation, share_reviews, system, users
from .client import auth as user_auth
from .client import files, library, memory, profile, push, schedules, sessions, shares

app = FastAPI(title="chat-api", version="0.1.0")
instrument(app, "chat-api")
app.include_router(turns.router)
app.include_router(agent.router)
app.include_router(agent.public)
for router in (
    auth.router,
    catalog.router,
    system.router,
    share_reviews.router,
    moderation.router,
    users.router,
):
    app.include_router(router, prefix="/api/admin")
for router in (
    user_auth.router,
    profile.router,
    sessions.router,
    shares.router,
    files.router,
    library.router,
    schedules.router,
    memory.router,
    push.router,
):
    app.include_router(router, prefix="/api")


@app.get("/healthz", response_model=R[dict[str, str]])
async def healthz() -> R[dict[str, str]]:
    return ok({"service": "chat-api", "status": "healthy"})
