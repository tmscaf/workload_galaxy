# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Workload Galaxy - the suite's front door: one bar across the top with the module selector, and
the selected module underneath.

Each module is its own service. The shell knows where they are, asks each one how it is doing,
and shows the one you pick in a frame (modules have an embed mode for exactly this).
"""
import os

from galaxy_core import read_version

__version__ = read_version("GALAXY_VERSION", __file__)
__build__ = os.environ.get("GALAXY_BUILD", "local")
