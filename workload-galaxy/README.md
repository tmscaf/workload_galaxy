# Workload Galaxy

Topology, traffic and latency-risk explorer for Kubernetes / OpenShift projects.
It shows every workload, its pods and the hosts under them, who calls whom
(upstream / downstream, across projects), which way data is flowing, and where
latency is likely being introduced — as an animated 3D **Galaxy**, a layered
**Strata** view (end users → services → pods → hosts) and a **Cascade** view
(call-depth waterfall with failure propagation), all sharing one selection.

Built to be embedded in a larger suite: a versioned JSON API plus a UI that
runs in an iframe and talks `postMessage`.

| | |
|---|---|
| `collector/` | Python service: reads the cluster API, merges telemetry, scores risk, serves API + UI |
| `agent/` | Optional node agent (DaemonSet): live TCP/UDP flows, kernel RTT, retransmits, queue backpressure |
| `web/` | The UI — Three.js (vendored, no CDN, works air-gapped), no build step |
| `deploy/k8s` | Base manifests (kustomize) · `deploy/homelab` k3s/Rancher overlay · `deploy/openshift` OpenShift overlay |

## What you see

**Galaxy.** End users glow at the core. Each namespace/project is a spiral arm;
the dotted orbits are call depth (entry → services → their dependencies → …),
with external dependencies on the outer rim. Workloads are stars sized by pod
count; pods orbit them as hexagons. Comets stream along each call **in request
direction** — density is throughput, colour is latency risk, and high-latency
calls visibly crawl. Dashed links are dependencies inferred from config with no
traffic observed yet.

**Strata.** The same graph as stacked planes: End users, Services, Pods &
containers, Hosts, with dashed drop-lines from each workload to its pods and
from pods to the node they run on. Click anything to light its whole path from
users to hosts.

**Cascade.** The call graph as a left-to-right waterfall of columns: End users,
then one column per call hop, then External dependencies, then Infrastructure
(hosts). Each column steps down and back. Requests flow left to right. On top,
every **failure cascade** is drawn: a light pillar on each root cause, and red
shock pulses travelling *backwards* along the calls (and from a sick host into
the workloads on it) to everything it is slowing down. The rail lists cascades
by severity × blast radius ("ocp-worker-2 → 20 degraded dependents, main cause
for 11, reaches users"); click one to isolate it.

**Gibson.** The cluster as a city on a circuit board, seen isometrically. Each
workload is a glass tower standing on a plinth, with one floor per pod: amber/red
when failing, dashed above the roof when the scheduler can't place it. Projects are
districts, and calls run as right-angle circuit traces along the streets with
pulses in request direction. End users enter through a gate, external dependencies
plug into edge connectors on the rim, and hosts sit along the back. Tower faces
scroll that workload's live telemetry and issue cards. Click a tower (or a floor,
to pick a pod) to bring it front and center.

**Colour language.** Cyan→violet = healthy flow (gradient runs source → target),
amber = elevated, red = latency/failures. A dashed amber/red ring around a
healthy star means *it* is fine but something downstream is slowing it — the
detail drawer names the root cause.

**Left rail.** Layer counts, **Hotspots** (things with problems of their own,
worst first — where latency starts) and the **riskiest request paths** from
outside the cluster, with summed latency where measured.

## Where the data comes from

Each edge records its evidence; the UI shows it as chips.

| Evidence | Source | Needs |
|---|---|---|
| `declared` | Ingress / Route / HTTPRoute → Service → pods; LoadBalancer/NodePort exposure | read-only RBAC |
| `config` | Service names, `*.svc` FQDNs, ClusterIPs and external URLs found in env, args and mounted ConfigMaps | read-only RBAC (`GALAXY_READ_CONFIGMAPS`) |
| `observed` | Agent: every pod's TCP sockets (`ss` in the pod netns) + node conntrack (TCP & UDP) | agent DaemonSet (privileged) |
| `mesh` | Request rate, 5xx rate, p50/p95 from Istio / OpenShift Service Mesh metrics in Prometheus | `GALAXY_PROMETHEUS_URL` |
| `probe` | Optional TCP connect timing to Service VIPs and endpoints | `GALAXY_PROBES=true` |

Services are folded into edges (`api-gateway → payments-api via payments-api`);
a Service with no backing pods becomes its own red node. Ingress traffic is
routed through the ingress controller / OpenShift router when one is detected.
Only *where* a config reference was found is reported (`env DATABASE_URL`,
`configmap app:application.yaml`) — never the value. Secrets are never read.

