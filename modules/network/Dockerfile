# Network Galaxy: API + UI in one image. Runs as non-root.
#
# Built from the repository root, because the module uses the shared core package:
#   docker build -f modules/network/Dockerfile -t network-galaxy .
FROM python:3.12-slim

WORKDIR /app
COPY modules/network/collector/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY modules/network/VERSION ./VERSION
COPY core/py/galaxy_core ./galaxy_core
COPY modules/network/collector/netgalaxy ./netgalaxy
COPY modules/network/web ./web

# Version/build stamp (scripts/deploy-homelab.sh passes these; defaults keep plain `docker build` working).
ARG NETGALAXY_VERSION=dev
ARG NETGALAXY_BUILD=local
LABEL org.opencontainers.image.title="network-galaxy" \
      org.opencontainers.image.version="${NETGALAXY_VERSION}" \
      org.opencontainers.image.revision="${NETGALAXY_BUILD}"
ENV NETGALAXY_VERSION=${NETGALAXY_VERSION} \
    NETGALAXY_BUILD=${NETGALAXY_BUILD} \
    NETGALAXY_WEB_ROOT=/app/web \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN mkdir -p /data && chgrp -R 0 /app /data && chmod -R g=u /app /data
USER 1001
EXPOSE 8080 5514/udp
CMD ["uvicorn", "netgalaxy.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
