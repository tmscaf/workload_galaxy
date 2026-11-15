// Deterministic layouts for both views. Same input -> same positions, so
// live refreshes only move what actually changed.
import { hash01 } from './format.js';

export const GALAXY = { R1: 48, RS: 32, TWIST: 0.34, MIN_ARC: 15 };
export const STRATA = { users: 100, services: 36, pods: -24, hosts: -80 };

const TAU = Math.PI * 2;

export function podKey(wid, podName) { return `${wid}#${podName}`; }

// Hop distance from the outside world (or from roots when nothing is exposed).
export function computeDepth(nodes, edges) {
  const out = new Map(), inc = new Map();
  for (const n of nodes) { out.set(n.id, []); inc.set(n.id, 0); }
  for (const e of edges) {
    if (!out.has(e.source) || !out.has(e.target)) continue;
    out.get(e.source).push(e.target);
    inc.set(e.target, inc.get(e.target) + 1);
  }
  const depth = new Map();
  const queue = [];
  for (const n of nodes) if (n.kind === 'clients') { depth.set(n.id, 0); queue.push(n.id); }
  const bfs = () => {
    while (queue.length) {
      const id = queue.shift();
      for (const t of out.get(id)) if (!depth.has(t)) { depth.set(t, depth.get(id) + 1); queue.push(t); }
    }
  };
  bfs();
  // Background workloads (controllers, batch, unreferenced) start their own chains at ring 1.
  for (const n of nodes) {
    if (!depth.has(n.id) && n.kind !== 'host' && inc.get(n.id) === 0) { depth.set(n.id, 1); queue.push(n.id); }
  }
  bfs();
  for (const n of nodes) if (!depth.has(n.id) && n.kind !== 'host') { depth.set(n.id, 1); queue.push(n.id); bfs(); }
  return { depth, out };
}

function groupKey(n) {
  if (n.kind === 'external') return '~external';
  return n.namespace || '~cluster';
}

