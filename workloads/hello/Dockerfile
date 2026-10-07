# hello workload: Python standard library only, no apt and no pip.
# BASE_IMAGE is a build argument so a build can use any Python base that is already local.
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# uid 10001 matches runtime.uid in workload.toml: the supervisor runs the container as that user.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin fleet
COPY app/ /app/
WORKDIR /app
USER 10001
ENTRYPOINT ["python3", "/app/main.py"]
