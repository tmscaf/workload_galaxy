"""A module's settings, declared once.

Each module lists its settings as `Field`s. From that one list come:
    - loading from environment variables (PREFIX_NAME), which is how a container is configured
    - loading from a saved dict, which is how the suite's module page will configure a built-in module
    - the schema the module page renders a form from (label, help, type, default, secret)

Secrets are never included in the schema's values and never echoed back by `public()`.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from types import SimpleNamespace

log = logging.getLogger(__name__)
TRUE = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Field:
    name: str                      # attribute name on the loaded settings, e.g. "refresh_seconds"
    kind: str = "str"              # str | int | bool | list | path
    default: object = ""
    label: str = ""                # what a person sees on the module page
    help: str = ""
    secret: bool = False           # a password or token: write-only in any UI
    required: bool = False         # the module cannot run in live mode without it
    minimum: int | None = None
    choices: tuple = ()
    group: str = ""                # heading the field sits under on the module page
    advanced: bool = False

    def env(self, prefix: str) -> str:
        return f"{prefix}_{self.name.upper()}"

    def parse(self, raw):
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            return list(self.default) if self.kind == "list" else self.default
        if self.kind == "bool":
            return raw if isinstance(raw, bool) else str(raw).strip().lower() in TRUE
        if self.kind == "int":
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return self.default
            return max(self.minimum, value) if self.minimum is not None else value
        if self.kind == "list":
            return list(raw) if isinstance(raw, (list, tuple)) else [x.strip() for x in str(raw).split(",") if x.strip()]
        value = str(raw).strip()
        if self.choices and value not in self.choices:
            return self.default
        return value


def load(fields: list[Field], prefix: str, values: dict | None = None, environ=None) -> SimpleNamespace:
    """Settings from a saved dict (wins) and the environment. Unknown keys in `values` are ignored."""
    environ = os.environ if environ is None else environ
    values = values or {}
    out = SimpleNamespace()
    for f in fields:
        raw = values[f.name] if f.name in values else environ.get(f.env(prefix))
        setattr(out, f.name, f.parse(raw))
    return out


def missing(fields: list[Field], settings) -> list[str]:
    """Labels of required settings that are empty."""
    return [f.label or f.name for f in fields if f.required and not getattr(settings, f.name, None)]


def schema(fields: list[Field], prefix: str) -> list[dict]:
    """What the module page needs to draw a form. Carries defaults, never current values."""
    return [{"name": f.name, "env": f.env(prefix), "type": f.kind, "label": f.label or f.name, "help": f.help,
             "default": None if f.secret else f.default, "secret": f.secret, "required": f.required,
             "choices": list(f.choices), "group": f.group, "advanced": f.advanced} for f in fields]


def public(fields: list[Field], settings) -> dict:
    """Current values with every secret reduced to whether it is set."""
    return {f.name: (bool(getattr(settings, f.name, "")) if f.secret else getattr(settings, f.name, None)) for f in fields}


def load_json(path: str) -> dict:
    """A JSON file of things that cannot be observed. Missing or broken means 'none', never a crash."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        log.warning("cannot read %s: %s", path, err)
        return {}
