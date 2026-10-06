# The Polymarket workload image (docs/workloads-design.md section 7).
#
# Only a Python runtime and bootstrap.py: no Polymarket code is baked in. On first start
# the bootstrap downloads the worker tarball from the host's /dl into /state/app, the
# same code a native worker runs, and then execs `python3 -m fleet.worker run`.
#
# Production base: debian:trixie-slim, the Debian 13 python3 package (the same build a
# native worker uses). A base that already ships python3 (python:3.13-slim, used only
# where apt cannot run, such as an offline build sandbox) skips the apt step.
ARG BASE_IMAGE=debian:trixie-slim
FROM ${BASE_IMAGE}

RUN set -eu; \
    if ! command -v python3 >/dev/null 2>&1; then \
      apt-get update; \
      apt-get install -y --no-install-recommends python3 ca-certificates; \
      rm -rf /var/lib/apt/lists/*; \
    fi; \
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; \
    useradd --uid 10001 --user-group --no-create-home --home-dir /state --shell /usr/sbin/nologin fleet; \
    mkdir -p /opt/fleet /state /scratch /run/fleet/secrets; \
    chown 10001:10001 /state /scratch

COPY bootstrap.py /opt/fleet/bootstrap.py
RUN chmod 0755 /opt/fleet && chmod 0644 /opt/fleet/bootstrap.py

USER 10001:10001
ENTRYPOINT ["python3", "/opt/fleet/bootstrap.py"]
