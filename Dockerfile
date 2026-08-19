# syntax=docker/dockerfile:1

FROM python:3.14-slim AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
# Pin the exact uv patch (matches setup-uv in CI) so image builds are reproducible,
# rather than tracking the floating 0.8 minor tag.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev

FROM python:3.14-slim AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 1000 pilot
WORKDIR /app
COPY --from=builder --chown=pilot:pilot /app /app
# WORKDIR created /app as root and COPY --chown only covers what it copies, so /app
# itself stays root-owned (the app must not rewrite its own code). The CV cache is the
# one path the runtime user writes, so create it and hand it over — otherwise the first
# Drive refresh dies on mkdir('cv') with a permission error.
RUN chmod +x /app/docker/entrypoint.sh && install -d -o pilot -g pilot /app/cv
USER pilot
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["daemon"]
