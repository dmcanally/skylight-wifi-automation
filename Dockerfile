FROM python:3.12-slim

# 3.12 does not enable VERIFY_X509_STRICT. The app also clears only that flag
# so the self-signed UCG certificate, which has no basicConstraints, can be
# the trust anchor. Hostname verification stays on.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
USER 10001
EXPOSE 8080
ENTRYPOINT ["skylight-wifi"]