export function computeLayouts(graph) {
  const nodes = graph.nodes.filter((n) => n.kind !== 'host');
  const hosts = graph.nodes.filter((n) => n.kind === 'host');
  const { depth } = computeDepth(nodes, graph.edges);
  const preds = new Map(nodes.map((n) => [n.id, []]));
  for (const e of graph.edges) if (preds.has(e.target) && preds.has(e.source)) preds.get(e.target).push(e.source);

  let maxDepth = 1;
  for (const n of nodes) if (n.kind !== 'external') maxDepth = Math.max(maxDepth, depth.get(n.id) || 1);
  // Rolled-up units are big stars with their members in orbit: give them wider orbits.
  const G = graph.view?.collapsed ? { ...GALAXY, R1: 92, RS: 58, MIN_ARC: 34 } : GALAXY;
  const ringR = (d) => (d <= 0 ? 0 : G.R1 + (d - 1) * G.RS);

  // ---------------- galaxy: namespaces are spiral arms, depth is the orbit
  const groups = new Map();
  for (const n of nodes) {
    if (n.kind === 'clients' || n.kind === 'external') continue;
    const k = groupKey(n);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(n);
  }
  const gkeys = [...groups.keys()].sort((a, b) => {
    const pa = groups.get(a)[0].platform ? 1 : 0, pb = groups.get(b)[0].platform ? 1 : 0;
    return pa - pb || a.localeCompare(b);
  });
  const weights = gkeys.map((k) => Math.pow(groups.get(k).length, 0.85) + 0.8);
  const total = weights.reduce((a, b) => a + b, 0) || 1;
  const gap = gkeys.length > 1 ? 0.07 : 0;
  const usable = TAU - gap * gkeys.length;
  const galaxy = new Map();
  const angle = new Map();
  const arms = [];
  let a = -Math.PI / 2;
  gkeys.forEach((k, gi) => {
    const span = (usable * weights[gi]) / total;
    const a0 = a + gap / 2, a1 = a0 + span;
    a += span + gap;
    const members = groups.get(k);
    const byDepth = new Map();
    for (const n of members) {
      const d = depth.get(n.id) || 1;
      if (!byDepth.has(d)) byDepth.set(d, []);
      byDepth.get(d).push(n);
    }
    let dMax = 1;
    for (const d of [...byDepth.keys()].sort((x, y) => x - y)) {
      dMax = Math.max(dMax, d);
      const level = byDepth.get(d);
      // Order by where callers sit, so call lines cross less.
      const bary = (n) => {
        const ps = preds.get(n.id).filter((p) => angle.has(p));
        if (!ps.length) return (a0 + a1) / 2 + (hash01(n.id) - 0.5) * 0.01;
        return ps.reduce((s, p) => s + unwrap(angle.get(p), (a0 + a1) / 2), 0) / ps.length;
      };
      level.sort((x, y) => bary(x) - bary(y) || x.name.localeCompare(y.name));
      const r = ringR(d);
      const arc = (a1 - a0) * r;
      const rows = Math.max(1, Math.ceil((G.MIN_ARC * level.length) / Math.max(arc, 1)));
      level.forEach((n, i) => {
        const row = i % rows;
        const idx = Math.floor(i / rows);
        const perRow = Math.ceil(level.length / rows);
        const t = perRow === 1 ? 0.5 : (idx + 0.5) / perRow;
        const ang = a0 + (a1 - a0) * t + G.TWIST * (d - 1) + row * 0.04;
        const rr = r + row * G.RS * 0.42;
        angle.set(n.id, ang);
        galaxy.set(n.id, { x: Math.cos(ang) * rr, y: (hash01(n.id) - 0.5) * 7, z: Math.sin(ang) * rr });
      });
    }
    const ns = members[0];
    arms.push({ key: k, namespace: ns.namespace, platform: !!ns.platform, a0, a1, dMax, count: members.length });
  });

  // External dependencies sit past the outermost orbit, near whoever calls them.
  const externals = nodes.filter((n) => n.kind === 'external');
  const extR = ringR(maxDepth + 1) + 8;
  const extAngles = externals.map((n) => {
    const ps = preds.get(n.id).filter((p) => angle.has(p));
    const base = ps.length ? Math.atan2(ps.reduce((s, p) => s + Math.sin(angle.get(p)), 0), ps.reduce((s, p) => s + Math.cos(angle.get(p)), 0))
      : hash01(n.id) * TAU;
    return { n, ang: base };
  }).sort((x, y) => x.ang - y.ang);
  spreadAngles(extAngles, G.MIN_ARC / extR);
  for (const { n, ang } of extAngles) {
    angle.set(n.id, ang);
    // Internet dependencies orbit just beyond those on your own network.
    const r = n.scope === 'internet' ? extR + G.RS * 0.8 : extR;
    galaxy.set(n.id, { x: Math.cos(ang) * r, y: (hash01(n.id) - 0.5) * 10, z: Math.sin(ang) * r });
  }
  const lanUsers = nodes.some((n) => n.kind === 'clients' && n.scope === 'lan');
  for (const n of nodes) {
    if (n.kind === 'clients') galaxy.set(n.id, n.scope === 'lan' ? { x: 0, y: 6, z: 20 } : { x: 0, y: 6, z: lanUsers ? -8 : 0 });
  }

  const rings = [];
  for (let d = 1; d <= maxDepth; d++) rings.push(ringR(d));
  if (externals.some((n) => n.scope !== 'internet')) rings.push(extR);
  if (externals.some((n) => n.scope === 'internet')) rings.push(extR + G.RS * 0.8);

  // ---------------- strata: users / services / pods / hosts planes
  const strata = new Map();
  const ring = nodes.filter((n) => n.kind === 'workload' || n.kind === 'service');
  const order = new Map(gkeys.map((k, i) => [k, i]));
  ring.sort((x, y) => (order.get(groupKey(x)) ?? 99) - (order.get(groupKey(y)) ?? 99)
    || (depth.get(x.id) || 0) - (depth.get(y.id) || 0) || x.name.localeCompare(y.name));
  const R = Math.max(95, (ring.length * 27) / TAU);
  const ringAngle = new Map();
  ring.forEach((n, i) => {
    const ang = -Math.PI / 2 + (i / Math.max(1, ring.length)) * TAU;
    ringAngle.set(n.id, ang);
    strata.set(n.id, { x: Math.cos(ang) * R, y: STRATA.services, z: Math.sin(ang) * R * 0.92 });
  });
  const entries = nodes.filter((n) => n.kind === 'entry');
  const succ = new Map();
  for (const e of graph.edges) {
    if (!succ.has(e.source)) succ.set(e.source, []);
    succ.get(e.source).push(e.target);
  }
  const placeNear = (list, radius, neighbours, y = STRATA.services) => {
    const items = list.map((n) => {
      const ns = neighbours(n).filter((x) => ringAngle.has(x));
      const ang = ns.length ? Math.atan2(ns.reduce((s, x) => s + Math.sin(ringAngle.get(x)), 0), ns.reduce((s, x) => s + Math.cos(ringAngle.get(x)), 0))
        : hash01(n.id) * TAU;
      return { n, ang };
    }).sort((x, y) => x.ang - y.ang);
    spreadAngles(items, 16 / radius);
    for (const { n, ang } of items) {
      ringAngle.set(n.id, ang);
      strata.set(n.id, { x: Math.cos(ang) * radius, y, z: Math.sin(ang) * radius * 0.92 });
    }
  };
  placeNear(entries, R * 0.5, (n) => succ.get(n.id) || []);
  // Your network's dependencies ring the services plane; internet ones live on the top plane with
  // the end users: outside, not on the same plane as your workloads.
  const netExt = externals.filter((n) => (n.scope || 'internet') === 'internet');
  const netR = Math.max(52, (netExt.length * 24) / TAU);
  placeNear(externals.filter((n) => !netExt.includes(n)), R * 1.22, (n) => preds.get(n.id) || []);
  placeNear(netExt, netR, (n) => preds.get(n.id) || [], STRATA.users);
  for (const n of nodes) {
    if (n.kind === 'clients') strata.set(n.id, n.scope === 'lan' ? { x: -46, y: STRATA.users, z: 8 } : { x: 0, y: STRATA.users, z: 0 });
  }

  // Pods under their workload, sunflower-packed; hosts on their own plane.
  const podSlots = new Map();
  for (const n of ring) {
    if (n.kind !== 'workload') continue;
    const c = strata.get(n.id);
    const cnt = n.pods.length;
    const cr = 2.8 * Math.sqrt(cnt);
    n.pods.forEach((p, i) => {
      const rr = cnt === 1 ? 0 : cr * Math.sqrt((i + 0.5) / cnt);
      const ang = i * 2.39996;
      podSlots.set(podKey(n.id, p.name), { x: c.x * 0.8 + Math.cos(ang) * rr, y: STRATA.pods, z: c.z * 0.8 + Math.sin(ang) * rr });
    });
  }
  const real = hosts.filter((h) => !h.pseudo);
  const hostR = real.length <= 1 ? 0 : Math.max(30, (real.length * 24) / TAU);
  real.sort((x, y) => x.name.localeCompare(y.name)).forEach((h, i) => {
    const ang = -Math.PI / 2 + (i / Math.max(1, real.length)) * TAU;
    strata.set(h.id, { x: Math.cos(ang) * hostR, y: STRATA.hosts, z: Math.sin(ang) * hostR * 0.92 });
    galaxy.set(h.id, { x: Math.cos(ang) * hostR * 0.3, y: -60, z: Math.sin(ang) * hostR * 0.3 });
  });
  // The scheduler's queue sits at the edge of the hosts plane: pods with no node hang off it.
  let hostsR = Math.max(46, hostR + 30);
  for (const h of hosts.filter((x) => x.pseudo)) {
    const x = hostR + 46;
    strata.set(h.id, { x, y: STRATA.hosts, z: 14 });
    galaxy.set(h.id, { x: x * 0.3, y: -60, z: 0 });
    hostsR = Math.max(hostsR, x + 18);
  }

  const planes = {
    users: { y: STRATA.users, r: netExt.length ? netR + 18 : lanUsers ? 64 : 26, internet: netExt.length },
    services: { y: STRATA.services, r: (externals.length > netExt.length ? R * 1.32 : R * 1.16) },
    pods: { y: STRATA.pods, r: R * 0.98 },
    hosts: { y: STRATA.hosts, r: hostsR },
  };
  let galaxyExtent = 60;
  for (const [id, p] of galaxy) if (!id.startsWith('host:')) galaxyExtent = Math.max(galaxyExtent, Math.hypot(p.x, p.z));
  const { cascade, columns, cascadeExtent } = cascadeLayout(nodes, hosts, depth, preds, maxDepth);
  const city = cityLayout(nodes, hosts, graph.edges, depth, gkeys);
  return { galaxy, strata, cascade, columns, cascadeExtent, city, podSlots, rings, arms, depth, planes, extent: galaxyExtent + 30 };
}

