FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim

# 3.12 does not enable VERIFY_X509_STRICT. The app also clears only that flag
# so the self-signed UCG certificate, which has no basicConstraints, can be
# the trust anchor. Hostname verification stays on.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    PATH="/app/.venv/bin:$PATH"

# /data holds app.db. Mount a volume writable by uid 10001.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown app:app /data \
    && chmod 0700 /data
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
USER 10001
VOLUME ["/data"]
EXPOSE 8080
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('UI_PORT', '8000') + '/healthz', timeout=3)"
ENTRYPOINT ["skylight-wifi"]
