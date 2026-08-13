# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project

COPY reference_transfer ./reference_transfer

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

RUN mkdir -p /app/data

CMD ["uv", "run", "--locked", "reference-transfer", "work"]
