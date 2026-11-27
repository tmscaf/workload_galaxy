// Copyright (C) 2026 Troy Scafuro
// SPDX-License-Identifier: AGPL-3.0-only

// Three.js rendering: one scene, two arrangements (galaxy / strata) blended by view weights.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import * as TX from './textures.js';
import { esc, fmtBits, hash01, isOffline, nsColor, nodeMetric, plural, podLevel, visiblePods } from './format.js';
import { computeLayouts, podKey } from './layout.js';

const C = {
  cyan: new THREE.Color('#4fd1ff'), violet: new THREE.Color('#a07bff'), warn: new THREE.Color('#ffb547'),
  crit: new THREE.Color('#ff4d6d'), inferred: new THREE.Color('#8391b8'), white: new THREE.Color('#ffffff'),
  entry: new THREE.Color('#5ff1d9'), external: new THREE.Color('#93a9ff'), clients: new THREE.Color('#cfe9ff'),
  // Where the far end of a connection lives: your own network or the internet.
  lan: new THREE.Color('#5ee6a8'), internet: new THREE.Color('#ffd75e'), clusterExt: new THREE.Color('#9fdcff'),
  podOk: new THREE.Color('#8f7dff'), hostOk: new THREE.Color('#c6fff0'), idle: new THREE.Color('#58627e'),
  up: new THREE.Color('#34e3b0'), down: new THREE.Color('#b18cff'),
};
// The strata planes, top to bottom. Keys are the engine's; labels are what they mean on a network.
export const LAYERS = [
  { key: 'users', label: 'The internet', color: '#ffd75e' },
  { key: 'services', label: 'Devices', color: '#4f7dff' },
  { key: 'pods', label: 'Addresses in use', color: '#8b5cf6' },
  { key: 'hosts', label: 'Router & hosts', color: '#19c9a0' },
];
const HOST_W = 18, HOST_H = 7, HOST_D = 11;
const SEG = 28;            // line segments per edge curve
const TAIL = 4;            // points per comet (head + tail)
const DROP_SEG = 10;

const VIEWS = ['galaxy', 'strata'];
const scopeColor = (scope) => (scope === 'lan' ? C.lan : scope === 'cluster' ? C.clusterExt : C.internet);
const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

const PARTICLE_VS = `
attribute float size; attribute vec4 pcolor; varying vec4 vColor; uniform float uScale;
void main(){ vColor = pcolor; vec4 mv = modelViewMatrix * vec4(position, 1.0);
  gl_PointSize = size * uScale / max(1.0, -mv.z); gl_Position = projectionMatrix * mv; }`;
const PARTICLE_FS = `
varying vec4 vColor;
void main(){ vec2 c = gl_PointCoord - 0.5; float d = length(c); if (d > 0.5) discard;
  float a = pow(1.0 - d * 2.0, 1.7); gl_FragColor = vec4(vColor.rgb * (0.7 + 0.8 * a), vColor.a * a); }`;
const HEX_VS = `
attribute float size; attribute vec4 pcolor; attribute float ring; varying vec4 vColor; varying float vRing; uniform float uScale;
void main(){ vColor = pcolor; vRing = ring; vec4 mv = modelViewMatrix * vec4(position, 1.0);
  gl_PointSize = size * uScale / max(1.0, -mv.z); gl_Position = projectionMatrix * mv; }`;
const HEX_FS = `
varying vec4 vColor; varying float vRing;
float hexd(vec2 p){ p = abs(p); return max(p.x * 0.8660254 + p.y * 0.5, p.y); }
void main(){
  vec2 p = (gl_PointCoord - 0.5) * 2.0;
  float inner = vRing > 0.5 ? 0.5 : 0.82;
  float h = hexd(p) / inner;
  float fill = 1.0 - smoothstep(0.9, 1.0, h);
  float rim = smoothstep(0.62, 0.92, h) * fill;
  float ringA = vRing * (smoothstep(0.80, 0.86, length(p)) - smoothstep(0.92, 0.98, length(p)));
  float a = max(fill * (0.6 + 0.4 * rim), ringA);
  if (a < 0.02) discard;
  vec3 col = vColor.rgb * (0.65 + 0.7 * rim) + ringA * vColor.rgb * 0.4;
  gl_FragColor = vec4(col, a * vColor.a);
}`;

export class GalaxyScene {
  constructor(container, labelHost, cb = {}) {
    this.cb = cb;
    this.container = container;
    this.opts = { labels: true, rotate: true };
    this.view = 'galaxy';
    this.wv = { galaxy: 1, strata: 0 };   // animated view weights (sum to 1)
    this.highlight = null;       // { nodes:Set, edges:Set, pods:Set, hosts:Set, up:Set, down:Set }
    this.selectedId = null;
    this.nodes = new Map();
    this.edges = [];
    this.pods = [];
    this.clock = new THREE.Clock();
    this.time = 0;
    this.lastInteraction = -100;

    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setClearColor(0x03050c, 1);
    container.appendChild(this.renderer.domElement);
    this.labelHost = labelHost;
    this.labelRenderer = new CSS2DRenderer({ element: labelHost });

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(45, 1, 1, 6000);
    this.camera.position.set(0, 260, 330);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.autoRotateSpeed = 0.25;
    this.controls.maxDistance = 2200;
    this.controls.minDistance = 20;
    this.controls.addEventListener('start', () => { this.lastInteraction = this.time; this.fly = null; });

    this.root = new THREE.Group();
    this.scene.add(this.root);
    this._uniformScale = { value: 800 };
    this._buildBackdrop();
    this._buildStatic();
    this._bindPointer();
    this.resize();
    window.addEventListener('resize', () => this.resize());
    this.renderer.setAnimationLoop(() => this._frame());
  }

  // ------------------------------------------------------------------ setup
  _pointsMaterial(blending = THREE.AdditiveBlending, vs = PARTICLE_VS, fs = PARTICLE_FS) {
    return new THREE.ShaderMaterial({
      uniforms: { uScale: this._uniformScale }, vertexShader: vs, fragmentShader: fs,
      transparent: true, depthWrite: false, blending,
    });
  }

  _buildBackdrop() {
    // Distant starfield.
    const n = 2600;
    const pos = new Float32Array(n * 3), col = new Float32Array(n * 4), size = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const u = Math.random() * 2 - 1, th = Math.random() * Math.PI * 2, r = 1500 + Math.random() * 1500;
      const s = Math.sqrt(1 - u * u);
      pos.set([r * s * Math.cos(th), r * u, r * s * Math.sin(th)], i * 3);
      const tint = Math.random();
      col.set([0.75 + 0.25 * tint, 0.8 + 0.2 * tint, 1, 0.35 + Math.random() * 0.5], i * 4);
      size[i] = 4 + Math.random() * 7;
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setAttribute('pcolor', new THREE.BufferAttribute(col, 4));
    g.setAttribute('size', new THREE.BufferAttribute(size, 1));
    this.starfield = new THREE.Points(g, this._pointsMaterial());
    this.scene.add(this.starfield);

    // Galactic core glow + bulge haze (galaxy view only).
    const core = new THREE.SpriteMaterial({ map: TX.nebula(), color: 0x9fc4ff, transparent: true, depthWrite: false,
      blending: THREE.AdditiveBlending, opacity: 0.55 });
    this.core = new THREE.Sprite(core);
    this.core.scale.set(170, 170, 1);
    this.root.add(this.core);
  }

