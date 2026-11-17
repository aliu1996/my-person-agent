FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
COPY packages/pa-shared packages/pa-shared
COPY services/chat-api services/chat-api
COPY services/chat-stream services/chat-stream
COPY services/search-service services/search-service
COPY services/worker services/worker

ARG PACKAGE=chat-api
RUN uv sync --frozen --no-dev --package ${PACKAGE}
ENV PATH="/app/.venv/bin:$PATH"
