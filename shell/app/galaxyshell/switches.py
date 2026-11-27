# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Which modules are turned on.

A module that is off stays in the shell's list, but it is left out of the selector and the shell
stops asking it how it is doing. The module is a separate service and keeps running: the shell
starts and stops nothing.

Every module is on unless GALAXY_MODULES_OFF names it. A choice made on the Settings page wins over
that and is kept in the data folder (shell.json). Without a data folder it lasts until the shell
restarts.
"""
from __future__ import annotations

import json
import logging
import os
import threading

log = logging.getLogger(__name__)
FILE = "shell.json"


class Switches:
    def __init__(self, off=(), data_dir: str = ""):
        self._off = {str(i).strip().lower() for i in off if str(i).strip()}
        self.path = os.path.join(data_dir, FILE) if data_dir else ""
        self._lock = threading.Lock()
        self._chosen: dict[str, bool] = {}
        if self.path and os.path.isfile(self.path):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    saved = (json.load(fh) or {}).get("modules") or {}
                self._chosen = {str(k): bool(v.get("enabled")) for k, v in saved.items()
                                if isinstance(v, dict) and isinstance(v.get("enabled"), bool)}
            except (OSError, ValueError, AttributeError) as err:
                log.warning("ignoring %s, which could not be read: %s", self.path, err)

    @property
    def persistent(self) -> bool:
        return bool(self.path)

    def enabled(self, mid: str) -> bool:
        with self._lock:
            return self._chosen.get(mid, mid not in self._off)

    def set(self, mid: str, on: bool) -> None:
        """Raises OSError when there is a data folder and the choice could not be written to it."""
        with self._lock:
            chosen = {**self._chosen, mid: bool(on)}
            if self.path:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp = self.path + ".tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"modules": {k: {"enabled": v} for k, v in sorted(chosen.items())}}, fh, indent=1)
                os.replace(tmp, self.path)
            self._chosen = chosen
