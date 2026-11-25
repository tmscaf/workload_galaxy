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

The top bar has two sections. **Triage** is the dashboard: what is wrong and what to
do. **Topology** is the live map of the same cluster, with four views (Galaxy, Strata,
Cascade, Gibson). The Triage button carries the number of open problems wherever you
are, and *Show on map* on any problem jumps to it in the topology.

**Triage** (start here when something is broken). One screen that answers *what is
wrong, why, and what do I do*:

- **Verdict and platform strip** — how many problems need attention, and whether the
  cluster itself is healthy: nodes ready, node pressure, OpenShift cluster operators,
  an upgrade or machine-config rollout in progress, and unhealthy platform components
  (DNS, ingress, pod network). Platform trouble is shown whatever project filter is
  set, because it explains failures inside projects.
- **Projects** — one tile per project, worst first: workloads healthy / degraded /
  down, pods ready, restarts in the last hour, a rollout in progress, quota usage,
  and the top problem. Click a tile to scope everything to that project.
- **Problems** — one card per thing that is wrong, root causes first. Each finding
  says **Why** (the likely cause, in words) and **Next** (what to do), with the
  evidence it is based on (the API server's, scheduler's or kubelet's own message)
  and copy-ready `oc` / `kubectl` commands. Effects are folded under their cause
  ("image can't be pulled" → "so: rollout stuck"), and downstream symptoms (slow or
  failing callers) are folded under the root cause they clear with. Filter by
  category: Rollout, Crashing, Image, Config, Admission, Quota, Scheduling, Storage,
  Probes, Availability, Network, Latency & errors, Saturation, Nodes, Platform, Jobs.
- **Workloads** — every workload in one sortable table: ready, latest rollout (state,
  revision, age), restarts, CPU and memory against limits, requests/s, inbound p95
  and error rate (what its callers experience), its slowest dependency, top issue.
- **Calls** — every call, upstream → downstream: requests/s, connections, p50/p95,
  5xx, network path RTT, retransmits / unanswered connects, evidence, and whether it
  is a root cause or a symptom of something else.
- **What changed** — rollouts (with what changed in the pod template: image, env
  names, resources, probes), restarts with exit codes, warning events, node
  transitions, autoscaler activity, failed jobs. Newest first: a recent change is
  the first suspect.

The Triage tab carries a badge with the number of open problems in every view, and
every card has **Show on map** to jump to the same thing in the topology.

Clicking anything opens the detail drawer, which now also shows the **Diagnosis**
for that workload or call, its **latest rollout**, **warning events**, **storage
claims**, and, for a pod, each container's state, last exit code and reason (and
its last log lines, if you enable that; see Security).

**Galaxy.** End users glow at the core. Each namespace/project is a spiral arm;
the dotted orbits are call depth (entry → services → their dependencies → …),
with external dependencies on the outer rim. Workloads are stars sized by pod
count; pods orbit them as hexagons. Comets stream along each call **in request
direction** — density is throughput, colour is latency risk, and high-latency
calls visibly crawl. Dashed links are dependencies inferred from config with no
traffic observed yet.

**Strata.** The same graph as stacked planes: End users (called *Internet* when
internet services sit there with them), Services, Pods & containers, Hosts, with dashed drop-lines from each workload to its pods and
from pods to the node they run on. Click anything to light its whole path from
users to hosts.

**Cascade.** The call graph as a left-to-right waterfall of columns: End users,
then one column per call hop, then Your network (LAN), Internet, then
Infrastructure (hosts). Each column steps down and back. Requests flow left to right. On top,
every **failure cascade** is drawn: a light pillar on each root cause, and red
shock pulses travelling *backwards* along the calls (and from a sick host into
the workloads on it) to everything it is slowing down. The rail lists cascades
by severity × blast radius ("ocp-worker-2 → 20 degraded dependents, main cause
for 11, reaches users"); click one to isolate it.

**Gibson.** The cluster as a city on a circuit board, seen isometrically. Each
workload is a glass tower standing on a plinth, with one floor per pod: amber/red
when failing, dashed above the roof when the scheduler can't place it. Projects are
districts, and calls run as right-angle circuit traces along the streets with
pulses in request direction. An **internet gateway** arch stands on the board's front
edge: beyond it, on its own lower plane off the board, is the internet, with end users
in a *coming in* zone and the internet services your workloads call in a *called
from the cluster* zone beside them. Every
call between the cluster and the internet, in either direction, runs through the
arch. Your own network stays inside: its connectors sit on a green zone at the
board's right rim. Hosts sit along the back. Tower faces
scroll that workload's live telemetry and issue cards. Click a tower (or a floor,
to pick a pod) to bring it front and center.

