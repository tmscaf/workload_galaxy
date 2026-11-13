# Changelog

The version lives in [`VERSION`](VERSION). Every deploy adds a UTC build stamp
(`0.3.0-20261111.2318`), shown in the UI's version chip, `/api/v1/version`, the
image tag and the `app.kubernetes.io/version` label in Rancher.

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