### Latency / risk signals

| Signal | Meaning | Where |
|---|---|---|
| Request p95 > 100/300/1000 ms, 5xx > 1%/5% | slow or failing calls | mesh |
| Network path RTT (kernel *minimum* RTT) > 2 / 10 ms inside the cluster | slow or long network path. Internet distance is not scored | agent |
| Retransmitting segments | packet loss | agent |
| Unacked client send queue | receiver or path can't keep up | agent |
| Unread bytes at server (Recv-Q) | app not draining sockets — saturated | agent |
| Accept-queue backlog on listeners | app not accepting connections fast enough | agent |
| SYN-SENT / unreplied flows | connects into nothing (dead endpoints, policy drops) | agent |
| CPU ≥ 75/90% of limit | CFS throttling adds tail latency | metrics.k8s.io |
| Memory ≥ 80/90% of limit, OOMKilled | GC pauses, restarts | metrics.k8s.io |
| Unready pods, CrashLoop/ImagePull, recent restarts | lost capacity | API |
| Autoscaler pinned at max, single replica with many callers | no headroom | API |
| Calls crossing zones | extra network hops | API (node zone labels) |
| Node NotReady / Memory-, Disk-, PIDPressure | infrastructure | API |
| Node memory requests ≥ 90/98% of allocatable, limits > 1.5× | no room to (re)schedule; OOM risk under load | API |
| Pods Pending with no node (shown on the **Unscheduled** pseudo-host) | scheduler can't place them — reason kept verbatim | API |
| Evicted pods | kubelet reclaimed resources | API |
| Pod using ≥ 2 cores with no limit on a busy node | noisy neighbour | metrics.k8s.io |
| A node owning ≥ 60% of the cluster's retransmitting / slower-than-peers connections | bad NIC / node network | agent |
| Agent on a node stopped reporting | lost telemetry, often a dead node | agent |

Host trouble is attributed to the workloads on that node through **impact**
(`impactFrom: host:<name>`), not as their own fault — so hotspots stay root
causes and the victims point down at the node.

Scores combine with noisy-OR per node/edge. Each workload also carries an
**impact** score inherited from downstream (decayed per hop) with
`impactFrom` naming the deepest likely root cause.

## Run it

### Demo (no cluster)

```bash
python3 -m venv .venv && .venv/bin/pip install -r collector/requirements.txt
cd collector && GALAXY_MODE=demo ../.venv/bin/uvicorn galaxy.main:app --port 8080
# open http://localhost:8080
```

The demo is a synthetic OpenShift cluster with deliberate problems
(a CPU-throttled `fraud-check`, a DB with a full accept queue, a CrashLooping
pod, a Service with no pods, slow internet RTT to a payment provider).

### Fault scenarios (demo)

In demo mode a **Simulate** menu appears in the top bar. Each scenario mutates
the synthetic cluster (nodes, pods, metrics, agent socket data, mesh metrics)
and runs it through the normal detector, so you see exactly what Workload
Galaxy would report on a real cluster in that state. Scenarios combine, and the
left rail shows *what to look for* on each strata layer, with the layer where
the root cause appears marked.

| Scenario | Root cause shows on | Signature |
|---|---|---|
| Node memory pressure + evictions | Hosts, Pods | node red (MemoryPressure, 95% used but only ~27% *requested* → an unlimited pod), grey evicted hexes, replacements starting elsewhere |
| Memory reservations exhausted | Hosts | reservation gauge full (requests 97%) while the white usage line sits lower; limits overcommitted; a big job hangs off **Unscheduled** |
| Pods can't be scheduled | Hosts, Pods | red dashed **Unscheduled** box with pending hexes, each carrying the scheduler's reason (selector, taint + anti-affinity, insufficient CPU) |
| Node NotReady | Hosts | node unreachable, every pod on it red, agent silent; singletons there go critical |
| CPU noisy neighbour | Hosts, Pods | node CPU ~97%, one pod flagged "no CPU limit"; neighbours' latency impact points at the node |
| Packet loss on one node | Hosts | many amber/red calls on the services plane, healthy pods; node owns most retransmits / slow-vs-peer connections |
| Node disk pressure | Hosts, Pods | DiskPressure, ephemeral-storage eviction |
| OOMKill loop | Pods, Services | OOMKilled / CrashLoopBackOff hexes, 5xx on callers, hosts green |
| Stuck rollout | Pods | one ImagePullBackOff hex, two images on the workload, traffic unaffected |
| Database outage cascade | Services, Users | DB critical, unanswered connects from callers, impact propagates to user paths, hosts green |

