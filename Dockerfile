FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY fleet/ ./fleet/
COPY host/ ./host/
COPY deploy/ ./deploy/

RUN pip install --no-cache-dir ".[host]"

ENV FLEET_BIND=0.0.0.0:8080 \
    FLEET_DEPLOY_DIR=/app/deploy

EXPOSE 8080

CMD ["python", "-m", "host.main"]
