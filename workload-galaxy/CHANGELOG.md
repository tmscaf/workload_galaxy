# Changelog

The version lives in [`VERSION`](VERSION). Every deploy adds a UTC build stamp
(`0.3.0-20261111.2318`), shown in the UI's version chip, `/api/v1/version`, the
image tag and the `app.kubernetes.io/version` label in Rancher.

## 0.10.0
- **Problems only** (Topology, bottom bar): draws what is failing, worst first, each with what
  calls it and what it depends on; everything healthy is rolled up into its project or unit.
  Works at any cluster size; `focus=1` in the URL, `focus=true` on `/api/v1/topology`.
- On a rolled-up estate the map groups by business unit (arms, districts), so a unit and the
  parts of it drawn in detail sit together.
- A workload with hundreds of pods no longer becomes a giant star or a tower through the roof:
  at most 36 pods / 18 floors are drawn (the worst ones); the drawer lists the worst 60.
- Cascade wraps a busy hop into lanes of 12 instead of running off the screen.
- Deep call chains inside an opened project are packed closer in Galaxy.

## 0.9.0
- **Scales to enterprise estates** (verified on a simulated 606 projects / 11,451 workloads /
  27,341 pods / 394 nodes) while small clusters look exactly as before.
  - Topology zooms by meaning: above 150 workloads the unit is the project, above 60 projects
    the business unit (namespace prefix, or a label). Units are drawn in every view, coloured by
    how much of them is failing; double-click opens one in place; at most 320 things are drawn.
  - Calls between units merge into bundles; healthy nodes group by zone.
  - Triage: the same problem on many workloads is one **shared cause**; projects show as an
    estate grid by business unit; problems page; Workloads and Calls are server-paged.
  - Search, the drawer and "Show on map" work against the whole estate through the server.
  - The page downloads what it draws (60-90 KB) instead of the whole graph (20 MB); responses
    are gzip-compressed.
- API: `/api/v1/node`, `/api/v1/workloads`, `/api/v1/calls`, `/api/v1/search`; `topology` gains
  `expand` and `level`; `triage` gains `summary`, `patterns` and `pattern=`.
- Demo: `GALAXY_DEMO_SCALE=150x20|enterprise|enterprise-xl` and `scripts/demo-local.sh enterprise`.
- Camera reframes when a unit is opened or closed, and no longer zooms inside large maps.

## 0.8.0
- **Navigation**: Triage and Topology are separate sections in the top bar. Galaxy, Strata, Cascade
  and Gibson are the views of Topology and only show there.
- **Labels never overlap** in any topology view. Names are placed first by priority (selection,
  then trouble, then landmarks): above the node, else below it, else the label steps aside until
  you zoom or hover. Metrics lines appear only where there is room. Labels also keep clear of plane,
  column, district and arm captions, and no label is clipped by the side rail in Cascade.
- Topology shows rollout state on the node label ("rollout stuck", "rolling out"); tooltips show the
  diagnosis and its next step; Gibson tower faces lead with the diagnosis and rollout.
- **Showcase**: `scripts/demo-local.sh` runs a simulated cluster in Docker with curated failure
  presets (also in the Simulate menu): a bad release day, wiring and network faults,
  infrastructure trouble, one root cause with many victims. Scenario guides in the rail are
  collapsible and say what Triage shows.
- The collector now rolls out with `maxUnavailable: 0`: the running version keeps serving until
  the new one has built its first snapshot, so a build that can't start never replaces one that
  works (0.7.0 took the dashboard down for ten minutes because of `Recreate`).

