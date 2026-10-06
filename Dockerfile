FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY fleet/ ./fleet/
COPY fleetagent/ ./fleetagent/
COPY host/ ./host/
COPY workloads/ ./workloads/
COPY deploy/ ./deploy/

RUN pip install --no-cache-dir ".[host]"

ENV FLEET_BIND=0.0.0.0:8080 \
    FLEET_DEPLOY_DIR=/app/deploy \
    FLEET_WORKLOADS_DIR=/app/workloads

EXPOSE 8080

CMD ["python", "-m", "host.main"]
