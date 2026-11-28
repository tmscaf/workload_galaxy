# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Workload Galaxy core: what every module shares.

    settings    declare a module's settings once; load them from the environment or a saved file
    plugins     optional add-ons inside a module: their switches, settings and hooks
    nodes       build nodes, edges and findings in the graph format the front end draws
    graphops    scope a graph to some groups; summarise findings into totals and patterns
    scale       semantic zoom for large estates, and the server-side tables, detail and search
    state       the background refresh loop and the per-refresh caches
    webapp      the HTTP API and UI serving every module exposes

A module supplies what is its own: where the data comes from, how it becomes a graph, and what it
checks for. See modules/esxi for the first module built on this package.
"""
from __future__ import annotations

import os
import pathlib

__all__ = ["read_version"]


def read_version(env_var: str, package_file: str) -> str:
    """A module's version: baked into the image as an environment variable, else its VERSION file."""
    env = os.environ.get(env_var, "")
    if env and env != "dev":
        return env
    here = pathlib.Path(package_file).resolve()
    for candidate in (here.parents[2] / "VERSION", here.parents[1] / "VERSION"):
        try:
            return candidate.read_text().strip()
        except OSError:
            continue
    return "0.0.0-dev"
