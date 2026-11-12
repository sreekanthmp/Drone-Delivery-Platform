# syntax=docker/dockerfile:1

# ---- build stage: resolve and install runtime dependencies from the lockfile ----
FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv==0.11.32

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
# --frozen: use uv.lock exactly; --no-dev: no pytest/ruff/mypy/httpx in the image.
RUN uv sync --frozen --no-dev

# ---- runtime stage: just the virtualenv and the application code ----
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY app ./app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8000

# Migrations are applied separately (see docker-compose.yml); the image only serves the API.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