**Where an external dependency lives.** Everything outside the cluster is
classified and coloured the same way in every view: **green = your network**
(private address ranges, `GALAXY_INTERNAL_CIDRS`, or internal names such as
`*.internal`, `*.local`, `*.corp`), **gold = the internet** (public addresses and
public hostnames), **pale blue = the cluster's own infrastructure** (API server,
node services). Healthy calls fade from cyan into that colour; slow or failing
calls stay amber/red so problems still stand out. The detail drawer says why each
one was classified as it was.

**End users vs users on your network.** Inbound traffic is split the same way when
the node agent can see the real source address: private addresses become *Users on
your network* (green, drawn inside the gateway), public ones stay *End users* on the
internet. Behind an external load balancer or SNAT the source is hidden, so those
users stay with End users.

**Colour language.** Cyan→violet = healthy flow (gradient runs source → target),
amber = elevated, red = latency/failures. A dashed amber/red ring around a
healthy star means *it* is fine but something downstream is slowing it — the
detail drawer names the root cause.

**Left rail.** Layer counts, **Hotspots** (things with problems of their own,
worst first — where latency starts) and the **riskiest request paths** from
outside the cluster, with summed latency where measured.

## Large estates

The same page serves a three-workload home cluster and an estate with thousands, and
neither should have to think about it. Nothing here needs configuring for a small
cluster: below the limits it is drawn workload by workload, exactly as described above.

**Topology zooms by meaning, not by pixels.** Above `GALAXY_DETAIL_LIMIT` (150)
workloads in view, the unit drawn is the **project**; above `GALAXY_PROJECT_LIMIT`
(60) projects it is the **business unit**: the part of the namespace name before the
first `-` (`retail-web`, `retail-api` → `retail`), or a namespace label
(`GALAXY_GROUP_BY=label:<key>`). A unit is drawn like a workload whose pods are its
members: a star with its projects in orbit, a tower with a floor per project, worst
first. Its colour says how much of it is in trouble, not merely whether anything is,
so one failing workload among 700 makes a unit amber rather than red. Calls between
units merge into one line (summed traffic, worst latency, how many of them are in
trouble); healthy low-traffic bundles are drawn only for the unit you select.

- **Double-click a unit** (or *Open on the map* in its drawer) to open it in place:
  a business unit into its projects, a project into its workloads. Everything else
  stays rolled up. The rail lists what is open and closes it again.
- At most `GALAXY_NODE_BUDGET` (320) things are ever drawn; when you open more, the
  oldest expansion closes, and the rail says so.
- **Problems only** (bottom bar, any cluster size) draws just what is failing, worst
  first, each with what calls it and what it depends on, and rolls everything healthy
  up into its project or unit. On a large estate it shows as many as stay readable
  (about 140 things) and says how many more there are.
- Healthy nodes group by zone; a node with a problem is always shown on its own.
- A workload with hundreds of pods (a DaemonSet on every node) is still one star and
  one tower: the worst 36 pods / 18 floors are drawn, the rest are counted.
- In Cascade, a hop with more than 12 members wraps into side-by-side lanes.
- Scoping to one project with the Projects filter shows it in full detail, as before.
- Search, the detail drawer and *Show on map* reach every workload through the
  server, whether or not it is on the page.

