FROM ghcr.io/astral-sh/uv:0.8.22 AS uv
FROM python:3.12.13-slim-bookworm

COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy PLAYWRIGHT_BROWSERS_PATH=/opt/browsers \
    CAR_MARKET_WORKSPACE=/workspace PATH=/app/.venv/bin:$PATH
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra browser --extra reports --no-install-project \
    && .venv/bin/python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY car_market ./car_market
COPY LICENSE ./LICENSE
RUN uv sync --frozen --no-dev --extra browser --extra reports \
    && useradd --create-home --uid 10001 collector \
    && mkdir /workspace && chown collector:collector /workspace \
    && chmod -R a+rX /opt/browsers
USER collector
ENTRYPOINT ["car-market"]
CMD ["--help"]
