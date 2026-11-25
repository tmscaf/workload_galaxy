// Deterministic layouts for both views (galaxy, strata). Same input -> same positions, so
// live refreshes only move what actually changed.
//
// The planes of the strata view, top to bottom (the keys are the engine's, the meaning is the network's):
//   users     the internet: the services devices talk to, and the internet reaching in
//   services  the devices on the network
//   pods      the address each online device holds right now
//   hosts     the router and anything else devices hang off (a hypervisor running VMs)
import { hash01, visiblePods } from './format.js';

export const GALAXY = { R1: 48, RS: 32, TWIST: 0.34, MIN_ARC: 15 };
export const STRATA = { users: 100, services: 36, pods: -24, hosts: -80 };

const TAU = Math.PI * 2;

export function podKey(wid, podName) { return `${wid}#${podName}`; }

// Hop distance from the internet reaching in (or from roots when nothing is forwarded).
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
  // Devices nothing connects to start their own chains at ring 1.
  for (const n of nodes) {
    if (!depth.has(n.id) && n.kind !== 'host' && inc.get(n.id) === 0) { depth.set(n.id, 1); queue.push(n.id); }
  }
  bfs();
  for (const n of nodes) if (!depth.has(n.id) && n.kind !== 'host') { depth.set(n.id, 1); queue.push(n.id); bfs(); }
  return { depth, out };
}

// What a node is grouped with (a spiral arm): its device group. When groups are rolled up, a group
// and whatever of it is drawn in detail sit together.
let groupByUnit = false;
function groupKey(n) {
  if (n.kind === 'external') return '~external';
  return (groupByUnit && n.group) || n.namespace || '~network';
}

export function computeLayouts(graph) {
  groupByUnit = !!graph.view?.collapsed;
  const nodes = graph.nodes.filter((n) => n.kind !== 'host');
  const hosts = graph.nodes.filter((n) => n.kind === 'host');
  const { depth } = computeDepth(nodes, graph.edges);
  const preds = new Map(nodes.map((n) => [n.id, []]));
  for (const e of graph.edges) if (preds.has(e.target) && preds.has(e.source)) preds.get(e.target).push(e.source);

  let maxDepth = 1;
  for (const n of nodes) if (n.kind !== 'external') maxDepth = Math.max(maxDepth, depth.get(n.id) || 1);
  // Rolled-up units are big stars with their members in orbit: give them wider orbits.
  const G = graph.view?.collapsed ? { ...GALAXY, R1: 92, RS: 58, MIN_ARC: 34 } : GALAXY;
  // ...and deep chains inside an opened group are packed closer, so they don't fling the map wide.
  const ringR = (d) => (d <= 0 ? 0 : G.R1 + G.RS * (graph.view?.collapsed ? Math.min(d - 1, 2) + 0.45 * Math.max(0, d - 3) : d - 1));

  // ---------------- galaxy: device groups are spiral arms, depth is the orbit
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
      // Order by where the other end sits, so connection lines cross less.
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
    arms.push({ key: k, namespace: groupByUnit ? k : ns.namespace, platform: !!ns.platform, a0, a1, dMax, count: members.length });
  });

  // Internet services sit past the outermost orbit, near whoever talks to them.
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
    // Internet services orbit just beyond anything on your own network.
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

  // ---------------- strata: internet / devices / addresses / router & hosts planes
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
  // Internet services live on the top plane with the internet reaching in: outside, not on the
  // same plane as your devices.
  const netExt = externals.filter((n) => (n.scope || 'internet') === 'internet');
  const netR = Math.max(52, (netExt.length * 24) / TAU);
  placeNear(externals.filter((n) => !netExt.includes(n)), R * 1.22, (n) => preds.get(n.id) || []);
  placeNear(netExt, netR, (n) => preds.get(n.id) || [], STRATA.users);
  for (const n of nodes) {
    if (n.kind === 'clients') strata.set(n.id, n.scope === 'lan' ? { x: -46, y: STRATA.users, z: 8 } : { x: 0, y: STRATA.users, z: 0 });
  }

  // Each device's address under it (sunflower-packed when a rolled-up group shows its members);
  // the router and other hosts on their own plane.
  const podSlots = new Map();
  for (const n of ring) {
    if (n.kind !== 'workload') continue;
    const c = strata.get(n.id);
    const shown = visiblePods(n);
    const cnt = shown.length;
    const cr = 2.8 * Math.sqrt(cnt);
    shown.forEach((p, i) => {
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
  return { galaxy, strata, podSlots, rings, arms, depth, planes, extent: galaxyExtent + 30 };
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