**Triage folds repetition.** The same problem on four or more workloads is reported
once, as a **shared cause** ("133 workloads in 119 projects: crash-looping,
application error (exit 1)"), with where it concentrates and a few examples; *Show
all* lists the members. Projects appear as an **estate grid**, one square each,
grouped by business unit, next to tiles for the worst few. Problems page 30 at a
time; the Workloads and Calls tables are filtered, sorted and paged by the server and
default to *only problems*. Totals always count everything in scope.

Measured on the simulated estate (606 projects, 11,451 workloads, 27,341 pods, 394
nodes): the collector builds the picture in about 2.6 s, and a page load is one
60–90 KB request for 30–100 drawn things instead of a 20 MB download.

One limit to know: the collector still lists every object from the API server on
each refresh. That is fine for thousands of pods; for tens of thousands, raise
`GALAXY_REFRESH_SECONDS` (60 or more). A watch-based cache is the proper fix and is
not built yet.

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

### What Triage can diagnose

| Category | Detected | From |
|---|---|---|
| Rollout | stuck or failed rollouts (Deployment, StatefulSet, DaemonSet, DeploymentConfig), with the revision, how long ago, and what changed | ReplicaSets / ControllerRevisions / RCs, rollout conditions |
| Image | tag doesn't exist, registry refuses access (pull secret), registry unreachable, untrusted certificate, rate limit, wrong architecture | container state + `Failed` events |
| Crashing | crash loops with the exit code translated (1 app error, 126/127 bad command, 137 killed, 139 segfault, 143 terminated, OOMKilled with the limit), init-container failures | container last state |
| Config | missing ConfigMap / Secret / key (with the likely typo), container can't be created, volume references that don't exist | container state, `FailedMount` events |
| Admission | pod rejected by SCC / Pod Security, admission webhook, LimitRange, missing ServiceAccount | `FailedCreate` events, `ReplicaFailure` condition |
| Quota | ResourceQuota exhausted or above 90%, and the workloads it is blocking | ResourceQuotas + events |
| Scheduling | why a pod is Pending: insufficient CPU/memory, taints, node selector / affinity, anti-affinity, unbound claim, cordoned nodes, pod limit | scheduler message |
| Storage | claims not bound (missing StorageClass, no default class, provisioner failing), lost volumes, Multi-Attach, attach/mount failures | PVCs + events |
| Probes | readiness / startup probe failing (refused, 404, 5xx, timeout) and containers killed by a liveness probe; compares the probe port with the ports the pod really listens on | `Unhealthy` events + agent |
| Availability | no ready pods, stuck in ContainerCreating / init / Terminating | pod status |
| Network | Service selects no pods; Service targetPort the pods don't listen on; Ingress/Route pointing at a missing Service or port; Route not admitted; Ingress without a controller; NetworkPolicy that blocks a call; certificates expiring or not issued; unanswered connects, retransmits | Services, EndpointSlices, Ingress/Route status, NetworkPolicies, cert-manager, agent |
| Latency & errors | slow or failing calls, each marked as a root cause or as a symptom of the dependency behind it | mesh metrics, agent |
| Saturation | CPU throttling, memory near limit, accept queue full, unread socket data, autoscaler at max or unable to scale | metrics.k8s.io, agent, HPA status |
| Nodes | NotReady, memory/disk/PID pressure, reservations exhausted, packet loss on one node, cordoned | Nodes + agent |
| Platform | OpenShift cluster operators degraded or unavailable, cluster upgrade, MachineConfigPool updating or degraded | config.openshift.io |
| Jobs | failed Jobs and CronJob runs (finished pods aren't on the maps) | Job conditions |
| Right-sizing | workloads reserving far more CPU or memory than they use (information only, and only after watching for an hour) | requests vs. peak usage |
| Hygiene | no readiness probe, no memory limit, no requests, `:latest` tags, single replica (information only, hidden by default) | pod templates |

It does not read Secrets, and it only reports what the API states: a finding's
*Why* is the most likely cause for that signal, and the evidence quoted under it
is the cluster's own message, so you can check it.

### Ways to fix it: sizing advice

When a workload is the wrong size, the finding carries a numbered list of **ways to fix
it**: what to change, to what value, why that value, and the command to copy. The
**Wrong-sized** filter in Triage lists every finding that has one.

| Problem | Options it offers |
|---|---|
| OOMKilled, or memory close to its limit | the limit (and request) to set; a runtime hint when the image is a JVM, Node.js, Go or a datastore; how to tell a leak from a limit that is too small |
| CPU throttled | a higher limit; the replica count that brings each pod back to ~70%; an autoscaler |
| Pod can't be scheduled (insufficient CPU / memory) | what each pod asks for against the most any node has free and the largest node; a smaller request sized from what its running pods use; which other workloads hold reservations they don't use; adding a node |
| Project quota exhausted | the quota value that fits what is running plus what is missing; what the project's own workloads could give back; rolling out without a surge pod |
| Quota requires requests / limits the pod doesn't set, or a LimitRange refuses it | the values to set |
| Autoscaler pinned at its maximum | the `maxReplicas` the load calls for (from current vs. target utilisation) and how many more pods the nodes can take; bigger pods instead |
| Autoscaler can't compute a replica count | the missing CPU request |
| Node out of memory or CPU, or its reservations exhausted | the pods using far more than they requested, and what to request instead |
| Pods evicted | an honest memory request; a second replica |
| Reserving far more than it uses | the requests to lower to |

Every suggestion is checked against what would stop it working, and says so under
**Check first**: the project quota doesn't have that much left, a LimitRange caps the
container below the new value, or no node has that much unreserved. Options that need
rights a project owner usually doesn't have are tagged **cluster admin**, and a workload
deployed by Helm or Argo CD is flagged, because a manual edit there is reverted.

The numbers come from live usage and the highest usage the collector has seen since it
started (kept in memory, up to a day). Until it has watched a workload for ten minutes
the advice says it is working from a single reading. It is a starting point for a
conversation with the workload's owner, not a capacity plan: check a busy period before
lowering anything. **The dashboard never applies a change.** RBAC stays read-only; the
commands are for a person to review and run.

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

### Showcase on your machine (Docker, no cluster)

```sh
scripts/demo-local.sh              # "A bad release day": six deployments failing six different ways
scripts/demo-local.sh network      # wiring and network faults
scripts/demo-local.sh infrastructure
scripts/demo-local.sh cascade      # one root cause, many victims
scripts/demo-local.sh sizing       # wrong-sized workloads, each with ways to fix it
scripts/demo-local.sh enterprise   # 600 projects, ~11,500 workloads, 390 nodes: how it scales
scripts/demo-local.sh stop
```

It builds the image, runs it in demo mode on http://127.0.0.1:8099 and opens the Triage
view. The cluster is simulated, but everything runs through the real detector, so what
you see is what the board would say about a real cluster in that state. The **Simulate**
menu in the top bar has the same five presets plus all 24 single scenarios, and the left
rail explains what each active scenario should show.

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

| Deploy fails: missing ConfigMap | Triage | rollout stuck ← ConfigMap missing, with the similarly named one that exists |
| Deploy fails: crash on start | Triage, Pods | exit 127 translated to "command not found", container's last message |
| Deploy fails: project quota | Triage | no new pods at all (nothing to see on the maps); the API server's quota message |
| Deploy fails: security policy (SCC) | Triage | rollout stuck at 0 new pods ← pod rejected by SCC |
| Deploy fails: volume not provisioned | Triage, Hosts | claim not bound (StorageClass doesn't exist) → pod unschedulable |
| Deploy fails: readiness probe | Triage, Pods | rollout failed (ProgressDeadlineExceeded) ← probe gets HTTP 404 |
| Liveness probe kills the app | Triage, Pods | restarts with exit 137 explained by liveness-probe events, not an app crash |
| Service points at the wrong port | Triage, Services | pods all green, every call fails; pods listen on 8080, Service targets 8081 |
| NetworkPolicy blocks callers | Triage, Services | three callers from other projects cut off, one in the same tier still works |
| Route misconfigured | Triage, Users | route to a Service that doesn't exist; route not admitted (HostAlreadyClaimed) |
| CronJob run failed | Triage | last run hit its backoff limit |
| Platform: operator degraded + node update | Triage | platform strip: operator degraded, pool updating, node cordoned |

Link straight to one: `/?view=triage&scenario=bad-rollout` or
`/?view=strata&scenario=memory-pressure&select=host:ocp-worker-2`.
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
| `GALAXY_EVENT_WINDOW_SECONDS` | `3600` | warning events older than this are ignored |
| `GALAXY_POD_LOGS` | `false` | let the drawer fetch the last 80 log lines of a container (also needs `deploy/k8s/optional-pod-logs.yaml`) |
| `GALAXY_SIZING_MIN_HISTORY_SECONDS` | `3600` | how long a workload must be watched before it is called over-provisioned |
| `GALAXY_GROUP_BY` | `prefix` | how projects roll up into business units on large estates: `prefix`, `label:<namespace label key>` or `none` |
| `GALAXY_GROUP_SEPARATOR` | `-` | separator for `prefix` |
| `GALAXY_DETAIL_LIMIT` / `GALAXY_PROJECT_LIMIT` / `GALAXY_NODE_BUDGET` | `150` / `60` / `320` | workloads drawn individually before rolling up to projects; projects before rolling up to units; most things drawn at once |
| `GALAXY_INTERNAL_CIDRS` | | extra ranges to count as *your network* (e.g. a company's public blocks); RFC 1918, CGNAT, link-local and ULA are always internal |

Agent: `GALAXY_COLLECTOR_URL`, `GALAXY_AGENT_TOKEN`, `GALAXY_AGENT_INTERVAL` (10s), `GALAXY_CA_FILE`.

## API (for the suite)

All paths are relative, so the service works behind a path prefix (mount it at
`/something/` **with** the trailing slash). OpenAPI at `/api/docs`.

| | |
|---|---|
| `GET /api/v1/meta` | version, mode, which data sources are live, last error |
| `GET /api/v1/namespaces` | per namespace: workloads, pods, warning/critical counts, `platform` flag |
| `GET /api/v1/topology?ns=a,b&platform=true&expand=grp:retail,ns:retail-web` | the graph for one screen: `nodes`, `edges`, `hotspots`, `paths`, `cascades`, `sources`, plus `view` (zoom level, what is expanded) and the findings summary. Large estates come back rolled up; `expand` opens units in place, `level=workloads|projects|units` forces a level, `focus=true` is problems only |
| `GET /api/v1/node?id=…` | one workload, host or call from the full graph with its neighbours, calls and findings; `at` is the id it is drawn as in the caller's view |
| `GET /api/v1/workloads` · `/api/v1/calls` | table rows, with `ns`, `q`, `sort`, `desc`, `offset`, `limit`, `problems` |
| `GET /api/v1/search?q=…` | workloads, pods, hosts and projects by name |
| `GET /api/v1/hotspots?ns=…` | hotspots, riskiest paths and failure cascades |
| `GET /api/v1/triage?ns=…&severity=warning&pattern=…` | the Triage view as data (with `summary` totals and `patterns`, the shared causes; `pattern=<key>` lists one pattern's members): `findings` (each with `severity`, `category`, `title`, `cause`, `fix`, `commands`, `evidence`, `root`, `causedBy`, `effectOf`, `blast`, `userFacing`), `projects` scorecards, `changes`, `platform`, `counts` |
| `GET /api/v1/logs?ns=&pod=&container=&previous=` | last log lines of one container; 403 unless `GALAXY_POD_LOGS=true` |
| `POST /api/v1/agent/report` | agent ingest (`Authorization: Bearer <token>`, gzip OK) |
| `GET /healthz`, `/readyz` | probes |

With `ns` set, neighbours outside the selection are included with
`"ghost": true` so cross-project dependencies stay visible.

### Embedding

```html
<iframe src="https://galaxy.example/?embed=1&ns=payments&view=strata"></iframe>
```

URL params: `ns`, `platform=1`, `focus=1` (problems only), `expand=grp:<unit>,ns:<project>`, `view=triage|galaxy|strata|cascade|gibson`, `select=<node id>`,
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
  (which can hold env values) never leave the process; rollout diffs name the env
  variables that changed, never their values.
- Sizing advice is text and commands to copy. Nothing is ever applied to the cluster.
- Diagnostics read Events, PersistentVolumeClaims, ResourceQuotas, LimitRanges, NetworkPolicies,
  ControllerRevisions and (OpenShift) cluster operators. Event and status messages
  are shown as the API serves them.
- **Pod logs are off by default.** Reading them needs an extra ClusterRole
  (`deploy/k8s/optional-pod-logs.yaml`) and `GALAXY_POD_LOGS=true`. Logs can hold
  data that configuration never shows, and the dashboard serves them to anyone who
  can open it, so put it behind a login first. Only pods the collector already
  shows can be read, 80 lines at a time.
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
  The one exception is peak CPU and memory per container, kept in memory for up to a day
  for the sizing advice and lost when the collector restarts.
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
