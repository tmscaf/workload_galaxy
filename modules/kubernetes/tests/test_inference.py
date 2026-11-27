# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

from galaxy.inference import ServiceIndex, infer, match_value

SERVICES = [
    {"metadata": {"name": "game-mysql", "namespace": "game"}, "spec": {"clusterIP": "10.43.0.180"}},
    {"metadata": {"name": "web", "namespace": "shop"}, "spec": {"clusterIP": "10.43.1.1"}},
    {"metadata": {"name": "orders-db", "namespace": "data"}, "spec": {"clusterIP": "10.43.2.2"}},
]
IDX = ServiceIndex(SERVICES)


def refs(value, ns="game", key="X", from_file=False):
    return [(r.kind, r.key) for r in match_value(value, ns, IDX, key=key, detail="d", from_file=from_file,
                                                  allow_external=True)]


def test_bare_service_name_needs_address_like_key():
    assert refs("game-mysql", key="GAME_MYSQL_HOST") == [("svc", "svc:game/game-mysql")]
    assert refs("game-mysql", key="REALM_NAME") == []


def test_fqdn_and_url_forms():
    assert refs("jdbc:postgresql://orders-db.data.svc.cluster.local:5432/orders", ns="shop") == [("svc", "svc:data/orders-db")]
    assert refs("http://orders-db.data:5432", ns="shop") == [("svc", "svc:data/orders-db")]
    assert refs("redis://web:6379/0", ns="shop") == [("svc", "svc:shop/web")]


def test_bare_word_in_other_namespace_or_prose_is_ignored():
    # 'web' is a service in shop, not in game; and plain prose must not match.
    assert refs("web", key="UPSTREAM_HOST") == []
    assert refs("the web frontend is slow", ns="shop", key="DESCRIPTION") == []


def test_cluster_ip_and_external_hosts():
    assert refs("10.43.0.180:3306", key="DB") == [("svc", "svc:game/game-mysql")]
    assert refs("https://api.stripe.com/v1", key="PAYMENT_API_URL") == [("external", "api.stripe.com")]
    assert refs("https://kubernetes.io/docs", key="DOCS_URL") == []


def test_config_files_skip_comments_and_bare_hostports():
    corefile = "# see https://docs.k3s.io/networking\nrancher.192.168.1.20.sslip.io:53 {\n hosts {}\n}"
    assert refs(corefile, key="Corefile", from_file=True) == []
    yaml = "spring:\n  datasource:\n    url: jdbc:mysql://game-mysql:3306/app  # main db\n"
    assert refs(yaml, key="application.yaml", from_file=True) == [("svc", "svc:game/game-mysql")]


def test_infer_follows_envfrom_configmaps_and_never_returns_values():
    spec = {"containers": [{"name": "login", "envFrom": [{"configMapRef": {"name": "game-server"}}],
                            "env": [{"name": "GAME_SERVER_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}]}]}
    cms = [{"metadata": {"name": "game-server", "namespace": "game"},
            "data": {"GAME_MYSQL_HOST": "game-mysql", "GAME_MYSQL_PORT": "3306", "GAME_WORLD_PUBLIC_HOST": "192.168.1.20"}}]
    out = infer({"wl:game/Deployment/game-login": ("game", spec)}, SERVICES, cms)
    rs = out["wl:game/Deployment/game-login"]
    svc = [r for r in rs if r.kind == "svc"]
    assert [r.key for r in svc] == ["svc:game/game-mysql"]
    assert svc[0].detail == "env GAME_MYSQL_HOST (configmap game-server)"
    assert all("game-mysql" not in r.detail.split("(")[0] for r in rs)  # detail names the key, not the value