// ---------------- city ("Gibson"): the cluster as a city on a circuit board
// Towers (workloads) stand on one global lattice, so the streets between lattice rows/columns are
// always free: calls are routed along them as right-angle traces and never cut through a building.
// Districts = namespaces. The internet gateway stands on the front edge (+z); beyond it, on a lower
// plane off the board, the internet (end users and internet services). Your network's connectors sit
// on the right rim, inside the gateway; hosts sit along the back.
export const CITY = { S: 30, TOWER: 12, FLOOR: 9, PLINTH: 9, NET_Y: -22 };   // plinth: a base so 1-pod services still stand up; NET_Y: the internet's plane, below the board

function cityLayout(nodes, hosts, edges, depth, gkeys) {
  const { S } = CITY;
  const districts = new Map();
  for (const n of nodes) {
    if (n.kind === 'clients' || n.kind === 'external') continue;
    const k = groupKey(n);
    if (!districts.has(k)) districts.set(k, []);
    districts.get(k).push(n);
  }
  // Each district is a little grid; shallow (closer to users) workloads in its front rows.
  const blocks = [...districts.entries()].map(([key, list]) => {
    list.sort((a, b) => (depth.get(a.id) || 0) - (depth.get(b.id) || 0) || (a.kind === 'entry' ? -1 : 0) - (b.kind === 'entry' ? -1 : 0)
      || a.name.localeCompare(b.name));
    const cols = Math.max(1, Math.ceil(Math.sqrt(list.length * 1.4)));
    const rows = Math.ceil(list.length / cols);
    return { key, list, cols, rows, platform: !!list[0].platform, namespace: list[0].namespace };
  });
  const order = new Map(gkeys.map((k, i) => [k, i]));
  blocks.sort((a, b) => (a.platform ? 1 : 0) - (b.platform ? 1 : 0) || (order.get(a.key) ?? 99) - (order.get(b.key) ?? 99));
  // Shelf-pack districts into rows of the lattice, one empty lattice line (a wide street) between them.
  const total = blocks.reduce((s, b) => s + b.cols * b.rows, 0);
  const maxW = Math.max(6, Math.ceil(Math.sqrt(total) * 1.9));
  let cx = 0, cy = 0, shelfH = 0, width = 0;
  for (const b of blocks) {
    if (cx > 0 && cx + b.cols > maxW) { cy += shelfH + 1; cx = 0; shelfH = 0; }
    b.c0 = cx; b.r0 = cy;
    cx += b.cols + 1; shelfH = Math.max(shelfH, b.rows); width = Math.max(width, cx - 1);
  }
  const height = cy + shelfH;
  const toX = (col) => (col - (width - 1) / 2) * S;
  const toZ = (row) => ((height - 1) / 2 - row) * S;       // row 0 = front (toward the gate)
  const pos = new Map();
  const districtsOut = [];
  for (const b of blocks) {
    b.list.forEach((n, i) => pos.set(n.id, { x: toX(b.c0 + (i % b.cols)), y: 0, z: toZ(b.r0 + Math.floor(i / b.cols)) }));
    districtsOut.push({ key: b.key, namespace: b.namespace, platform: b.platform,
      x0: toX(b.c0) - S * 0.5, x1: toX(b.c0 + b.cols - 1) + S * 0.5,
      z0: toZ(b.r0 + b.rows - 1) - S * 0.5, z1: toZ(b.r0) + S * 0.5, count: b.list.length });
  }
  const xMin = toX(0) - S, xMax = toX(width - 1) + S, zFront = toZ(0) + S, zBack = toZ(height - 1) - S;
  const H = S / 2, Y = 0.4, NET_Y = CITY.NET_Y;
  // The internet gateway: the one way between your network and the internet, standing on the board's
  // front edge. It sits on a street line, so traces reach it without cutting through a tower.
  const gate = { x: width % 2 ? H : 0, z: zFront + S };
  const preds = new Map();
  for (const e of edges) { if (!preds.has(e.target)) preds.set(e.target, []); preds.get(e.target).push(e.source); }
  const avg = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
  // Your network (LAN): inside the gateway, on a zone at the board's right rim, near the callers.
  const extGap = S * 0.75;
  const rims = [];
  // Users on your network sit here too, at the front, near what they use.
  const succs = new Map();
  for (const e of edges) { if (!succs.has(e.source)) succs.set(e.source, []); succs.get(e.source).push(e.target); }
  const lan = nodes.filter((n) => (n.kind === 'external' || n.kind === 'clients') && n.scope === 'lan').map((n) => {
    const nb = n.kind === 'clients' ? succs.get(n.id) : preds.get(n.id);
    const z = avg((nb || []).map((p) => pos.get(p)?.z).filter((v) => v != null));
    return { n, z: n.kind === 'clients' ? Math.max(z ?? 0, zFront) : z ?? 0 };
  }).sort((a, b) => b.z - a.z);
  lan.forEach((e, i) => { if (i && lan[i - 1].z - e.z < extGap) e.z = lan[i - 1].z - extGap; });
  const lanX = xMax + S * 0.9;
  for (const e of lan) pos.set(e.n.id, { x: lanX, y: 0, z: e.z });
  if (lan.length) rims.push({ scope: 'lan', x: lanX, w: S * 1.05, z0: Math.min(...lan.map((e) => e.z)) - S * 1.0,
    z1: Math.max(...lan.map((e) => e.z)) + S * 0.45 });
  // The internet: off the board, on its own lower plane beyond the gateway, in two zones by role.
  // "Coming in": the end users, straight out from the gateway. "Called from the cluster": the
  // internet services your workloads connect out to, beside them. Everything between the cluster
  // and the internet, in either direction, passes through the gateway.
  const netIds = new Set();
  const users = nodes.filter((n) => n.kind === 'clients' && n.scope !== 'lan');
  const inet = nodes.filter((n) => n.kind === 'external' && (n.scope || 'internet') === 'internet').map((n) => {
    const x = avg((preds.get(n.id) || []).map((p) => pos.get(p)?.x).filter((v) => v != null));
    return { n, x: x ?? gate.x };
  }).sort((a, b) => a.x - b.x || a.n.name.localeCompare(b.n.name));
  let internet = null;
  if (users.length || inet.length) {
    const zNet = gate.z + S * 1.7, G = S * 1.25;
    const zones = [];
    const box = (role, pts) => {
      const xs = pts.map((p) => p[0]), zs = pts.map((p) => p[1]);
      zones.push({ role, x0: Math.min(...xs) - S * 0.65, x1: Math.max(...xs) + S * 0.65,
        z0: Math.min(...zs) - S * 0.7, z1: Math.max(...zs) + S * 0.95 });   // room in front for the pad labels
    };
    if (users.length) {
      users.forEach((u) => { pos.set(u.id, { x: gate.x, y: NET_Y, z: zNet }); netIds.add(u.id); });
      box('in', [[gate.x, zNet]]);
    }
    if (inet.length) {
      // A grid to the left of the users (your network's zone is on the right); left-most callers
      // get the left-most pads so the fan out of the gateway crosses itself less.
      const cols = Math.min(4, inet.length), rows = Math.ceil(inet.length / cols);
      const right = users.length ? gate.x - S * 1.9 : gate.x + ((cols - 1) * G) / 2;
      const pts = [];
      inet.forEach((e, i) => {
        const x = right - (cols - 1 - (i % cols)) * G, z = zNet + (Math.floor(i / cols) - (rows - 1) / 2) * S;
        pos.set(e.n.id, { x, y: NET_Y, z });
        netIds.add(e.n.id);
        pts.push([x, z]);
      });
      box('out', pts);
    }
    internet = { y: NET_Y, zones, x0: Math.min(...zones.map((z) => z.x0)) - S * 0.3, x1: Math.max(...zones.map((z) => z.x1)) + S * 0.3,
      z0: Math.min(...zones.map((z) => z.z0)) - S * 0.3, z1: Math.max(...zones.map((z) => z.z1)) + S * 0.75 };
  }
  // Hosts, and cluster-level dependencies (API server, node services): a row of blocks behind the city.
  const infra = [...hosts.slice().sort((a, b) => (a.pseudo ? 1 : 0) - (b.pseudo ? 1 : 0) || a.name.localeCompare(b.name)),
    ...nodes.filter((n) => n.kind === 'external' && n.scope === 'cluster')];
  infra.forEach((h, i, all) => {
    pos.set(h.id, { x: (i - (all.length - 1) / 2) * S * 1.6, y: 0, z: zBack - S * 1.2 });
  });
  // Right-angle traces along the streets: out of the source onto the street beside its row, along to
  // the street beside the target, then into the target. Traffic to or from the internet runs to the
  // gateway along its street and drops through it to the internet plane. Small per-call lane offsets
  // keep parallel traces apart.
  const routes = new Map();
  for (const e of edges) {
    const a = pos.get(e.source), b = pos.get(e.target);
    if (!a || !b) continue;
    const l1 = (hash01(e.id) - 0.5) * S * 0.36, l2 = (hash01(e.id + '~') - 0.5) * S * 0.36;
    const gx = gate.x + l2 * 0.5, out = gate.z + S * 0.45;
    const fromNet = netIds.has(e.source), toNet = netIds.has(e.target);
    let pts;
    if (fromNet && !toNet) {          // in from the internet, through the gateway, to the target's row
      const zs = b.z + H + l1 * 0.5;
      pts = [[a.x, a.y + Y, a.z], [gx, Y, out], [gx, Y, gate.z], [gx, Y, zs], [b.x, Y, zs], [b.x, Y, b.z]];
    } else if (toNet && !fromNet) {   // out to the internet: to the gateway's street, out through it
      const zs = a.z + H + l1 * 0.5;
      pts = [[a.x, Y, a.z], [a.x, Y, zs], [gx, Y, zs], [gx, Y, gate.z], [gx, Y, out], [b.x, b.y + Y, b.z]];
    } else {
      const zStreet = a.z - H + l1 * 0.5;          // street just behind the source row (toward the back)
      const xStreet = b.x + (b.x >= a.x ? -H : H) + l2 * 0.5;
      pts = [[a.x, Y, a.z], [a.x, Y, zStreet], [xStreet, Y, zStreet], [xStreet, Y, b.z], [b.x, Y, b.z]];
    }
    const clean = pts.filter((p, i) => i === 0 || Math.abs(p[0] - pts[i - 1][0]) + Math.abs(p[1] - pts[i - 1][1])
      + Math.abs(p[2] - pts[i - 1][2]) > 0.01);
    routes.set(e.id, clean.map(([x, y, z]) => ({ x, y, z })));
  }
  const bounds = { x0: xMin - S * 0.4, x1: xMax + S * (lan.length ? 1.6 : 0.6), z0: zBack - S * 2, z1: gate.z };
  const frame = internet ? { x0: Math.min(bounds.x0, internet.x0), x1: Math.max(bounds.x1, internet.x1),
    z0: bounds.z0, z1: internet.z1 } : bounds;
  return { pos, routes, districts: districtsOut, bounds, frame, rims, internet, gate };
}

