"""A module's settings, declared once.

Each module lists its settings as `Field`s. From that one list come:
    - loading from environment variables (PREFIX_NAME), which is how a container is configured
    - loading from a saved dict, which is how the suite's module page will configure a built-in module
    - the schema the module page renders a form from (label, help, type, default, secret)

Secrets are never included in the schema's values and never echoed back by `public()`.

`Store` adds settings saved from a page: a small JSON file in the module's data folder that wins
over the environment. A deployment still starts from environment variables; whatever is saved
later replaces them without a redeploy.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from types import SimpleNamespace

log = logging.getLogger(__name__)
TRUE = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Field:
    name: str                      # attribute name on the loaded settings, e.g. "refresh_seconds"
    kind: str = "str"              # str | int | bool | list | path | text (several lines)
    default: object = ""
    label: str = ""                # what a person sees on the module page
    help: str = ""
    secret: bool = False           # a password or token: write-only in any UI
    required: bool = False         # the module cannot run in live mode without it
    minimum: int | None = None
    choices: tuple = ()
    group: str = ""                # heading the field sits under on the module page
    advanced: bool = False
    editable: bool = True          # False: set where the module is deployed, never from a page
    bound_to: tuple = ()           # a secret is forgotten when one of these fields changes without it being re-entered

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
        value = str(raw).replace("\r\n", "\n").strip() if self.kind == "text" else str(raw).strip()
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
             "choices": list(f.choices), "group": f.group, "advanced": f.advanced, "editable": f.editable,
             "minimum": f.minimum} for f in fields]


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


class SettingsError(ValueError):
    """What was sent cannot be saved; the message says why in words a person can act on."""


class Store:
    """Settings saved from a page, layered over the environment.

    `path` is a JSON file (mode 0600); without one, saved values live in memory and are lost on
    restart, which is fine for trying things out and is reported as `persistent: False`.
    """

    def __init__(self, fields: list[Field], prefix: str, path: str = "", environ=None):
        self.fields, self.prefix, self.path = fields, prefix, path
        self.environ = os.environ if environ is None else environ
        self.by_name = {f.name: f for f in fields}
        self._lock = threading.Lock()
        self.saved: dict = {}
        if path:
            data = load_json(path)
            self.saved = {k: v for k, v in data.items() if k in self.by_name and self.by_name[k].editable}

    def effective(self):
        with self._lock:
            return load(self.fields, self.prefix, dict(self.saved), self.environ)

    def describe(self) -> dict:
        """The form: every editable field with its current value and where that value comes from."""
        current = self.effective()
        out = []
        for row in schema(self.fields, self.prefix):
            f = self.by_name[row["name"]]
            if not f.editable:
                continue
            value = getattr(current, f.name)
            source = "saved" if f.name in self.saved else "environment" if self.environ.get(f.env(self.prefix)) else "default"
            out.append({**row, "value": None if f.secret else value, "set": bool(value) if f.secret else None, "source": source})
        return {"fields": out, "persistent": bool(self.path), "missing": missing(self.fields, current)}

    def merged(self, changes: dict) -> tuple[dict, list[str]]:
        """-> (what would be saved, secrets that were forgotten). Raises SettingsError on anything unusable."""
        if not isinstance(changes, dict):
            raise SettingsError("settings must be sent as an object of name: value")
        current = self.effective()
        new = dict(self.saved)
        touched = set()
        for name, raw in changes.items():
            f = self.by_name.get(name)
            if f is None or not f.editable:
                raise SettingsError(f"{name} is not a setting that can be changed here")
            if f.secret and (raw is None or raw == ""):
                continue                                   # left empty: keep what is there
            if f.kind == "int":
                try:
                    int(raw)
                except (TypeError, ValueError):
                    raise SettingsError(f"{f.label or name} must be a whole number") from None
            if f.choices and str(raw).strip() not in f.choices:
                raise SettingsError(f"{f.label or name} must be one of: {', '.join(f.choices)}")
            value = f.parse(raw)
            if value != getattr(current, name) or name in self.saved:
                new[name] = value
            if f.secret or value != getattr(current, name):
                touched.add(name)
        # A secret belongs to the place it was entered for. Pointing the module somewhere else
        # without re-entering it must not send the old one there.
        dropped = []
        for f in self.fields:
            if f.secret and f.name not in touched and any(b in touched for b in f.bound_to) and getattr(current, f.name):
                new[f.name] = ""
                dropped.append(f.label or f.name)
        return new, dropped

    def preview(self, changes: dict):
        """Settings as they would be with these changes, without saving anything."""
        new, _dropped = self.merged(changes)
        return load(self.fields, self.prefix, new, self.environ)

    def update(self, changes: dict) -> list[str]:
        """Save. Returns the secrets that were forgotten because what they belong to changed."""
        new, dropped = self.merged(changes)
        with self._lock:
            self.saved = new
            if self.path:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = self.path + ".tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(new, fh, indent=1)
                os.replace(tmp, self.path)
        return dropped
