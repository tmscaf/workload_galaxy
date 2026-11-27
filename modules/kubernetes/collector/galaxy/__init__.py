# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Workload Galaxy - topology, traffic and latency-risk explorer for Kubernetes / OpenShift projects.

Version comes from one place: the repo's VERSION file, baked into the image as GALAXY_VERSION
together with GALAXY_BUILD (a UTC build stamp) by tools/deploy-k3s.sh / the Dockerfile.
"""
import os
import pathlib


def _version() -> str:
    env = os.environ.get("GALAXY_VERSION", "")
    if env and env != "dev":
        return env
    here = pathlib.Path(__file__).resolve()
    for candidate in (here.parents[2] / "VERSION", here.parents[1] / "VERSION"):
        try:
            return candidate.read_text().strip()
        except OSError:
            continue
    return "0.0.0-dev"


__version__ = _version()
__build__ = os.environ.get("GALAXY_BUILD", "local")