Link straight to one: `/?view=strata&scenario=memory-pressure&select=host:ocp-worker-2`.
API: `GET/PUT /api/v1/demo/scenarios` (`{"active": [...]}`); preset with
`GALAXY_DEMO_SCENARIOS=a,b`. Not available outside demo mode.

### Against any cluster from your machine (read-only, no agent)

```bash
cd collector && KUBECONFIG=~/.kube/config ../.venv/bin/uvicorn galaxy.main:app --port 8080
```

Works with `oc login` tokens too. Without the agent you get declared + config
topology and pod health; live flows need the in-cluster agent.

### Home-lab k3s + Rancher

```bash
scripts/deploy-homelab.sh              # version from VERSION + a unique UTC build stamp
```

Builds an amd64 image, imports it on `node-1`, creates the **Workload Galaxy**
Rancher project + `workload-galaxy` namespace, the agent token Secret, and
applies `deploy/homelab`. UI: <http://192.168.1.20:8090> (or
`galaxy.192.168.1.20.sslip.io` once a pfSense Host Override exists).

### OpenShift

```bash
oc new-project workload-galaxy
oc -n workload-galaxy create secret generic galaxy-agent-token --from-literal=token="$(openssl rand -hex 32)"
# push the image to a registry the cluster can pull from, set it in deploy/openshift/kustomization.yaml
oc apply -k deploy/openshift
```

The collector runs under `restricted-v2` (non-root, arbitrary UID, read-only
root FS). The agent needs the `privileged` SCC (`deploy/openshift/scc.yaml`);
drop `agent.yaml` + `scc.yaml` if that won't be granted. The overlay wires
request latency from OpenShift Service Mesh via the cluster Thanos querier
(`cluster-monitoring-view`).

## Versioning

`VERSION` is the single source of truth; bump it for anything you'd want to tell
apart, and note it in [CHANGELOG.md](CHANGELOG.md). Every deploy is stamped
`<version>-<UTC build time>`, so even redeploys of the same version are distinct.
To see what is running:

| Where | What it shows |
|---|---|
| UI top bar | `v0.3.0` chip; hover for build, start time and each agent's version; turns amber if an agent differs |
| `GET /api/v1/version` | collector version + build, every agent's version/build/last report, `agentsMismatched` |
| Rancher / `kubectl` | image tag `workload-galaxy:0.3.0-20261111.2318`, label `app.kubernetes.io/version`, annotation `workload-galaxy/build` |
| Logs | first line of collector and agent: version and build |

An open page notices a redeploy and offers a reload.

## Configuration

| Variable | Default | |
|---|---|---|
| `GALAXY_MODE` | `cluster` | `demo` for synthetic data |
| `GALAXY_NAMESPACES` | *(all)* | restrict to these namespaces (also switches to per-namespace API calls — useful with project-scoped RBAC) |
| `GALAXY_EXCLUDE_NAMESPACES` | | never show these |
| `GALAXY_HIDE_SELF` | `true` | hide this tool's own namespace |
| `GALAXY_REFRESH_SECONDS` | `15` | snapshot interval |
| `GALAXY_READ_CONFIGMAPS` | `true` | config-based dependency inference |
| `GALAXY_AGENT_TOKEN` | | shared secret for agent reports (ingest is disabled without it) |
| `GALAXY_FLOW_WINDOW_SECONDS` | `60` | agent reports older than this are ignored |
| `GALAXY_FLOW_IDLE_SECONDS` | `60` | conntrack flows with no packet for this long are treated as finished; open sockets quieter than this are drawn as *idle* |
| `GALAXY_PROMETHEUS_URL` | | Prometheus/Thanos base URL for mesh metrics |
| `GALAXY_PROMETHEUS_TOKEN_FILE` / `_CA_FILE` / `_VERIFY_TLS` | | auth/TLS for the above |
| `GALAXY_PROBES` | `false` | active TCP connect probes (in-cluster only) |
| `GALAXY_PROBE_INTERVAL_SECONDS` | `60` | |
| `GALAXY_PROBE_SKIP_PORTS` | DB/broker ports | never probed: closing a socket pre-handshake counts as a connection error on MySQL and friends |
| `GALAXY_RESOLVE_EXTERNAL_HOSTS` | `true` | DNS-resolve config hostnames so observed external IPs get names |

