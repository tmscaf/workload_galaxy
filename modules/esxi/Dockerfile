# ESXi Galaxy: API + UI in one image. Runs as non-root with a read-only root filesystem.
#
# Built from the repository root, because the module uses the shared core package:
#   docker build -f modules/esxi/Dockerfile -t esxi-galaxy .
FROM python:3.12-slim

WORKDIR /app
COPY modules/esxi/collector/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY modules/esxi/VERSION ./VERSION
COPY core/py/galaxy_core ./galaxy_core
COPY modules/esxi/collector/esxigalaxy ./esxigalaxy
COPY modules/esxi/web ./web

# Version/build stamp (scripts/deploy-homelab.sh passes these; defaults keep plain `docker build` working).
ARG ESXIGALAXY_VERSION=dev
ARG ESXIGALAXY_BUILD=local
LABEL org.opencontainers.image.title="esxi-galaxy" \
      org.opencontainers.image.version="${ESXIGALAXY_VERSION}" \
      org.opencontainers.image.revision="${ESXIGALAXY_BUILD}"
ENV ESXIGALAXY_VERSION=${ESXIGALAXY_VERSION} \
    ESXIGALAXY_BUILD=${ESXIGALAXY_BUILD} \
    ESXIGALAXY_WEB_ROOT=/app/web \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN mkdir -p /data && chgrp -R 0 /app /data && chmod -R g=u /app /data
USER 1001
EXPOSE 8080
CMD ["uvicorn", "esxigalaxy.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
