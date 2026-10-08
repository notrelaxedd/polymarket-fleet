# Stage 1: build the 3D fleet page (fleet-ui/, React + three.js, Vite base /fleet/).
# The owner never runs npm: `docker compose build` does it here.
FROM node:22-slim AS ui

WORKDIR /ui

COPY fleet-ui/package.json fleet-ui/package-lock.json ./
RUN npm ci

COPY fleet-ui/ ./
RUN npm run build

# Stage 2: the host image.
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

# The built page (index.html and assets/) where host/api/dashboard_fleet3d.py looks first.
COPY --from=ui /ui/dist ./host/fleet_ui/

ENV FLEET_BIND=0.0.0.0:8080 \
    FLEET_DEPLOY_DIR=/app/deploy \
    FLEET_UI_DIR=/app/host/fleet_ui \
    FLEET_WORKLOADS_DIR=/app/workloads

EXPOSE 8080

CMD ["python", "-m", "host.main"]
