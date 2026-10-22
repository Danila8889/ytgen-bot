# YouTubeGen Bot: a long-polling Telegram client. No port is exposed, no volume is needed.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_HTTP_TIMEOUT=120 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Dependencies first (cached layer), then the project.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 --no-create-home bot
USER bot

# `python -m ytgen_bot selfcheck` is the deploy check (getMe + one authenticated wrapper call).
CMD ["/app/.venv/bin/python", "-m", "ytgen_bot"]
