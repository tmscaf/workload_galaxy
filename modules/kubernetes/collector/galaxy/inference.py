"""Infer upstream -> downstream dependencies from workload configuration.

Scans container env, args/command and referenced ConfigMaps for hostnames that
match cluster Services (any of: bare name, name.ns, name.ns.svc[.cluster.local])
or ClusterIPs, and for external hostnames that look like dependencies.

Only *where* a reference was found is ever reported (env var name, ConfigMap
key) - never the value, which may contain credentials. Secrets are not read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .util import is_ip, name_of, ns_of

# Env-var names that usually hold an address of something we call.
ADDR_KEY = re.compile(
    r"(HOST|ADDR|URL|URI|ENDPOINT|SERVER|SERVICE|DSN|CONN|BROKER|UPSTREAM|BACKEND|TARGET|REMOTE|"
    r"API|DATABASE|_DB|DB_|REDIS|KAFKA|AMQP|RABBIT|MONGO|POSTGRES|PG_|MYSQL|ELASTIC|CASSANDRA|"
    r"NATS|PEERS?|NODES|GATEWAY|PROXY|SMTP|LDAP|VAULT|ETCD|ZOOKEEPER|MEMCACHE|COLLECTOR|OTLP)",
    re.I,
)
SPLIT = re.compile(r"[\s,;'\"()\[\]{}<>|]+")
HOSTPORT = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?):(\d{1,5})(?:/.*)?$")
HOSTNAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
# Hosts that show up in config as documentation / schema references, not dependencies.
IGNORED_EXTERNAL = re.compile(
    r"(^|\.)(example\.(com|org|net)|w3\.org|xmlns\.com|json-schema\.org|schema\.org|apache\.org|"
    r"kubernetes\.io|k8s\.io|localhost|localdomain|svc|cluster\.local|github\.com|gitlab\.com|"
    r"docker\.io|quay\.io|gcr\.io|ghcr\.io|registry\.k8s\.io|helm\.sh|openshift\.io)$",
    re.I,
)
COMMENT = re.compile(r"^\s*(#|//|;|--|<!--)")
TLD_OK = re.compile(r"\.[a-z]{2,24}$", re.I)
SCHEMES_FOR_FILES = {
    "http", "https", "grpc", "grpcs", "ws", "wss", "postgres", "postgresql", "mysql", "mariadb",
    "redis", "rediss", "amqp", "amqps", "mongodb", "mongodb+srv", "kafka", "nats", "tcp", "ldap",
    "ldaps", "smtp", "sqlserver", "oracle", "cassandra", "memcached", "s3",
}


@dataclass(frozen=True)
class Ref:
    kind: str          # "svc" | "ip" | "external"
    key: str           # svc id ("svc:ns/name"), ip address, or external hostname
    detail: str        # where it was found (never the value)
    port: int | None = None


@dataclass
class _Token:
    host: str
    port: int | None
    form: str          # "url" | "hostport" | "bare"
    scheme: str | None = None


def _tokens(value: str):
    for raw in SPLIT.split(value):
        raw = raw.strip().strip(".")
        if not raw or len(raw) > 512:
            continue
        if raw.lower().startswith("jdbc:"):
            raw = raw[5:]
        if "://" in raw:
            try:
                parts = urlsplit(raw)
                host = parts.hostname
                port = parts.port
            except ValueError:
                continue
            if host:
                yield _Token(host.lower(), port, "url", parts.scheme.lower())
            continue
        m = HOSTPORT.match(raw)
        if m:
            yield _Token(m.group(1).lower(), int(m.group(2)), "hostport")
            continue
        if "/" not in raw and "=" not in raw:
            yield _Token(raw.lower(), None, "bare")


class ServiceIndex:
    """Lookup tables the matcher needs: services by name and by ClusterIP."""

    def __init__(self, services: list[dict]):
        self.by_ns_name: dict[tuple[str, str], str] = {}
        self.by_ip: dict[str, str] = {}
        for svc in services:
            ns, name = ns_of(svc), name_of(svc)
            sid = f"svc:{ns}/{name}"
            self.by_ns_name[(ns, name)] = sid
            spec = svc.get("spec") or {}
            for ip in [spec.get("clusterIP")] + list(spec.get("clusterIPs") or []):
                if ip and ip != "None":
                    self.by_ip[ip] = sid
        self.namespaces = {ns for ns, _ in self.by_ns_name}


def match_value(value: str, own_ns: str, idx: ServiceIndex, *, key: str, detail: str,
                from_file: bool, allow_external: bool) -> list[Ref]:
    """Find service / external references inside one configuration value."""
    refs: list[Ref] = []
    key_says_addr = bool(ADDR_KEY.search(key or ""))
    if from_file:
        value = "\n".join(line for line in value.splitlines() if not COMMENT.match(line))
    whole = value.strip().lower()
    for tok in _tokens(value):
        host = tok.host
        strong_form = tok.form in ("url", "hostport")
        if tok.form == "url" and from_file and tok.scheme not in SCHEMES_FOR_FILES:
            continue
        if is_ip(host):
            if host in idx.by_ip:
                refs.append(Ref("svc", idx.by_ip[host], detail, tok.port))
            elif strong_form or key_says_addr:
                refs.append(Ref("ip", host, detail, tok.port))
            continue
        if not HOSTNAME.match(host):
            continue
        labels = host.split(".")
        # name.ns.svc / name.ns.svc.cluster.local (any cluster domain): unambiguous.
        if len(labels) >= 3 and labels[2] == "svc":
            sid = idx.by_ns_name.get((labels[1], labels[0]))
            if sid:
                refs.append(Ref("svc", sid, detail, tok.port))
            continue
        if len(labels) == 2 and (labels[1], labels[0]) in idx.by_ns_name:
            if strong_form or whole == host or key_says_addr:
                refs.append(Ref("svc", idx.by_ns_name[(labels[1], labels[0])], detail, tok.port))
                continue
        if len(labels) == 1:
            sid = idx.by_ns_name.get((own_ns, host))
            if sid and (strong_form or (whole == host and key_says_addr and not from_file)):
                refs.append(Ref("svc", sid, detail, tok.port))
            continue
        if (allow_external and len(labels) >= 2 and TLD_OK.search(host)
                and not IGNORED_EXTERNAL.search(host)
                and (tok.form == "url" or (tok.form == "hostport" and key_says_addr and not from_file)
                     or (whole == host and key_says_addr and not from_file))):
            refs.append(Ref("external", host, detail, tok.port))
    return refs


def _configmap_lookup(configmaps: list[dict]) -> dict[tuple[str, str], dict]:
    out = {}
    for cm in configmaps:
        data = dict(cm.get("data") or {})
        out[(ns_of(cm), name_of(cm))] = {k: v for k, v in data.items() if isinstance(v, str) and len(v) <= 65536}
    return out


def references_for_pod_spec(pod_spec: dict, ns: str, idx: ServiceIndex,
                            configmaps: dict[tuple[str, str], dict]) -> list[Ref]:
    """All references made by one pod template (containers + init containers)."""
    refs: list[Ref] = []
    containers = list(pod_spec.get("initContainers") or []) + list(pod_spec.get("containers") or [])
    for c in containers:
        for env in c.get("env") or []:
            name = env.get("name", "")
            if "value" in env and env["value"]:
                refs += match_value(str(env["value"]), ns, idx, key=name, detail=f"env {name}",
                                    from_file=False, allow_external=True)
                continue
            cmref = ((env.get("valueFrom") or {}).get("configMapKeyRef") or {})
            if cmref:
                data = configmaps.get((ns, cmref.get("name", "")), {})
                val = data.get(cmref.get("key", ""))
                if val:
                    refs += match_value(val, ns, idx, key=name, detail=f"env {name} (configmap {cmref.get('name')})",
                                        from_file=False, allow_external=True)
        for src in c.get("envFrom") or []:
            cm = (src.get("configMapRef") or {}).get("name")
            if not cm:
                continue
            prefix = src.get("prefix", "")
            for k, v in configmaps.get((ns, cm), {}).items():
                refs += match_value(v, ns, idx, key=prefix + k, detail=f"env {prefix}{k} (configmap {cm})",
                                    from_file=False, allow_external=True)
        for i, arg in enumerate(list(c.get("command") or []) + list(c.get("args") or [])):
            refs += match_value(str(arg), ns, idx, key="", detail=f"{c.get('name', 'container')} args",
                                from_file=True, allow_external=True)
    # ConfigMaps mounted as files (application.yaml, nginx.conf, ...)
    for vol in pod_spec.get("volumes") or []:
        sources = []
        if vol.get("configMap"):
            sources.append(vol["configMap"].get("name"))
        for proj in ((vol.get("projected") or {}).get("sources") or []):
            if proj.get("configMap"):
                sources.append(proj["configMap"].get("name"))
        for cm in filter(None, sources):
            if cm == "kube-root-ca.crt" or cm.endswith("service-ca.crt") or cm.startswith("openshift-service-ca"):
                continue
            for k, v in configmaps.get((ns, cm), {}).items():
                refs += match_value(v, ns, idx, key=k, detail=f"configmap {cm}:{k}",
                                    from_file=True, allow_external=True)
    return refs


def infer(workload_templates: dict[str, tuple[str, dict]], services: list[dict],
          configmaps: list[dict]) -> dict[str, list[Ref]]:
    """workload_templates: workload id -> (namespace, pod spec). Returns id -> refs (deduplicated)."""
    idx = ServiceIndex(services)
    cms = _configmap_lookup(configmaps)
    out: dict[str, list[Ref]] = {}
    for wid, (ns, spec) in workload_templates.items():
        seen: dict[tuple[str, str, str], Ref] = {}
        for ref in references_for_pod_spec(spec or {}, ns, idx, cms):
            seen.setdefault((ref.kind, ref.key, ref.detail), ref)
        if seen:
            out[wid] = list(seen.values())
    return out