// ---------------- cascade: call depth as a left-to-right waterfall of columns
// Users | Entry/Hop 1 | Hop 2 | ... | External | Infrastructure. Each column steps down and back.
export const CASCADE = { DX: 82, DY: 26, STEP_Y: 9, STEP_Z: 14 };

function cascadeLayout(nodes, hosts, depth, preds, maxDepth) {
  const cols = new Map();
  const put = (t, n) => { if (!cols.has(t)) cols.set(t, []); cols.get(t).push(n); };
  for (const n of nodes) {
    if (n.kind === 'clients') put(0, n);
    else if (n.kind === 'external') put(n.scope === 'cluster' ? maxDepth + 3 : n.scope === 'lan' ? maxDepth + 1 : maxDepth + 2, n);
    else put(Math.max(1, depth.get(n.id) || 1), n);
  }
  const hostCol = maxDepth + 3;
  for (const h of hosts) put(hostCol, h);
  const ts = [...cols.keys()].sort((a, b) => a - b);
  const T = ts.length - 1;
  const cascade = new Map();
  const columns = [];
  let maxH = 0;
  // Columns past the last hop (your network, the internet, infrastructure) hold long hostnames:
  // give them wider gutters so their labels don't run into the neighbouring column's.
  const xs = [];
  ts.forEach((t, ci) => { xs.push(ci === 0 ? 0 : xs[ci - 1] + (t > maxDepth ? CASCADE.DX * 1.7 : CASCADE.DX)); });
  const span = xs[xs.length - 1] || 0;
  ts.forEach((t, ci) => {
    const list = cols.get(t);
    // Order each column by where its callers sit, so the waterfall's lines cross less.
    const bary = (n) => {
      const ps = (preds.get(n.id) || []).filter((p) => cascade.has(p));
      if (!ps.length) return 0;
      return ps.reduce((s, p) => s + cascade.get(p).y, 0) / ps.length;
    };
    if (t === hostCol) list.sort((a, b) => (a.pseudo ? 1 : 0) - (b.pseudo ? 1 : 0) || a.name.localeCompare(b.name));
    else list.sort((a, b) => bary(b) - bary(a) || (a.namespace || '').localeCompare(b.namespace || '') || a.name.localeCompare(b.name));
    const x = xs[ci] - span / 2;
    const base = -(ci - T / 2) * CASCADE.STEP_Y;
    const z = -(ci - T / 2) * CASCADE.STEP_Z;
    const dy = t === hostCol ? CASCADE.DY * 1.25 : CASCADE.DY;
    list.forEach((n, i) => cascade.set(n.id, { x, y: base + ((list.length - 1) / 2 - i) * dy, z }));
    const h = Math.max(1, list.length - 1) * dy;
    maxH = Math.max(maxH, h);
    const allExt = list.every((n) => n.kind === 'external');
    const kind = t === 0 ? 'users' : t === hostCol ? 'hosts' : allExt && t === maxDepth + 1 ? 'lan' : allExt && t === maxDepth + 2 ? 'internet' : 'hop';
    columns.push({ t, kind, x, z, y: base, top: base + h / 2, bottom: base - h / 2, count: list.length,
      label: kind === 'users' ? 'End users' : kind === 'hosts' ? 'Infrastructure' : kind === 'lan' ? 'Your network (LAN)' : kind === 'internet' ? 'Internet' : `Hop ${t}`,
      ids: list.map((n) => n.id) });
  });
  return { cascade, columns, cascadeExtent: { w: span, h: maxH + T * CASCADE.STEP_Y } };
}

function unwrap(ang, ref) {
  while (ang - ref > Math.PI) ang -= TAU;
  while (ref - ang > Math.PI) ang += TAU;
  return ang;
}

// Push sorted angles apart so neighbours keep a minimum spacing.
function spreadAngles(items, minGap) {
  if (items.length < 2) return;
  minGap = Math.min(minGap, TAU / items.length);
  for (let iter = 0; iter < 60; iter++) {
    let moved = false;
    for (let i = 0; i < items.length; i++) {
      const a = items[i], b = items[(i + 1) % items.length];
      let diff = b.ang - a.ang;
      if (i === items.length - 1) diff += TAU;
      if (diff < minGap) {
        const push = (minGap - diff) / 2;
        a.ang -= push; b.ang += push; moved = true;
      }
    }
    if (!moved) break;
  }
}
