# Template workload image. Python standard library only: no apt and no pip in the build,
# so it builds offline from a Python base that is already on the machine.
# BASE_IMAGE is a build argument; the default is the base every workload here uses.
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

# The container runs with --read-only, so Python must not write .pyc files; unbuffered output
# makes logs appear at once.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Keep the uid equal to runtime.uid in workload.toml (the supervisor starts the container as that user).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin fleet
# app/ holds main.py and fleet_client.py (the SDK, byte-identical in every workload).
COPY app/ /app/
WORKDIR /app
USER 10001
ENTRYPOINT ["python3", "/app/main.py"]
