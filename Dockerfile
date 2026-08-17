# syntax=docker/dockerfile:1

FROM python:3.14-slim-bookworm@sha256:23c59390fc717bf09f9336908199a0ae75d9c4264bf296123f94ad772fea3b52 AS builder

WORKDIR /build

ARG PIP_TRUSTED_HOST
ARG PIP_INDEX_URL
ARG PIP_FALLBACK_INDEX_URL=https://packagefeedproxy.microsoft.io/pypi/simple/

COPY requirements.txt .
RUN pip wheel --no-cache-dir --retries 1 --wheel-dir /wheels -r requirements.txt \
    || pip wheel --no-cache-dir --index-url "${PIP_FALLBACK_INDEX_URL}" \
        --wheel-dir /wheels -r requirements.txt

FROM python:3.14-slim-bookworm@sha256:23c59390fc717bf09f9336908199a0ae75d9c4264bf296123f94ad772fea3b52

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=5000

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt .
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY --chown=app:app app.py .
COPY --chown=app:app service_health ./service_health

USER app

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "60", "--access-logfile", "-", "app:web_app"]
