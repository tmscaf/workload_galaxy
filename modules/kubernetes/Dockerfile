# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

# One image, two roles:
#   collector (default CMD)  - API + UI, runs as non-root / arbitrary UID (OpenShift restricted-v2)
#   agent                    - `python /app/agent.py`, DaemonSet with hostPID + privileged
FROM python:3.12-slim

# iproute2 -> ss (TCP RTT / queues), util-linux -> nsenter, conntrack -> fallback when
# /proc/net/nf_conntrack is not available (RHEL CoreOS, newer kernels).
RUN apt-get update \
 && apt-get install -y --no-install-recommends iproute2 util-linux conntrack \
 && rm -rf /var/lib/apt/lists/*

# Built from the repository root, because the collector uses the shared core package:
#   docker build -f modules/kubernetes/Dockerfile -t workload-galaxy .
WORKDIR /app
COPY modules/kubernetes/collector/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY modules/kubernetes/VERSION ./VERSION
COPY core/py/galaxy_core ./galaxy_core
COPY modules/kubernetes/collector/galaxy ./galaxy
COPY modules/kubernetes/agent/agent.py ./agent.py
COPY modules/kubernetes/web ./web

# Version/build stamp (tools/deploy-k3s.sh passes these; defaults keep plain `docker build` working).
ARG GALAXY_VERSION=dev
ARG GALAXY_BUILD=local
LABEL org.opencontainers.image.title="workload-galaxy" \
      org.opencontainers.image.version="${GALAXY_VERSION}" \
      org.opencontainers.image.revision="${GALAXY_BUILD}" \
      org.opencontainers.image.source="https://github.com/tmscaf/workload_galaxy" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
ENV GALAXY_VERSION=${GALAXY_VERSION} \
    GALAXY_BUILD=${GALAXY_BUILD} \
    GALAXY_WEB_ROOT=/app/web \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# OpenShift runs with a random UID in group 0.
RUN chgrp -R 0 /app && chmod -R g=u /app
USER 1001
EXPOSE 8080
CMD ["uvicorn", "galaxy.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
