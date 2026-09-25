FROM python:3.12-slim

ARG VCS_REF=unknown
ARG VERSION=1.4.1

LABEL org.opencontainers.image.title="NetBird Complementary Operator" \
      org.opencontainers.image.source="https://github.com/runik-platform/netbird-complementary-operator" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY webhook/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY webhook/__init__.py /app/webhook/__init__.py
COPY webhook/main.py /app/webhook/main.py
COPY webhook/netbird_client.py /app/webhook/netbird_client.py
COPY webhook/keycloak_client.py /app/webhook/keycloak_client.py
COPY webhook/handlers/ /app/webhook/handlers/

RUN useradd -u 65532 -m -s /bin/false app && chown -R app:app /app
USER 65532

EXPOSE 8080
ENTRYPOINT ["uvicorn", "webhook.main:app", "--host", "0.0.0.0", "--port", "8080"]