## 0.7.1
- Fix: a warning event on a pod in a hidden namespace (such as this tool's own) crashed every
  refresh, so 0.7.0 never became ready on a real cluster. Events for hidden workloads are ignored.

## 0.7.0
- **Triage view**: a troubleshooting dashboard. Verdict and platform strip, per-project
  scorecards, problems grouped by root cause (each with *Why*, *Next*, evidence and copy-ready
  `oc`/`kubectl` commands), sortable Workloads and Calls tables, and a "What changed" timeline.
  The Triage tab shows the number of open problems in every view.
- **Diagnostics engine** (`diagnose.py`): reads Events, rollout history, per-container state and
  exit codes, PersistentVolumeClaims, ResourceQuotas, NetworkPolicies, Ingress/Route status,
  Jobs, HPA conditions, cert-manager Certificates and OpenShift cluster operators, and explains:
  stuck/failed rollouts (with what changed), image pull failures, crash loops by exit code,
  missing ConfigMaps/Secrets, SCC / quota / webhook / LimitRange rejections, unschedulable pods,
  unbound volumes, failing probes, Services that select nothing or target a port the pods don't
  listen on, routes to missing Services, NetworkPolicies that block a call, failed jobs,
  degraded platform operators. Symptoms are linked to their root cause.
- Findings feed risk scoring, so the maps turn amber/red for problems that pod states alone
  don't show (quota, storage, wiring, rejected routes).
- Detail drawer: Diagnosis, latest rollout, warning events, storage claims, per-container state
  with last exit code; optional log tail (`GALAXY_POD_LOGS`, off by default, extra RBAC).
- API: `GET /api/v1/triage`, `GET /api/v1/logs`. Project summaries gain healthy/degraded/down,
  pods ready, restarts, quota and top problem.
- RBAC: the collector's ClusterRole now also reads events, persistentvolumeclaims,
  resourcequotas, networkpolicies, controllerrevisions, cert-manager certificates and OpenShift
  clusteroperators / clusterversions / machineconfigpools (all read-only; still no Secrets).
- Init-container failures now count as the pod's state (`Init:CrashLoopBackOff`, as kubectl shows).
- 12 new demo scenarios for deployment failures; the remembered view is restored on load.

## 0.6.1
- Gibson: the gold lines holding the internet plane now run from the gateway's pillars to the
  plane's back corners. They used to rise from the plane's corners to the board's edge, and when
  the plane was wider than the board one of them ended in empty space.

## 0.6.0
- **End users are split by where they come from.** When the node agent can see a connection's
  real source address, private ones become *Users on your network* (green, inside the gateway, on
  the your-network zone in Gibson); public ones stay *End users* on the internet. Where the source
  is hidden (an external load balancer, SNAT) everything stays with End users. The drawer shows
  where each group comes from and the source addresses recently seen.
- Gibson: the internet plane has two labelled zones: **Internet · coming in** (end users) and
  **Internet · called from the cluster** (the internet services your workloads connect out to).
- Strata: the top plane is *Users & internet* when internet services sit there with the users.
- Failure cascades count either group of users as "reaches users".

## 0.5.1
- Gibson overview framing measures what's actually on screen (board, internet plane, tower tops),
  so the internet plane no longer slides under the side rail. The *Internet* label sits centred
  on the plane's front edge.

## 0.5.0
- Gibson: an **internet gateway** arch on the board's front edge. The internet is its own plane
  below and beyond the board, holding the end users and every internet service, so nothing on the
  internet looks like it's on your network. All traffic between the cluster and the internet, in
  either direction, runs through the arch (outbound calls such as a game server reaching its
  players included). Your network's connectors stay on the board, inside the gateway.
- Strata: internet services move up to the top plane with the end users (shown as *Internet*);
  your network's dependencies stay on the services plane.

## 0.4.2
- External dependencies are classified by where they live: **your network** (private ranges,
  internal names, or `GALAXY_INTERNAL_CIDRS`), **the internet**, or the cluster's own
  infrastructure. Each has its own colour (green / gold / pale blue) in every view, on the node,
  its label tag and the healthy calls into it; slow or failing calls keep amber/red.
- Gibson: your-network connectors sit on a green zone at the rim, internet connectors on a gold
  zone one row farther out. Cluster infrastructure joins the hosts along the back.
- Cascade: separate *Your network (LAN)* and *Internet* columns, with wider gutters so long
  hostnames don't run into the neighbouring column. Galaxy: the internet orbits farther out.
- The detail drawer for an external dependency says where it lives and why.

## 0.4.1
- Tower faces show only real data: risk and what it's inherited from, image, services, hosts,
  per-pod node/IP/QoS/CPU/memory/age/issues, callers, calls (latency, evidence, the config key
  they were found in) and blast radius. The decorative hex filler is gone. Short content stays
  still and top-aligned; only long content scrolls.
- Clicking a tower flies to a face-on close-up. Its faces re-render at 4x resolution, bloom and
  scrolling ease off so the text stays sharp, towers in front fade out of the way, and clearing
  the selection returns to the overview.
- Asset URLs now include a fingerprint of the app files, so a browser can never keep stale
  code, even when files are edited under the same build stamp (this is what broke the local
  demo). An unknown view falls back to Galaxy instead of collapsing the scene.

## 0.4.0
- **Gibson view**: the cluster as a city on a circuit board, in a fixed isometric view.
  - Towers = workloads, standing on a plinth. Floors = pods: amber/red when failing, dashed
    above the roof when unscheduled, dark rubble at the base when evicted.
  - Districts = projects. Calls run as right-angle traces along the streets, with pulses in
    request direction (failure cascades send red pulses back along them).
  - End users enter through a gate, external dependencies plug into edge connectors on the rim,
    and hosts sit as chips along the back.
  - Each tower's faces scroll its live telemetry (pods, calls, boxed issue cards).
  - Clicking a tower or floor brings it front and center; a floor selects that pod.
- Bloom glow in the Gibson view (vendored Three.js post-processing, works air-gapped).

## 0.3.2
- The page now loads its code from build-versioned URLs (`/v/<version>-<build>/…`). A browser can
  no longer keep running an older build's JavaScript after a deploy. Builds before 0.3.0 served
  scripts without cache headers, and Chrome's normal reload does not recheck them, which is how
  the 0.3.1 label fix could appear not to work.
- The version chip also reports the code this tab is running, and turns amber ("page out of
  date") if that differs from the server.
- Safety sweep: after every refresh, label elements that no longer belong to a scene object are
  removed.

## 0.3.1
- Fixed floating "End users / Hop N / External N" labels that piled up in every view. Each refresh
  rebuilt the Cascade column panes, but their labels were never removed from the page.
- Fixed the always-visible empty "Reload" box. The update banner now only shows when a new build
  is detected.
- Rebuilt scene objects now free their GPU memory, so a tab left open no longer grows.
- `/api/v1/version` lists the simulated agents in demo mode.

## 0.3.0
- **Versioning.** Collector, agent and image now share a single source of truth.
  The UI shows the running version, warns when an agent doesn't match the
  collector, and offers a reload when a new build is deployed under an open page.
  Added `GET /api/v1/version`.
- App files are revalidated on every page load, so a redeploy is never hidden
  behind a cached script.

## 0.2.3
- Conntrack flows that ended no longer linger as ghost edges and external nodes.
  Idle time is derived from each entry's remaining TTL; flows quiet for longer
  than `GALAXY_FLOW_IDLE_SECONDS` (60) are dropped.
- Unreplied UDP is no longer treated as a failed connection.
- Connections that are open but idle are drawn dimmed, with "last packet N ago".

## 0.2.2
- Single-node clusters no longer blame their node for being "slower than other
  nodes".
- Cascade "main cause" counts respect the namespace filter.

## 0.2.1
- Network latency is scored on the kernel's minimum RTT (the path). Smoothed RTT
  is inflated by delayed ACKs on quiet connections. Internet distance is no
  longer a fault.

## 0.2.0 (first deploy to the home-lab cluster)
- Cascade view: call-depth waterfall with failure cascades and blast radius.
- Node-level diagnosis: memory/CPU requests vs limits vs usage, overcommit,
  system-reserved, taints, an Unscheduled pseudo-host, per-pod issues, and
  node-local network health. A sick node becomes a root cause.
- Fault scenarios for the demo cluster (10, combinable).
- Image transfer that survives VPN drops.

## 0.1.0
- Collector (topology, config inference, risk, API), node agent, Galaxy and
  Strata views, demo mode, k8s / home-lab / OpenShift manifests.
