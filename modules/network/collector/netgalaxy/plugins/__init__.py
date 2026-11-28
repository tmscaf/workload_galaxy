# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""The Network module's plugins: optional add-ons, each off until it is turned on in Settings.

The list below is the whole of it. Nothing is discovered or loaded at run time (see
galaxy_core.plugins for what a plugin is and the rules it follows).
"""
from galaxy_core.plugins import Plugins

from .adblock import AdBlocker

PREFIX = "NETGALAXY"
PLUGINS = Plugins([AdBlocker], prefix=PREFIX)