Agent: `GALAXY_COLLECTOR_URL`, `GALAXY_AGENT_TOKEN`, `GALAXY_AGENT_INTERVAL` (10s), `GALAXY_CA_FILE`.

## API (for the suite)

All paths are relative, so the service works behind a path prefix (mount it at
`/something/` **with** the trailing slash). OpenAPI at `/api/docs`.

| | |
|---|---|
| `GET /api/v1/meta` | version, mode, which data sources are live, last error |
| `GET /api/v1/namespaces` | per namespace: workloads, pods, warning/critical counts, `platform` flag |
| `GET /api/v1/topology?ns=a,b&platform=true` | the graph: `nodes`, `edges`, `hotspots`, `paths`, `cascades`, `sources` |
| `GET /api/v1/hotspots?ns=…` | hotspots, riskiest paths and failure cascades |
| `POST /api/v1/agent/report` | agent ingest (`Authorization: Bearer <token>`, gzip OK) |
| `GET /healthz`, `/readyz` | probes |

With `ns` set, neighbours outside the selection are included with
`"ghost": true` so cross-project dependencies stay visible.

### Embedding

```html
<iframe src="https://galaxy.example/?embed=1&ns=payments&view=strata"></iframe>
```

URL params: `ns`, `platform=1`, `view=galaxy|strata|cascade|gibson`, `select=<node id>`,
`embed=1` (compact chrome), `rail=0` (hide left rail), `api=<collector base URL>`.

Messages out (`event.data.source === 'workload-galaxy'`): `{type:'ready'}`,
`{type:'select', selection}`. Messages in:

```js
frame.contentWindow.postMessage({ type: 'workload-galaxy:command', command: 'select', id: 'wl:payments/Deployment/payments-api' }, '*');
// command: 'select' | 'clear' | 'setView' (galaxy|strata|cascade|gibson) | 'setNamespaces' (namespaces, platform)
```

Node ids: `wl:<ns>/<Kind>/<name>`, `entry:<ns>/<Kind>/<name>`, `svc:<ns>/<name>`,
`ext:<host-or-ip>`, `host:<node>`, `clients`. Edge ids: `<source>-><target>`.

## Security

- Collector RBAC is read-only cluster-wide and excludes Secrets. Pod templates
  (which can hold env values) never leave the process.
- The agent is privileged with `hostPID` — it reads socket and conntrack
  metadata only, captures no payloads, writes nothing to the node, and has no
  Kubernetes API access. Reports are authenticated with a shared token.
- **The UI has no login of its own.** It reveals cluster topology. Keep it on
  trusted networks or put it behind the suite's gateway / an oauth-proxy before
  exposing it more widely. The home-lab overlay exposes it on the LAN only.

## Limits worth knowing

- One collector replica (agent reports are held in memory, ~1 min window).
- **What "latency" means without a mesh.** The agent reports two kernel RTTs per
  connection. *Path RTT* (the kernel's minimum) is the network itself and is what
  gets scored. *Smoothed RTT* includes the peer's delayed ACKs: on quiet or one-way
  connections such as Kubernetes watch streams it reads 5–12 ms on a path that is
  really ~0.02 ms. So it's shown, and compared between nodes (where the artifact
  cancels out), but never scored on its own. Neither is request latency; for
  request-level p95 use mesh or OTel metrics.
- **Staleness.** Conntrack keeps entries long after a flow ends (k3s: dead TCP up
  to 24 h, UDP 30–120 s). The agent turns each entry's remaining TTL into "seconds
  since last packet", and the collector drops flows quiet for longer than
  `GALAXY_FLOW_IDLE_SECONDS`. Unreplied UDP is not treated as failure (UDP has no
  handshake). Unanswered TCP SYNs are.
- **Single-node clusters.** Node-versus-peer network checks need at least two
  reporting nodes and are skipped otherwise.
- **No history.** Every number is current (last 5 min for mesh, last report for the agent).
- Conntrack byte rates need `net.netfilter.nf_conntrack_acct=1` on nodes;
  without it, UDP edges show connections only.
- Config inference is conservative (bare names only count in address-like
  keys) and can't see dependencies configured only through Secrets.

## Development

```bash
.venv/bin/pip install -r collector/requirements.txt pytest
.venv/bin/pytest tests
```

The demo source runs through the same builder as a real cluster, so it doubles
as the end-to-end fixture.
