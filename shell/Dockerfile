# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

# Workload Galaxy shell: the suite bar and module selector. Runs as non-root with a read-only root filesystem.
#
# Built from the repository root, because it uses the shared core package:
#   docker build -f shell/Dockerfile -t workload-galaxy-shell .
FROM python:3.12-slim

WORKDIR /app
COPY shell/app/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY shell/VERSION ./VERSION
COPY core/py/galaxy_core ./galaxy_core
COPY shell/app/galaxyshell ./galaxyshell
COPY shell/web ./web

ARG GALAXY_VERSION=dev
ARG GALAXY_BUILD=local
LABEL org.opencontainers.image.title="workload-galaxy-shell" \
      org.opencontainers.image.version="${GALAXY_VERSION}" \
      org.opencontainers.image.revision="${GALAXY_BUILD}"
ENV GALAXY_VERSION=${GALAXY_VERSION} \
    GALAXY_BUILD=${GALAXY_BUILD} \
    GALAXY_WEB_ROOT=/app/web \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN chgrp -R 0 /app && chmod -R g=u /app
USER 1001
EXPOSE 8080
CMD ["uvicorn", "galaxyshell.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