  _buildStatic() {
    // Edge curves.
    this.edgeGeo = new THREE.BufferGeometry();
    this.edgeLines = new THREE.LineSegments(this.edgeGeo, new THREE.LineBasicMaterial({
      vertexColors: true, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }));
    this.edgeLines.frustumCulled = false;
    this.root.add(this.edgeLines);
    // Strata drop lines (device -> its address -> the router or host it hangs off).
    this.dropGeo = new THREE.BufferGeometry();
    this.dropLines = new THREE.LineSegments(this.dropGeo, new THREE.LineBasicMaterial({
      vertexColors: true, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }));
    this.dropLines.frustumCulled = false;
    this.root.add(this.dropLines);
    // Comets.
    this.particleGeo = new THREE.BufferGeometry();
    this.particles = new THREE.Points(this.particleGeo, this._pointsMaterial());
    this.particles.frustumCulled = false;
    this.root.add(this.particles);
    // Addresses (and the members of a rolled-up group).
    this.podGeo = new THREE.BufferGeometry();
    this.podPoints = new THREE.Points(this.podGeo, this._pointsMaterial(THREE.NormalBlending, HEX_VS, HEX_FS));
    this.podPoints.frustumCulled = false;
    this.root.add(this.podPoints);
    // Galaxy decoration (orbit rings + arm dust), rebuilt per layout.
    this.decor = new THREE.Group();
    this.root.add(this.decor);
    // Strata planes.
    this.planes = new THREE.Group();
    this.root.add(this.planes);
    this.planeObjs = {};
    for (const L of LAYERS) {
      const color = new THREE.Color(L.color);
      const disc = new THREE.Mesh(new THREE.CircleGeometry(1, 96), new THREE.MeshBasicMaterial({
        map: TX.planeFill(), color, transparent: true, opacity: 0.0, depthWrite: false, side: THREE.DoubleSide,
        blending: THREE.AdditiveBlending }));
      disc.rotation.x = -Math.PI / 2;
      const pts = [];
      for (let i = 0; i <= 128; i++) { const a = (i / 128) * Math.PI * 2; pts.push(new THREE.Vector3(Math.cos(a), 0, Math.sin(a))); }
      const rim = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({
        color, transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending }));
      const div = document.createElement('div');
      div.className = 'lbl layer plane';
      div.style.setProperty('--c', L.color);
      const label = new CSS2DObject(div);
      label.center.set(1, 0);          // hangs from its anchor, text running away from the plane
      const grp = new THREE.Group();
      grp.add(disc, rim, label);
      this.planes.add(grp);
      this.planeObjs[L.key] = { grp, disc, rim, label, div };
    }
    // Selection ring.
    this.selRing = new THREE.Sprite(new THREE.SpriteMaterial({ map: TX.ring(), color: 0xe6f4ff, transparent: true,
      depthWrite: false, blending: THREE.AdditiveBlending, opacity: 0 }));
    this.root.add(this.selRing);
  }

  // Panels covering the canvas: shift the projection so the scene centres in the free area.
  // The shift eases toward its target in the frame loop, so opening the drawer glides.
  setInsets(left, right, instant = false) {
    this.insetPx = { left, right };
    this.insetTarget = (left - right) / 2;
    if (instant || this.insetShift == null) { this.insetShift = this.insetTarget; this.resize(); }
  }

  _applyShift() {
    const w = this.container.clientWidth || window.innerWidth, h = this.container.clientHeight || window.innerHeight;
    const shift = Math.round(this.insetShift || 0);
    if (shift) this.camera.setViewOffset(w, h, -shift, 0, w, h); else this.camera.clearViewOffset();
    this.camera.updateProjectionMatrix();
  }

  resize() {
    const w = this.container.clientWidth || window.innerWidth, h = this.container.clientHeight || window.innerHeight;
    this.renderer.setSize(w, h);
    this.labelRenderer.setSize(w, h);
    this.camera.aspect = w / h;
    this._applyShift();
    this._updateScale();
  }

  _updateScale() {
    const h = this.container.clientHeight || window.innerHeight;
    this._uniformScale.value = (h * this.renderer.getPixelRatio()) / (2 * Math.tan((this.camera.fov * Math.PI) / 360));
  }

  // ------------------------------------------------------------------ data
  setData(graph) {
    this.graph = graph;
    this.groupTitles = new Map((graph.namespaces || []).map((g) => [g.name, g.title || g.name]));
    const L = computeLayouts(graph);
    this.layout = L;
    const byId = new Map(graph.nodes.map((n) => [n.id, n]));
    const first = this.nodes.size === 0;

    // Nodes: update in place, create new, remove gone.
    for (const [id, obj] of this.nodes) {
      if (!byId.has(id)) { disposeTree(obj.group); this.root.remove(obj.group); this.nodes.delete(id); }
    }
    for (const n of graph.nodes) {
      let obj = this.nodes.get(n.id);
      const g = L.galaxy.get(n.id) || { x: 0, y: 0, z: 0 };
      const s = L.strata.get(n.id) || g;
      if (!obj) {
        obj = this._makeNode(n);
        obj.gal.set(g.x, g.y, g.z); obj.str.set(s.x, s.y, s.z);
        if (!first) { obj.gal.multiplyScalar(0.85); obj.str.multiplyScalar(0.85); }
        this.nodes.set(n.id, obj);
      }
      obj.data = n;
      obj.galTarget.set(g.x, g.y, g.z);
      obj.strTarget.set(s.x, s.y, s.z);
      this._styleNode(obj);
    }

    // Addresses: one per online device. A rolled-up group shows its member devices the same way.
    const oldPods = new Map(this.pods.map((p) => [p.key, p]));
    this.pods = [];
    for (const n of graph.nodes) {
      if (n.kind !== 'workload') continue;
      const shownPods = visiblePods(n);
      shownPods.forEach((p, i) => {
        const key = podKey(n.id, p.name);
        const slot = L.podSlots.get(key) || L.strata.get(n.id);
        const prev = oldPods.get(key);
        const st = prev ? prev.str : new THREE.Vector3(slot.x, slot.y, slot.z);
        this.pods.push({
          key, wid: n.id, data: p, i, count: shownPods.length, str: st,
          strTarget: new THREE.Vector3(slot.x, slot.y, slot.z), level: podLevel(p),
          incl: (hash01(key) - 0.5) * 1.2, phase: hash01(key + 'p') * Math.PI * 2,
          speed: 0.25 + hash01(key + 's') * 0.25, pos: new THREE.Vector3(),
        });
      });
    }
    this._allocPods();

    // Edges.
    const prevPhase = new Map(this.edges.map((e) => [e.data.id, e.phases]));
    this.edges = graph.edges.filter((e) => this.nodes.has(e.source) && this.nodes.has(e.target)).map((e) => {
      // A port forward that is set up but has carried no traffic: drawn dashed, with a single slow pulse.
      const inferredOnly = !(e.evidence || []).includes('observed');
      const idle = !!e.traffic?.idle;   // connection open but quiet
      const lvl = e.risk?.level || 'ok';
      const level = idle ? 0 : e.traffic?.level || 0;   // 0..1, traffic volume
      const count = inferredOnly || idle ? 1 : Math.round(2 + 9 * level);
      const phases = prevPhase.get(e.id) && prevPhase.get(e.id).length === count ? prevPhase.get(e.id)
        : Array.from({ length: count }, (_, k) => (k + hash01(e.id)) / count);
      return {
        data: e, inferredOnly, idle, lvl, level, count, phases,
        extScope: byId.get(e.target)?.kind === 'external' ? (byId.get(e.target).scope || 'internet') : null,
        inScope: byId.get(e.source)?.kind === 'clients' && byId.get(e.source).scope === 'lan' ? 'lan' : null,
        p0: new THREE.Vector3(), p1: new THREE.Vector3(), p2: new THREE.Vector3(), len: 1,
      };
    });
    this._allocEdges();
    this._buildDecor();
    this._layoutPlanes();
    this._sweepLabels();
    this._applyHighlight();
    this.positionsDirty = true;
    // Opening or closing a group changes the size of the map: frame the new one.
    const zoomKey = `${graph.view?.level || ''}|${graph.view?.focus ? 'focus' : ''}|${(graph.view?.expanded || []).join(',')}`;
    if (first) this.resetCamera(true);
    else if (zoomKey !== this._zoomKey) this.resetCamera(false);
    this._zoomKey = zoomKey;
  }

  // Safety net: any label element in the page that no longer belongs to a scene object is removed,
  // whatever path orphaned it. (CSS2D only appends live labels, so this never removes a real one.)
  _sweepLabels() {
    const live = new Set();
    this.scene.traverse((o) => { if (o.isCSS2DObject) live.add(o.element); });
    for (const el of [...this.labelHost.children]) if (!live.has(el)) el.remove();
  }

  _makeNode(n) {
    const group = new THREE.Group();
    const mk = (map, blending = THREE.AdditiveBlending) => new THREE.Sprite(new THREE.SpriteMaterial({
      map, transparent: true, depthWrite: false, blending }));
    const obj = { group, gal: new THREE.Vector3(), str: new THREE.Vector3(),
      galTarget: new THREE.Vector3(), strTarget: new THREE.Vector3(),
      pos: new THREE.Vector3(), dim: 1 };
    if (n.kind === 'host') {
      const box = new THREE.BoxGeometry(HOST_W, HOST_H, HOST_D);
      const wireMat = n.pseudo
        ? new THREE.LineDashedMaterial({ color: C.crit, dashSize: 1.2, gapSize: 0.9, transparent: true, depthWrite: false })
        : new THREE.LineBasicMaterial({ color: C.hostOk, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending });
      obj.wire = new THREE.LineSegments(new THREE.EdgesGeometry(box), wireMat);
      if (n.pseudo) obj.wire.computeLineDistances();
      obj.pick = new THREE.Mesh(box, new THREE.MeshBasicMaterial({ color: C.hostOk, transparent: true, opacity: 0.08,
        depthWrite: false, blending: THREE.AdditiveBlending }));
      obj.glow = mk(TX.glow());
      obj.glow.scale.set(26, 26, 1);
      group.add(obj.wire, obj.pick, obj.glow);
    } else {
      const map = { clients: TX.cloud(), entry: TX.diamond(), external: TX.hexOutline(), service: TX.ring() }[n.kind];
      obj.glow = mk(TX.glow());
      obj.core = mk(map || TX.star());
      obj.halo = mk(TX.dashedRing());
      obj.halo.visible = false;
      group.add(obj.glow, obj.core, obj.halo);
      obj.pick = obj.glow;
    }
    obj.pick.userData.nodeId = n.id;
    const div = document.createElement('div');
    div.className = 'lbl';
    obj.label = new CSS2DObject(div);
    group.add(obj.label);
    this.root.add(group);
    return obj;
  }

  _styleNode(obj) {
    const n = obj.data;
    const lvl = n.risk?.level || 'ok';
    const lvlColor = lvl === 'critical' ? C.crit : lvl === 'warning' ? C.warn : null;
    const offline = isOffline(n);
    const blocked = n.kind === 'external' && !!(n.ext?.allBlocked || (n.ext?.blocked || []).length);
    let color, size;
    if (n.kind === 'workload') {
      const base = new THREE.Color(nsColor(n.namespace));
      color = lvlColor ? lvlColor.clone() : base.lerp(C.white, 0.35);
      if (offline) color = C.idle.clone();
      // A device grows with the traffic it carries (log scale: a sensor vs a TV streaming).
      const d = n.device || {};
      const bps = Math.max((d.upBps || 0) + (d.downBps || 0), (d.upAvgBps || 0) + (d.downAvgBps || 0));
      size = offline ? 9 : Math.min(22, 10 + 2.3 * Math.log10(1 + bps / 2000));
      // A whole group rolled up: bigger with what it holds.
      if (n.aggregate) size = 12 + 2.6 * Math.log2(1 + (n.counts?.workloads || 0));
    } else if (n.kind === 'clients') {
      color = n.scope === 'lan' ? C.lan.clone().lerp(C.white, 0.45) : C.clients.clone();
      size = n.scope === 'lan' ? 20 : 30;
    }
    else if (n.kind === 'entry') { color = C.entry.clone(); size = 12; }
    else if (n.kind === 'external') {
      color = lvlColor ? lvlColor.clone() : n.ext?.blacklisted ? C.crit.clone() : scopeColor(n.scope).clone();
      // Blocked by you: greyed with a hint of red, dimmed (its connections should die away).
      if (blocked) color = C.idle.clone().lerp(C.crit, 0.35);
      size = Math.min(17, 10 + 1.6 * Math.log10(1 + ((n.ext?.bytesPerSec || 0) * 8) / 2000));
    }
    else if (n.kind === 'service') { color = C.inferred.clone(); size = 11; }
    else { color = lvlColor ? lvlColor.clone() : C.hostOk.clone(); size = 14; }
    if (n.ghost) color.lerp(new THREE.Color('#566180'), 0.55);
    obj.color = color;
    obj.size = size;
    obj.offline = offline || blocked;
    if (n.kind === 'host') {
      obj.wire.material.color.copy(color);
      obj.pick.material.color.copy(color);
      obj.glow.material.color.copy(color);
    } else {
      obj.glow.material.color.copy(color);
      obj.core.material.color.copy(n.kind === 'workload' && !lvlColor && !offline ? C.white.clone().lerp(color, 0.35) : color);
      const coreScale = n.kind === 'workload' ? size * 0.55 : n.kind === 'clients' ? size : size * 1.25;
      obj.core.scale.set(coreScale, coreScale, 1);
      obj.glow.scale.set(size * 2.1, size * 2.1, 1);
      obj.halo.visible = false;
    }
    obj.pulse = lvl === 'critical' && !offline && !blocked;
    const k = n.counts;
    let metric;
    if (n.aggregate && n.kind === 'workload') {
      const dev = k?.workloads || 0;
      metric = [plural(dev, 'device'), k?.failing ? `${k.failing} need${k.failing === 1 ? 's' : ''} attention` : 'nothing wrong'].join(' · ');
    } else if (n.kind === 'host') {
      const inf = n.infra || {};
      const dev = n.subkind === 'Router' && inf.devices != null ? inf.devices : n.podCount;
      metric = [dev != null ? plural(dev, 'device') : '', inf.throughputBps ? fmtBits(inf.throughputBps) : ''].filter(Boolean).join(' · ');
    } else if (n.kind === 'clients') {
      const fw = (this.graph?.edges || []).filter((e) => e.source === n.id && e.type === 'exposes').length;
      metric = fw ? plural(fw, 'port forward') : '';
    } else metric = nodeMetric(n);
    if (blocked) metric = [n.ext.allBlocked ? 'blocked' : 'partly blocked', metric].filter(Boolean).join(' · ');
    const lanUsers = n.kind === 'clients' && n.scope === 'lan';
    obj.label.element.className = `lbl ${offline ? 'offline' : lvl}${blocked ? ' blocked' : ''}${n.ghost ? ' ghost' : ''}${n.kind === 'external' ? ` ext-${n.scope || 'internet'}` : lanUsers ? ' ext-lan' : ''}`;
    const nsBit = n.ghost && n.namespace ? `<span class="m">${esc(this.groupTitle(n.namespace))} / </span>` : '';
    const name = n.aggregate && n.kind === 'workload' ? this.groupTitle(n.project || n.namespace || n.name) : n.name;
    obj.label.element.innerHTML = `<div class="n">${nsBit}${esc(name)}</div>${metric ? `<div class="m">${esc(metric)}</div>` : ''}`;
    obj.labelY = n.kind === 'host' ? 7 : n.kind === 'external' ? size * 0.95 : size * 0.45;
    obj.label.position.set(0, obj.labelY, 0);
  }

  // The display name of a device group ("Phones & tablets" for phones-tablets).
  groupTitle(id) {
    return this.groupTitles?.get(id) || id || '';
  }

  _allocPods() {
    const n = this.pods.length;
    this.podPos = new Float32Array(n * 3);
    this.podCol = new Float32Array(n * 4);
    this.podSize = new Float32Array(n);
    this.podRing = new Float32Array(n);
    this.podGeo.setAttribute('position', new THREE.BufferAttribute(this.podPos, 3));
    this.podGeo.setAttribute('pcolor', new THREE.BufferAttribute(this.podCol, 4));
    this.podGeo.setAttribute('size', new THREE.BufferAttribute(this.podSize, 1));
    this.podGeo.setAttribute('ring', new THREE.BufferAttribute(this.podRing, 1));
    this.podGeo.setDrawRange(0, n);
    this.pods.forEach((p, i) => {
      const c = p.level === 'critical' ? C.crit : p.level === 'warning' ? C.warn : p.level === 'evicted' ? C.idle : C.podOk;
      this.podCol.set([c.r, c.g, c.b, 1], i * 4);
      this.podRing[i] = p.level === 'warning' || p.level === 'critical' ? 1 : 0;
      this.podSize[i] = this.podRing[i] ? 6.5 : 3.6;
    });
    this.podGeo.attributes.ring.needsUpdate = true;
    this.podGeo.attributes.size.needsUpdate = true;
  }

  _allocEdges() {
    const ne = this.edges.length;
    this.edgePos = new Float32Array(ne * SEG * 2 * 3);
    this.edgeCol = new Float32Array(ne * SEG * 2 * 4);
    this.edgeGeo.setAttribute('position', new THREE.BufferAttribute(this.edgePos, 3));
    this.edgeGeo.setAttribute('color', new THREE.BufferAttribute(this.edgeCol, 4));
    let np = 0;
    for (const e of this.edges) np += e.count;
    this.pCap = np * TAIL;
    this.pPos = new Float32Array(this.pCap * 3);
    this.pCol = new Float32Array(this.pCap * 4);
    this.pSize = new Float32Array(this.pCap);
    this.particleGeo.setAttribute('position', new THREE.BufferAttribute(this.pPos, 3));
    this.particleGeo.setAttribute('pcolor', new THREE.BufferAttribute(this.pCol, 4));
    this.particleGeo.setAttribute('size', new THREE.BufferAttribute(this.pSize, 1));
    // Drop lines: device->address and address->host, DROP_SEG segments each.
    const nd = this.pods.length * 2;
    this.dropPos = new Float32Array(nd * DROP_SEG * 2 * 3);
    this.dropCol = new Float32Array(nd * DROP_SEG * 2 * 4);
    this.dropGeo.setAttribute('position', new THREE.BufferAttribute(this.dropPos, 3));
    this.dropGeo.setAttribute('color', new THREE.BufferAttribute(this.dropCol, 4));
  }

  _buildDecor() {
    clearGroup(this.decor);
    const L = this.layout;
    // Dotted orbit rings.
    const ringPts = [], ringCol = [], ringSize = [];
    for (const r of L.rings) {
      const n = Math.max(90, Math.round(r * 1.4));
      for (let i = 0; i < n; i++) {
        const a = (i / n) * Math.PI * 2;
        ringPts.push(Math.cos(a) * r, 0, Math.sin(a) * r);
        ringCol.push(0.55, 0.65, 0.95, 0.5);
        ringSize.push(1.5);
      }
    }
    // Spiral-arm dust, tinted per device group.
    const { R1, RS, TWIST } = { R1: 48, RS: 32, TWIST: 0.34 };
    for (const arm of L.arms) {
      const c = new THREE.Color(nsColor(arm.namespace));
      const n = 120 + arm.count * 40;
      const span = arm.a1 - arm.a0;
      for (let i = 0; i < n; i++) {
        const d = 0.6 + Math.random() * (arm.dMax + 0.2);
        const g = gauss() * 0.32;
        const ang = (arm.a0 + arm.a1) / 2 + g * span + TWIST * (d - 1);
        const r = R1 + (d - 1) * RS + gauss() * RS * 0.28;
        ringPts.push(Math.cos(ang) * r, gauss() * 3.5, Math.sin(ang) * r);
        ringCol.push(c.r, c.g, c.b, 0.12 + Math.random() * 0.3);
        ringSize.push(1.5 + Math.random() * 3.5);
      }
      const labelAng = (arm.a0 + arm.a1) / 2 + TWIST * arm.dMax;
      const lr = R1 + arm.dMax * RS + 4;
      const div = document.createElement('div');
      div.className = 'lbl ns';
      div.style.setProperty('--c', nsColor(arm.namespace));
      div.innerHTML = `<div class="n">${esc(this.groupTitle(arm.namespace) || 'network')}</div>`;
      const lbl = new CSS2DObject(div);
      lbl.position.set(Math.cos(labelAng) * lr, 0, Math.sin(labelAng) * lr);
      lbl.userData.nsLabel = true;
      this.decor.add(lbl);
    }
    // Central bulge haze.
    for (let i = 0; i < 500; i++) {
      const r = Math.abs(gauss()) * 30, a = Math.random() * Math.PI * 2;
      ringPts.push(Math.cos(a) * r, gauss() * 4, Math.sin(a) * r);
      ringCol.push(0.8, 0.88, 1, 0.15 + Math.random() * 0.25);
      ringSize.push(1.5 + Math.random() * 2.5);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(ringPts, 3));
    g.setAttribute('pcolor', new THREE.Float32BufferAttribute(ringCol, 4));
    g.setAttribute('size', new THREE.Float32BufferAttribute(ringSize, 1));
    this.decorPoints = new THREE.Points(g, this._pointsMaterial());
    this.decorAlpha = new Float32Array(ringCol.filter((_, i) => i % 4 === 3));
    this.decor.add(this.decorPoints);
  }

  // A plane's caption stays at the plane's left edge as seen on screen, whatever the camera angle.
  // Pinned to a fixed point of the rim it would sweep past the node labels as the view drifts,
  // knocking their metrics out and letting them back in on every pass.
  _anchorPlaneLabels() {
    const e = this.camera.matrixWorld.elements;      // column 0 is the camera's right vector
    let x = -e[0], z = -e[2];
    const len = Math.hypot(x, z);
    if (len < 1e-4) return;
    x /= len; z /= len;
    for (const L of LAYERS) {
      const p = this.layout.planes[L.key];
      this.planeObjs[L.key].label.position.set(x * (p.r + 8), 0, z * (p.r * 0.92 + 8));
    }
  }

  _layoutPlanes() {
    const P = this.layout.planes;
    const counts = this.layerCounts();
    for (const L of LAYERS) {
      const o = this.planeObjs[L.key];
      const p = P[L.key];
      o.grp.position.set(0, p.y, 0);
      o.disc.scale.set(p.r, p.r * 0.92, 1);
      o.rim.scale.set(p.r, 1, p.r * 0.92);
      o.label.position.set(-p.r - 8, 0, 0);
      const c = counts[L.key];
      const bits = [c.text];
      if (c.critical) bits.push(`${c.critical} critical`);
      if (c.warning) bits.push(`${c.warning} warning`);
      o.div.innerHTML = `<div class="n">${esc(L.label)}</div><div class="m">${esc(bits.join(' · '))}</div>`;
    }
  }

  // What each plane holds, in words, with how many of them have a warning or critical finding.
  // An offline device is counted as offline, never as a problem.
  layerCounts() {
    const mk = () => ({ shown: 0, warning: 0, critical: 0, text: '' });
    const out = { users: mk(), services: mk(), pods: mk(), hosts: mk() };
    const bump = (k, lvl) => {
      out[k].shown++;
      if (lvl === 'warning' || lvl === 'critical') out[k][lvl]++;
    };
    let services = 0, inbound = 0, offline = 0, groups = 0;
    for (const n of this.graph?.nodes || []) {
      if (n.kind === 'clients') { inbound++; bump('users', n.risk?.level); }
      else if (n.kind === 'external') { services++; bump('users', n.risk?.level || (n.ext?.blacklisted ? 'critical' : 'ok')); }
      else if (n.kind === 'host') bump('hosts', n.risk?.level);
      else {
        const off = isOffline(n);
        if (off) offline++;
        if (n.aggregate) groups++;
        bump('services', off ? 'ok' : n.risk?.level);
      }
    }
    for (const p of this.pods) if (!p.data.pseudo) bump('pods', 'ok');
    out.users.text = [plural(services, 'service'), inbound ? 'inbound' : ''].filter(Boolean).join(' + ');
    const dev = out.services.shown - groups;
    out.services.text = [dev || !groups ? plural(dev, 'device') : '', groups ? plural(groups, 'group') + ' rolled up' : '', offline ? `${offline} offline` : ''].filter(Boolean).join(' · ');
    out.pods.text = plural(out.pods.shown, 'address', 'addresses');
    const router = (this.graph?.nodes || []).some((n) => n.kind === 'host' && n.subkind === 'Router');
    const others = out.hosts.shown - (router ? 1 : 0);
    out.hosts.text = [router ? 'router' : '', others ? plural(others, 'host') : ''].filter(Boolean).join(' + ') || 'none';
    return out;
  }

  // ------------------------------------------------------------------ view / options / selection
  setView(view) {
    if (!VIEWS.includes(view)) view = 'galaxy';   // an unknown view must never zero every layout weight
    this.view = view;
    this.resetCamera(false);
  }

  setOptions(o) {
    Object.assign(this.opts, o);
    this.positionsDirty = true;
    this._applyHighlight();
  }

  resetCamera(instant) {
    const ext = this.layout ? this.layout.extent : 200;
    const sr = this.layout ? this.layout.planes.services.r : 120;
    let pos;
    const target = new THREE.Vector3(0, 0, 0);
    if (this.view === 'strata') pos = new THREE.Vector3(0, sr * 0.72 + 40, sr * 2.5 + 90);
    else pos = new THREE.Vector3(0, ext * 1.45, ext * 1.75);
    this._flyTo(pos, target, instant ? 0 : 1.2);
  }

  focus(id) {
    const obj = this.nodes.get(id);
    if (!obj) return;
    const dir = this.camera.position.clone().sub(this.controls.target).normalize();
    const cur = this.camera.position.distanceTo(this.controls.target);
    if (this.view === 'strata') {
      // Keep the whole stack in frame (internet -> router); just lean toward the node.
      const target = new THREE.Vector3(obj.strTarget.x * 0.35, 0, obj.strTarget.z * 0.35);
      const sr = this.layout ? this.layout.planes.services.r : 120;
      this._flyTo(target.clone().add(dir.multiplyScalar(Math.max(sr * 2.6 + 60, cur))), target, 0.9);
      return;
    }
    // The router and other hosts are only drawn in strata: in the galaxy, show the whole map (their devices light up).
    if (obj.data.kind === 'host') { this.resetCamera(false); return; }
    // Where the node is heading, not where it is drawn this frame: right after a load it has not been placed yet.
    const target = obj.galTarget.clone();
    // Close enough to read the node and its neighbours, never so close that a large map becomes a blur.
    const ext = this.layout ? this.layout.extent : 200;
    const dist = Math.max(90, Math.min(Math.max(220, ext * 0.8), cur * 0.6));
    this._flyTo(target.clone().add(dir.multiplyScalar(dist)), target, 0.9);
  }

  // Frame a connection: both ends in view.
  focusEdge(id) {
    const i = id.indexOf('->');
    const A = this.nodes.get(id.slice(0, i)), B = this.nodes.get(id.slice(i + 2));
    if (!A || !B) return;
    if (this.view === 'strata') { this.focus(A.data.kind === 'workload' ? A.data.id : B.data.id); return; }
    const target = A.galTarget.clone().add(B.galTarget).multiplyScalar(0.5);
    const dir = this.camera.position.clone().sub(this.controls.target).normalize();
    const ext = this.layout ? this.layout.extent : 200;
    const dist = Math.max(150, Math.min(ext * 2.2, A.galTarget.distanceTo(B.galTarget) * 1.9));
    this._flyTo(target.clone().add(dir.multiplyScalar(dist)), target, 0.9);
  }

  _flyTo(pos, target, dur) {
    if (!dur) {
      this.camera.position.copy(pos); this.controls.target.copy(target); this.controls.update();
      this.fly = null; return;
    }
    this.fly = { p0: this.camera.position.clone(), p1: pos, t0: this.controls.target.clone(), t1: target, t: 0, dur };
  }

  // sel: null | {type:'node', id} | {type:'edge', id} | {type:'path', nodes, edges}
  setSelection(sel) {
    this.selection = sel;
    this._applyHighlight();
  }

  _applyHighlight() {
    const sel = this.selection;
    if (!sel || !this.graph) { this.highlight = null; return; }
    const nodes = new Set(), edges = new Set(), up = new Set(), down = new Set(), hosts = new Set(), podKeys = new Set();
    const out = new Map(), inc = new Map();
    for (const e of this.edges) {
      const d = e.data;
      if (!out.has(d.source)) out.set(d.source, []);
      if (!inc.has(d.target)) inc.set(d.target, []);
      out.get(d.source).push(d);
      inc.get(d.target).push(d);
    }
    const walk = (start, map, key, mark) => {
      const stack = [start];
      const seen = new Set([start]);
      while (stack.length) {
        const id = stack.pop();
        for (const e of map.get(id) || []) {
          edges.add(e.id); mark.add(e.id);
          const nxt = e[key];
          if (!seen.has(nxt)) { seen.add(nxt); nodes.add(nxt); stack.push(nxt); }
        }
      }
    };
    let focusIds = [];
    if (sel.type === 'node') focusIds = [sel.id];
    else if (sel.type === 'pod') focusIds = [sel.key.split('#')[0]];
    else if (sel.type === 'edge') {
      const e = this.graph.edges.find((x) => x.id === sel.id);
      if (e) { nodes.add(e.source); nodes.add(e.target); edges.add(e.id); }
    } else if (sel.type === 'path') {
      sel.nodes.forEach((x) => nodes.add(x)); sel.edges.forEach((x) => edges.add(x));
    }
    const selNode = focusIds.length ? this.nodes.get(focusIds[0])?.data : null;
    const hostOf = (p) => p.data.hostId || `host:${p.data.node}`;
    if (selNode && selNode.kind === 'host') {
      // The router or a host: every device hanging off it, and who talks to those devices.
      nodes.add(selNode.id);
      for (const p of this.pods) {
        if (hostOf(p) === selNode.id && p.level !== 'evicted') {
          podKeys.add(p.key);
          if (!nodes.has(p.wid)) { nodes.add(p.wid); walk(p.wid, inc, 'source', up); }
        }
      }
      hosts.add(selNode.id);
    } else {
      for (const id of focusIds) {
        nodes.add(id);
        walk(id, inc, 'source', up);
        walk(id, out, 'target', down);
      }
    }
    // Light the whole stack under highlighted devices: their address and what they hang off.
    if (!(selNode && selNode.kind === 'host')) {
      for (const p of this.pods) {
        if (sel.type === 'pod' ? p.key === sel.key : nodes.has(p.wid)) { podKeys.add(p.key); hosts.add(hostOf(p)); }
      }
    }
    if (sel.type === 'pod') { for (const p of this.pods) if (p.wid === focusIds[0]) podKeys.add(p.key); }
    for (const h of hosts) nodes.add(h);
    this.highlight = { nodes, edges, up, down, podKeys, hosts };
  }

  // ------------------------------------------------------------------ interaction
  _bindPointer() {
    const el = this.renderer.domElement;
    this.raycaster = new THREE.Raycaster();
    this.raycaster.params.Points.threshold = 2.5;
    this.mouse = new THREE.Vector2(-9, -9);
    let down = null;
    el.addEventListener('pointermove', (ev) => {
      this.mouse.set((ev.clientX / el.clientWidth) * 2 - 1, -(ev.clientY / el.clientHeight) * 2 + 1);
      this.mouseClient = { x: ev.clientX, y: ev.clientY };
      this.hoverDirty = true;
    });
    el.addEventListener('pointerleave', () => { this.mouse.set(-9, -9); this.hoverDirty = true; });
    el.addEventListener('pointerdown', (ev) => { down = { x: ev.clientX, y: ev.clientY }; this.lastInteraction = this.time; });
    el.addEventListener('pointerup', (ev) => {
      if (!down || Math.hypot(ev.clientX - down.x, ev.clientY - down.y) > 5) return;
      const hit = this._pick();
      if (hit) this.cb.onSelect?.(hit); else this.cb.onBackground?.();
    });
    // Double-click: open a rolled-up group in place; on empty space, reset the camera.
    el.addEventListener('dblclick', () => { const hit = this._pick(); if (hit) this.cb.onOpen?.(hit); else this.resetCamera(false); });
  }

  _pick() {
    this.raycaster.setFromCamera(this.mouse, this.camera);
    const targets = [];
    const strataVis = this.wv.strata > 0.5;   // hosts and addresses are drawn there
    for (const obj of this.nodes.values()) {
      if (obj.data.kind === 'host' && !strataVis) continue;
      targets.push(obj.pick);
    }
    targets.push(this.podPoints);
    const hits = this.raycaster.intersectObjects(targets, false);
    for (const h of hits) {
      if (h.object === this.podPoints) {
        const p = this.pods[h.index];
        if (p && (p.data.pseudo || strataVis)) return { type: 'pod', key: p.key, pod: p.data, wid: p.wid };
      } else if (h.object.userData.nodeId) {
        return { type: 'node', id: h.object.userData.nodeId };
      }
    }
    return this._pickEdge();
  }

  // The connection under the pointer when no node is: the nearest drawn line within a few pixels.
  _pickEdge() {
    if (!this.mouseClient || !this.edges.length || this.mouse.x < -2) return null;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const mx = this.mouseClient.x - rect.left, my = this.mouseClient.y - rect.top;
    const hi = this.highlight;
    const v = this._pickV || (this._pickV = new THREE.Vector3());
    let best = null, bestD = 6 * 6;
    for (const e of this.edges) {
      if (hi && !hi.edges.has(e.data.id)) continue;      // faded out: not a target
      if (e.data.minor && !hi) continue;
      let px = null, py = null;
      for (let i = 0; i <= 14; i++) {
        bezier(e.p0, e.p1, e.p2, 0.07 + (i / 14) * 0.86, v).project(this.camera);
        if (v.z > 1 || v.z < -1) { px = null; continue; }
        const x = (v.x * 0.5 + 0.5) * rect.width, y = (-v.y * 0.5 + 0.5) * rect.height;
        if (px != null) {
          const dx = x - px, dy = y - py;
          const t = Math.max(0, Math.min(1, ((mx - px) * dx + (my - py) * dy) / (dx * dx + dy * dy || 1)));
          const d = (mx - px - dx * t) ** 2 + (my - py - dy * t) ** 2;
          if (d < bestD) { bestD = d; best = e; }
        }
        px = x; py = y;
      }
    }
    return best ? { type: 'edge', id: best.data.id } : null;
  }

  // ------------------------------------------------------------------ frame loop
  setPaused(paused) { this.paused = !!paused; }

  _frame() {
    if (this.paused) return;
    const dt = Math.min(0.05, this.clock.getDelta());
    this.time += dt;
    // View weights glide toward the selected view and always sum to 1.
    let changing = false;
    for (const v of VIEWS) {
      const t = v === this.view ? 1 : 0;
      const d = t - this.wv[v];
      if (Math.abs(d) > 0.0005) { this.wv[v] += d * Math.min(1, dt * 2.6); changing = true; } else this.wv[v] = t;
    }
    const W = { g: this.wv.galaxy, s: this.wv.strata };
    const k = 1 - Math.exp(-dt * 3.2);

    if (this.fly) {
      this.fly.t += dt / this.fly.dur;
      const t = ease(Math.min(1, this.fly.t));
      this.camera.position.lerpVectors(this.fly.p0, this.fly.p1, t);
      this.controls.target.lerpVectors(this.fly.t0, this.fly.t1, t);
      if (this.fly.t >= 1) this.fly = null;
    }
    this.controls.autoRotate = this.opts.rotate && !this.fly && this.time - this.lastInteraction > 8 && !this.selection;
    this.controls.update();
    if (this.insetTarget != null && Math.abs(this.insetTarget - this.insetShift) > 0.5) {
      this.insetShift += (this.insetTarget - this.insetShift) * Math.min(1, dt * 6);
      this._applyShift();
    }
    if (!this.graph) {
      this.renderer.render(this.scene, this.camera);
      return;
    }
    const hi = this.highlight;
    let moving = this.positionsDirty || changing;
    this.positionsDirty = false;
    const hostVis = W.s;

    // Nodes.
    const before = new THREE.Vector3();
    for (const obj of this.nodes.values()) {
      before.copy(obj.pos);
      obj.gal.lerp(obj.galTarget, k);
      obj.str.lerp(obj.strTarget, k);
      obj.pos.set(0, 0, 0).addScaledVector(obj.gal, W.g).addScaledVector(obj.str, W.s);
      if (before.distanceToSquared(obj.pos) > 1e-4) moving = true;
      obj.group.position.copy(obj.pos);
      const n = obj.data;
      const inHi = !hi || hi.nodes.has(n.id);
      const dimTo = inHi ? 1 : 0.12;
      obj.dim += (dimTo - obj.dim) * Math.min(1, dt * 6);
      const ghost = n.ghost ? 0.55 : 1;
      const pulse = obj.pulse ? 1 + 0.18 * Math.sin(this.time * 4.5) : 1;
      if (n.kind === 'host') {
        obj.wire.material.opacity = 0.9 * hostVis * obj.dim;
        obj.pick.material.opacity = 0.08 * hostVis * obj.dim;
        obj.glow.material.opacity = (n.risk?.level !== 'ok' ? 0.55 * pulse : 0.35) * hostVis * obj.dim;
        obj.group.visible = hostVis > 0.01;
      } else {
        const off = obj.offline ? 0.4 : 1;      // known but not on the network right now: dimmed, never red
        obj.glow.material.opacity = (n.kind === 'clients' ? 0.5 : 0.55) * obj.dim * ghost * off;
        obj.glow.scale.set(obj.size * 2.1 * pulse, obj.size * 2.1 * pulse, 1);
        obj.core.material.opacity = 0.95 * obj.dim * ghost * (obj.offline ? 0.6 : 1);
        if (obj.halo.visible) {
          const hs = obj.size * 1.35;
          obj.halo.scale.set(hs, hs, 1);
          obj.halo.material.opacity = 0.75 * obj.dim;
          obj.halo.material.rotation += dt * 0.4;
        }
      }
    }
    this.core.material.opacity = 0.5 * W.g;
    this.decor.visible = W.g > 0.01;
    if (this.decorPoints) {
      const a = this.decorPoints.geometry.attributes.pcolor;
      const fade = W.g * (hi ? 0.5 : 1);
      if (this._decorFade !== fade) {
        for (let i = 0; i < this.decorAlpha.length; i++) a.array[i * 4 + 3] = this.decorAlpha[i] * fade;
        a.needsUpdate = true;
        this._decorFade = fade;
      }
    }
    for (const L of LAYERS) {
      const o = this.planeObjs[L.key];
      o.disc.material.opacity = 0.13 * W.s;
      o.rim.material.opacity = 0.55 * W.s;
      o.label.element.style.opacity = W.s > 0.6 ? (W.s - 0.6) / 0.4 : 0;
      o.grp.visible = W.s > 0.01;
    }
    if (W.s > 0.01) this._anchorPlaneLabels();

    // Addresses settle onto their own plane in strata; in the galaxy only the members of a
    // rolled-up group are drawn, orbiting their star (a device's one address would be noise there).
    const tmp = new THREE.Vector3();
    for (let i = 0; i < this.pods.length; i++) {
      const p = this.pods[i];
      const host = this.nodes.get(p.wid);
      if (!host) continue;
      p.str.lerp(p.strTarget, k);
      const r = host.size * 0.62 + 2.5 + (p.i % 3) * 1.6;
      const a = p.phase + this.time * p.speed + (p.i / p.count) * Math.PI * 2;
      tmp.set(Math.cos(a) * r + host.pos.x, Math.sin(a) * r * p.incl * 0.6 + host.pos.y, Math.sin(a) * r + host.pos.z);
      p.pos.lerpVectors(tmp, p.str, W.s);
      this.podPos[i * 3] = p.pos.x; this.podPos[i * 3 + 1] = p.pos.y; this.podPos[i * 3 + 2] = p.pos.z;
      const inHi = !hi || hi.podKeys.has(p.key);
      const faded = (host.data.ghost ? 0.5 : 1) * (p.level === 'evicted' ? 0.55 : 1);
      this.podCol[i * 4 + 3] = (inHi ? 0.95 : 0.1) * faded * (p.data.pseudo ? 1 : W.s);
    }
    if (this.pods.length) {
      this.podGeo.attributes.position.needsUpdate = true;
      this.podGeo.attributes.pcolor.needsUpdate = true;
    }

    const optsKey = JSON.stringify(this.opts);
    if (moving || this._lastHi !== hi || this._lastOpts !== optsKey) {
      this._updateEdges(W);
      this._lastHi = hi;
      this._lastOpts = optsKey;
    }
    if (W.s > 0.01) this._updateDrops(W.s); else this.dropGeo.setDrawRange(0, 0);
    this._updateParticles(dt, W);

    // Selection ring.
    const ringId = this.selection?.type === 'node' ? this.selection.id : null;
    const selObj = ringId ? this.nodes.get(ringId) : null;
    if (selObj) {
      this.selRing.position.copy(selObj.pos);
      const s = selObj.size * 1.7 * (1 + 0.04 * Math.sin(this.time * 3));
      this.selRing.scale.set(s, s, 1);
      this.selRing.material.opacity = 0.9;
      this.selRing.material.rotation -= dt * 0.5;
    } else this.selRing.material.opacity = 0;

    if (this.hoverDirty) {
      this.hoverDirty = false;
      const hit = this.mouse.x < -2 ? null : this._pick();
      this.renderer.domElement.style.cursor = hit ? 'pointer' : 'grab';
      this.cb.onHover?.(hit, this.mouseClient);
    }
    if (!this._lblT || this.time - this._lblT > 0.2) { this._lblT = this.time; this._updateLabels(W); }

    this.renderer.render(this.scene, this.camera);
    this.labelRenderer.render(this.scene, this.camera);
  }

  _edgeColor(e, t, out) {
    // Traffic leaving the network fades from cyan into gold (the internet); warning and critical
    // still win, so a connection with a finding is never hidden by where it goes.
    if (e.extScope && e.lvl === 'ok') {
      const sc = scopeColor(e.extScope);
      return e.inferredOnly ? out.copy(sc).lerp(C.inferred, 0.35) : out.copy(C.cyan).lerp(sc, Math.min(1, 0.25 + t));
    }
    if (e.inScope && e.lvl === 'ok') return out.copy(scopeColor(e.inScope)).lerp(C.cyan, Math.min(1, 0.25 + t));
    if (e.inferredOnly) return out.copy(C.inferred);
    if (e.lvl === 'critical') return out.copy(C.crit);
    if (e.lvl === 'warning') return out.copy(C.warn);
    return out.copy(C.cyan).lerp(C.violet, t);
  }

  _updateEdges(W) {
    const hi = this.highlight;
    const col = new THREE.Color();
    const v = new THREE.Vector3();
    let vi = 0;
    for (const e of this.edges) {
      const sa = this.nodes.get(e.data.source), sb = this.nodes.get(e.data.target);
      const a = sa.pos, b = sb.pos;
      const dist = a.distanceTo(b);
      e.p0.copy(a); e.p2.copy(b);
      e.p1.copy(a).add(b).multiplyScalar(0.5);
      // Galaxy: gentle arcs above the disc. Strata: tall arches over the devices plane.
      const bothOnPlane = Math.abs(sa.strTarget.y - sb.strTarget.y) < 8;
      e.p1.y += W.g * dist * 0.16 + W.s * (dist * (0.16 + (bothOnPlane ? 0.32 : 0.05)) + 4);
      e.len = Math.max(1, dist * 1.15);
      // Minor bundles (little traffic, on a very large network) only show for what is selected.
      const visible = !(e.data.minor && !(hi && hi.edges.has(e.data.id)));
      const inHi = !hi || hi.edges.has(e.data.id);
      const base = e.inferredOnly ? 0.22 : e.idle ? 0.13 : 0.18 + 0.35 * e.level + (e.lvl !== 'ok' ? 0.25 : 0);
      const alpha = visible ? base * (inHi ? (hi ? 1.6 : 1) : 0.05) : 0;
      for (let s = 0; s < SEG; s++) {
        for (const tt of [s / SEG, (s + 1) / SEG]) {
          bezier(e.p0, e.p1, e.p2, tt, v);
          this.edgePos[vi * 3] = v.x; this.edgePos[vi * 3 + 1] = v.y; this.edgePos[vi * 3 + 2] = v.z;
          this._edgeColor(e, tt, col);
          const dash = e.inferredOnly && s % 2 === 1 ? 0 : 1;
          const fade = Math.min(1, tt * 6, (1 - tt) * 6) * 0.6 + 0.4;
          this.edgeCol[vi * 4] = col.r; this.edgeCol[vi * 4 + 1] = col.g; this.edgeCol[vi * 4 + 2] = col.b;
          this.edgeCol[vi * 4 + 3] = alpha * dash * fade;
          vi++;
        }
      }
    }
    this.edgeGeo.setDrawRange(0, vi);
    this.edgeGeo.attributes.position.needsUpdate = true;
    this.edgeGeo.attributes.color.needsUpdate = true;
  }

  _updateDrops(ws) {
    const hi = this.highlight;
    let vi = 0;
    const push = (a, b, c, alpha) => {
      for (let s = 0; s < DROP_SEG; s++) {
        if (s % 2) continue;
        for (const tt of [s / DROP_SEG, (s + 1) / DROP_SEG]) {
          this.dropPos[vi * 3] = a.x + (b.x - a.x) * tt;
          this.dropPos[vi * 3 + 1] = a.y + (b.y - a.y) * tt;
          this.dropPos[vi * 3 + 2] = a.z + (b.z - a.z) * tt;
          this.dropCol[vi * 4] = c.r; this.dropCol[vi * 4 + 1] = c.g; this.dropCol[vi * 4 + 2] = c.b;
          this.dropCol[vi * 4 + 3] = alpha;
          vi++;
        }
      }
    };
    for (const p of this.pods) {
      const w = this.nodes.get(p.wid);
      const h = this.nodes.get(p.data.hostId || `host:${p.data.node}`);
      const inHi = !hi || hi.podKeys.has(p.key);
      const alpha = ws * (inHi ? (hi ? 0.9 : 0.4) : 0.04);
      if (w) push(w.pos, p.pos, C.violet, alpha);
      if (h) push(p.pos, h.pos, C.up, alpha * 0.8);
    }
    this.dropGeo.setDrawRange(0, vi);
    this.dropGeo.attributes.position.needsUpdate = true;
    this.dropGeo.attributes.color.needsUpdate = true;
  }

  _updateParticles(dt, W) {
    const hi = this.highlight;
    const col = new THREE.Color();
    const v = new THREE.Vector3();
    let pi = 0;
    const emit = (curve, t, size, alpha, color, tail = 0.016) => {
      for (let j = 0; j < TAIL; j++) {
        const tt = t - j * tail;
        if (tt < 0 || tt > 1 || pi >= this.pCap) continue;
        bezier(curve.p0, curve.p1, curve.p2, tt, v);
        this.pPos[pi * 3] = v.x; this.pPos[pi * 3 + 1] = v.y; this.pPos[pi * 3 + 2] = v.z;
        const c = color || this._edgeColor(curve, tt, col);
        const fall = 1 - j / TAIL;
        this.pCol[pi * 4] = c.r; this.pCol[pi * 4 + 1] = c.g; this.pCol[pi * 4 + 2] = c.b;
        this.pCol[pi * 4 + 3] = alpha * fall;
        this.pSize[pi] = size * (0.45 + 0.55 * fall);
        pi++;
      }
    };
    // Pulses run from whoever opened the connection to the other end: more and faster with more traffic.
    for (const e of this.edges) {
      const inHi = !hi || hi.edges.has(e.data.id);
      if (hi && !inHi) continue;
      if (e.data.minor && !hi) continue;
      const quiet = e.inferredOnly || e.idle;
      const speed = (14 + 46 * e.level) * (quiet ? 0.35 : 1);
      const step = (speed * dt) / e.len;
      const size = quiet ? 2.4 : 3.2 + 3 * e.level;
      const alpha = quiet ? 0.35 : 0.95;
      for (let i = 0; i < e.phases.length; i++) {
        e.phases[i] = (e.phases[i] + step) % 1;
        emit(e, e.phases[i], size, alpha, null, 0.016 * (e.inferredOnly ? 0.5 : 1));
      }
    }
    for (let i = pi; i < Math.min(this.pCap, this._pLast || 0); i++) this.pCol[i * 4 + 3] = 0;
    this._pLast = pi;
    this.particleGeo.setDrawRange(0, Math.max(pi, 0));
    this.particleGeo.attributes.position.needsUpdate = true;
    this.particleGeo.attributes.pcolor.needsUpdate = true;
    this.particleGeo.attributes.size.needsUpdate = true;
  }

  // Labels never overlap. Names matter most, so they are placed first, in priority order: above the
  // node, else below it, else the label steps aside (hover or zoom brings it back). Then each label
  // that still has room gets its metrics line too. Runs with the label refresh, 5x/s.
  _declutter(items) {
    const w = this.container.clientWidth, h = this.container.clientHeight;
    const v = this._lblV || (this._lblV = new THREE.Vector3());
    const hit = (b, list, skip) => list.some((p) => p !== skip && b.x0 < p.x1 && b.x1 > p.x0 && b.y0 < p.y1 && b.y1 > p.y0);
    // Plane and arm captions are fixed furniture: node labels keep clear of them.
    const placed = [];
    for (const el of this.labelRenderer.domElement.querySelectorAll('.lbl.layer, .lbl.ns')) {
      if (el.style.display === 'none' || el.style.opacity === '0') continue;
      const r = el.getBoundingClientRect();
      if (r.width && r.height) placed.push({ x0: r.left, x1: r.right, y0: r.top, y1: r.bottom });
    }
    const base = this.labelRenderer.domElement.getBoundingClientRect();
    for (const p of placed) { p.x0 -= base.left; p.x1 -= base.left; p.y0 -= base.top; p.y1 -= base.top; }
    items.sort((a, b) => b.prio - a.prio);
    const shown = [];
    const v2 = this._lblV2 || (this._lblV2 = new THREE.Vector3());
    for (const it of items) {
      const el = it.el;
      v.setFromMatrixPosition(it.obj.label.matrixWorld).project(this.camera);
      if (v.z > 1 || v.z < -1) { el.classList.add('hidden'); continue; }
      // The label element is centred on its anchor, which floats just above the node.
      const x = (v.x * 0.5 + 0.5) * w, y = (-v.y * 0.5 + 0.5) * h;
      const ny = (-v2.copy(it.obj.pos).project(this.camera).y * 0.5 + 0.5) * h;
      const nameEl = el.firstElementChild, meta = el.children[1];
      const nw = (nameEl ? nameEl.offsetWidth : 60) || 60;
      const pad = el.classList.contains('hidden') ? 7 : 2;      // a label that stepped aside needs clear room to return
      const at = (cy) => ({ x0: x - nw / 2 - pad, x1: x + nw / 2 + pad, y0: cy - 8 - pad, y1: cy + 8 + pad });
      const drop = Math.max(26, 2 * (ny - y));
      const above = at(y), below = at(y + drop);
      const box = !hit(above, placed) ? above : below && !hit(below, placed) ? below : null;
      el.classList.toggle('hidden', !box);
      el.classList.add('compact');
      el.style.marginTop = box && box === below ? `${Math.round(drop)}px` : '';
      if (!box) continue;
      placed.push(box);
      if (meta && !it.compact && box === above) {
        shown.push({ el, box, x, y, nw, had: !!el._meta, mw: Math.max(nw, meta.textContent.trim().length * 5.6) });
      }
      el._meta = false;
    }
    for (const s of shown) {      // second pass: the metrics line, where nothing else needs the room
      // With its metrics the label grows to two lines around the same centre: name up, metrics under it.
      const name = { x0: s.x - s.nw / 2 - 2, x1: s.x + s.nw / 2 + 2, y0: s.y - 17, y1: s.y + 1 };
      const line = { x0: s.x - s.mw / 2, x1: s.x + s.mw / 2, y0: s.y + 1, y1: s.y + 15 };
      // Sticky: a line that is showing survives a near miss, one that is not needs clear room to
      // come back. Without it a label on the boundary blinks as the view drifts.
      const g = s.had ? -4 : 6;
      const grow = (b) => ({ x0: b.x0 - g, x1: b.x1 + g, y0: b.y0 - g, y1: b.y1 + g });
      if (hit(grow(name), placed, s.box) || hit(grow(line), placed, s.box)) continue;
      s.el._meta = true;
      s.el.classList.remove('compact');
      Object.assign(s.box, name);
      placed.push(line);
    }
  }

  _updateLabels(W) {
    const items = [];
    const hi = this.highlight;
    const many = this.nodes.size > 45;
    const camPos = this.camera.position;
    for (const obj of this.nodes.values()) {
      const n = obj.data;
      const el = obj.label.element;
      let show = this.opts.labels;
      if (n.kind === 'host') show = show && W.s > 0.5;
      else if (show && many && !hi) {
        const near = camPos.distanceTo(obj.pos) < 320;
        show = near || n.kind === 'clients' || n.kind === 'entry' || (n.risk && n.risk.level !== 'ok');
      }
      const dim = hi && !hi.nodes.has(n.id);
      // Strata packs devices on one ring: keep only names unless the node is in focus.
      const compact = W.s > 0.5 && this.nodes.size > 14 && !(hi && hi.nodes.has(n.id)) && n.kind !== 'host';
      el.classList.toggle('dim', !!dim && show);
      if (!show) { el.classList.add('hidden'); continue; }
      // What must stay readable when labels collide: the selection, then trouble, then the landmarks.
      const lvl = n.risk?.level;
      const prio = (hi && hi.nodes.has(n.id) ? 100 : 0) + (lvl === 'critical' ? 60 : lvl === 'warning' ? 45 : 0)
        + (n.kind === 'clients' ? (n.scope === 'lan' ? 40 : 58) : n.kind === 'entry' ? 30 : n.kind === 'workload' ? 20 + Math.min(10, Math.round(obj.size - 9))
          : n.kind === 'host' ? 25 : n.kind === 'service' ? 20 : 12) - (n.ghost ? 15 : 0);
      items.push({ obj, el, prio, compact });
    }
    this._declutter(items);
    for (const child of this.decor.children) {
      if (child.userData.nsLabel) child.element.style.opacity = this.opts.labels ? String(Math.max(0, 1 - (1 - W.g) * 1.6) * (hi ? 0.3 : 0.9)) : '0';
    }
  }
}

// Remove a subtree for good. The CSS2D renderer only deletes a label's DOM element when that label
// object itself is removed - not when an ancestor group is - so nested labels must be dropped here,
// or they stay frozen on screen. Also frees GPU geometry/materials (textures are cached and shared;
// sprites share one geometry, so it is left alone).
function disposeTree(root) {
  root.traverse((o) => {
    if (o.isCSS2DObject && o.element) o.element.remove();
    if (o.geometry && !o.isSprite) o.geometry.dispose();
    if (o.material) {
      (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => {
        if (m.map && m.map.userData.owned) m.map.dispose();   // per-object canvas textures
        m.dispose();
      });
    }
  });
}

function clearGroup(group) {
  for (const child of [...group.children]) disposeTree(child);
  group.clear();
}

function bezier(p0, p1, p2, t, out) {
  const u = 1 - t;
  out.set(u * u * p0.x + 2 * u * t * p1.x + t * t * p2.x,
    u * u * p0.y + 2 * u * t * p1.y + t * t * p2.y,
    u * u * p0.z + 2 * u * t * p1.z + t * t * p2.z);
  return out;
}

function gauss() {
  let u = 0, v = 0;
  while (u === 0) u = Math.random();
  while (v === 0) v = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}
