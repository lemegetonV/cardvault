# syntax=docker/dockerfile:1.7
# One recipe for every Python service and mock. ARG SERVICE picks what the container runs.
# Dependencies are installed in their own layer before any code is copied, so code changes
# rebuild in seconds and every service shares the same cached dependency layers.

FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
# The optional "build_ca" secret is only for building behind a TLS-intercepting proxy;
# normal builds (including CI) do not pass it.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=build_ca,required=false \
    if [ -f /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --frozen --no-dev --no-install-project

COPY shared ./shared
COPY services ./services
COPY mocks ./mocks
RUN python -m compileall -q shared services mocks && useradd --uid 10001 --no-create-home app
USER app

ARG SERVICE
ENV SERVICE=${SERVICE}
CMD ["python", "-m", "shared.entrypoint"]
