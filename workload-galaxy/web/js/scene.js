// Three.js rendering: one scene, four arrangements (galaxy / strata / cascade / gibson) blended by view weights.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import * as TX from './textures.js';
import { edgeMetric, esc, fmtAge, fmtBytes, fmtCpu, fmtMs, hash01, nsColor, nodeMetric, podLevel } from './format.js';
import { computeLayouts, podKey, CASCADE, CITY } from './layout.js';

const C = {
  cyan: new THREE.Color('#4fd1ff'), violet: new THREE.Color('#a07bff'), warn: new THREE.Color('#ffb547'),
  crit: new THREE.Color('#ff4d6d'), inferred: new THREE.Color('#8391b8'), white: new THREE.Color('#ffffff'),
  entry: new THREE.Color('#5ff1d9'), external: new THREE.Color('#93a9ff'), clients: new THREE.Color('#cfe9ff'),
  // Where an external dependency lives: your own network, the internet, or the cluster's own infrastructure.
  lan: new THREE.Color('#5ee6a8'), internet: new THREE.Color('#ffd75e'), clusterExt: new THREE.Color('#9fdcff'),
  podOk: new THREE.Color('#8f7dff'), hostOk: new THREE.Color('#c6fff0'), idle: new THREE.Color('#58627e'),
  up: new THREE.Color('#34e3b0'), down: new THREE.Color('#b18cff'),
};
const LAYERS = [
  { key: 'users', label: 'End users', color: '#4fd1ff' },
  { key: 'services', label: 'Services', color: '#4f7dff' },
  { key: 'pods', label: 'Pods & containers', color: '#8b5cf6' },
  { key: 'hosts', label: 'Hosts', color: '#19c9a0' },
];
const HOST_W = 18, HOST_H = 7, HOST_D = 11;
const SEG = 28;            // line segments per edge curve
const TAIL = 4;            // points per comet (head + tail)
const DROP_SEG = 10;

const VIEWS = ['galaxy', 'strata', 'cascade', 'gibson'];
// Gibson camera: a fixed isometric-ish direction (from front-right, above) and a narrow lens.
const ISO = new THREE.Vector3(0.6, 0.62, 0.95).normalize();
const GIBSON_FOV = 26;
const TEAL = new THREE.Color('#5cf2dc');
const COLUMN_COLOR = { users: '#4fd1ff', hop: '#6f7dff', external: '#93a9ff', lan: '#5ee6a8', internet: '#ffd75e', hosts: '#19c9a0' };
const SCOPE_LABEL = { lan: 'LAN', internet: 'INTERNET', cluster: 'CLUSTER' };
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
    this.opts = { pods: true, inferred: true, responses: false, labels: true, rotate: true };
    this.view = 'galaxy';
    this.wv = { galaxy: 1, strata: 0, cascade: 0, gibson: 0 };   // animated view weights (sum to 1)
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
    this._setupBloom();
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
    // Strata drop lines (workload -> pods -> host).
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
    // Pods.
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
      div.className = 'lbl layer';
      div.style.setProperty('--c', L.color);
      const label = new CSS2DObject(div);
      const grp = new THREE.Group();
      grp.add(disc, rim, label);
      this.planes.add(grp);
      this.planeObjs[L.key] = { grp, disc, rim, label, div };
    }
    // Cascade view: column panes + root-cause beacons (rebuilt per layout) and host->workload impact links.
    this.columnsGroup = new THREE.Group();
    this.root.add(this.columnsGroup);
    this.beacons = new THREE.Group();
    this.root.add(this.beacons);
    this.casGeo = new THREE.BufferGeometry();
    this.casLines = new THREE.LineSegments(this.casGeo, new THREE.LineBasicMaterial({
      vertexColors: true, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }));
    this.casLines.frustumCulled = false;
    this.root.add(this.casLines);
    this.cascadeLinks = [];
    // Gibson: circuit-board city (rebuilt per layout).
    this.cityGroup = new THREE.Group();
    this.root.add(this.cityGroup);
    this.cityMats = [];
    this.towers = new Map();
    this.faceTextures = [];
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
    if (this.composer) { this.composer.setPixelRatio(this.renderer.getPixelRatio()); this.composer.setSize(w, h); }
    this._updateScale();
  }

  _updateScale() {
    const h = this.container.clientHeight || window.innerHeight;
    this._uniformScale.value = (h * this.renderer.getPixelRatio()) / (2 * Math.tan((this.camera.fov * Math.PI) / 360));
  }

  // Bloom only runs in the Gibson view (the glass-and-neon look); other views render directly.
  _setupBloom() {
    try {
      this.composer = new EffectComposer(this.renderer);
      this.composer.addPass(new RenderPass(this.scene, this.camera));
      // High threshold: only bright edges and pulses glow, not the board or the glass.
      this.bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), 0, 0.35, 0.62);
      this.composer.addPass(this.bloom);
      this.composer.addPass(new OutputPass());
    } catch (err) {
      console.warn('bloom unavailable, rendering without it', err);
      this.composer = null;
    }
  }

  // ------------------------------------------------------------------ data
  setData(graph) {
    this.graph = graph;
    const L = computeLayouts(graph);
    this.layout = L;
    const byId = new Map(graph.nodes.map((n) => [n.id, n]));
    const inbound = new Map();
    for (const e of graph.edges) {
      if (!inbound.has(e.target)) inbound.set(e.target, []);
      inbound.get(e.target).push(e);
    }
    this.inbound = inbound;
    const first = this.nodes.size === 0;

    // Nodes: update in place, create new, remove gone.
    for (const [id, obj] of this.nodes) {
      if (!byId.has(id)) { disposeTree(obj.group); this.root.remove(obj.group); this.nodes.delete(id); }
    }
    for (const n of graph.nodes) {
      let obj = this.nodes.get(n.id);
      const g = L.galaxy.get(n.id) || { x: 0, y: 0, z: 0 };
      const s = L.strata.get(n.id) || g;
      const c = L.cascade.get(n.id) || g;
      const k2 = L.city.pos.get(n.id) || g;
      if (!obj) {
        obj = this._makeNode(n);
        obj.gal.set(g.x, g.y, g.z); obj.str.set(s.x, s.y, s.z); obj.cas.set(c.x, c.y, c.z); obj.gib.set(k2.x, k2.y, k2.z);
        if (!first) { obj.gal.multiplyScalar(0.85); obj.str.multiplyScalar(0.85); obj.cas.multiplyScalar(0.85); }
        this.nodes.set(n.id, obj);
      }
      obj.data = n;
      obj.galTarget.set(g.x, g.y, g.z);
      obj.strTarget.set(s.x, s.y, s.z);
      obj.casTarget.set(c.x, c.y, c.z);
      obj.gibTarget.set(k2.x, k2.y, k2.z);
      this._styleNode(obj);
    }

    // Pods.
    const oldPods = new Map(this.pods.map((p) => [p.key, p]));
    this.pods = [];
    for (const n of graph.nodes) {
      if (n.kind !== 'workload') continue;
      n.pods.forEach((p, i) => {
        const key = podKey(n.id, p.name);
        const slot = L.podSlots.get(key) || L.strata.get(n.id);
        const prev = oldPods.get(key);
        const st = prev ? prev.str : new THREE.Vector3(slot.x, slot.y, slot.z);
        this.pods.push({
          key, wid: n.id, data: p, i, count: n.pods.length, str: st,
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
      const inferredOnly = !e.evidence.some((x) => x === 'observed' || x === 'mesh' || x === 'declared');
      const idle = !!e.traffic.idle;   // connection open but quiet: real dependency, no live traffic
      const lvl = e.risk.level;
      const level = idle ? 0 : e.traffic.level || 0;
      const est = e.latency.estimateMs;
      const count = inferredOnly || idle ? 1 : Math.round(2 + 9 * level);
      const phases = prevPhase.get(e.id) && prevPhase.get(e.id).length === count ? prevPhase.get(e.id)
        : Array.from({ length: count }, (_, k) => (k + hash01(e.id)) / count);
      return {
        data: e, inferredOnly, idle, lvl, level, count, phases,
        extScope: byId.get(e.target)?.kind === 'external' ? (byId.get(e.target).scope || 'internet') : null,
        inScope: byId.get(e.source)?.kind === 'clients' && byId.get(e.source).scope === 'lan' ? 'lan' : null,
        slow: 1 / (1 + Math.log10(1 + (est || 0) / 25)),
        p0: new THREE.Vector3(), p1: new THREE.Vector3(), p2: new THREE.Vector3(), len: 1,
        ...routeOf(L.city.routes.get(e.id)),
      };
    });
    this._buildCascade();
    this._allocEdges();
    this._buildDecor();
    this._buildColumns();
    this._buildCity();
    this._layoutPlanes();
    this._sweepLabels();
    this._applyHighlight();
    this.positionsDirty = true;
    if (first) this.resetCamera(true);
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
    const obj = { group, gal: new THREE.Vector3(), str: new THREE.Vector3(), cas: new THREE.Vector3(),
      gib: new THREE.Vector3(), gibTarget: new THREE.Vector3(),
      galTarget: new THREE.Vector3(), strTarget: new THREE.Vector3(), casTarget: new THREE.Vector3(),
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
      if (!n.pseudo) {
        // Memory reservation gauge: fill = requests / allocatable, white line = actual usage.
        obj.gauge = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshBasicMaterial({
          color: C.up, transparent: true, opacity: 0.4, depthWrite: false, blending: THREE.AdditiveBlending }));
        const r = [[-0.5, 0, -0.5], [0.5, 0, -0.5], [0.5, 0, 0.5], [-0.5, 0, 0.5]].map((p) => new THREE.Vector3(...p));
        obj.usage = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(r), new THREE.LineBasicMaterial({
          color: 0xffffff, transparent: true, depthWrite: false }));
        obj.usage.scale.set(HOST_W - 0.6, 1, HOST_D - 0.6);
        group.add(obj.gauge, obj.usage);
      }
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
    let color, size;
    if (n.kind === 'workload') {
      const base = new THREE.Color(nsColor(n.namespace, n.platform));
      color = lvlColor ? lvlColor.clone() : base.lerp(C.white, 0.35);
      if (n.idle) color = C.idle.clone();
      size = 9 + 4.2 * Math.sqrt(Math.max(1, n.pods.length));
    } else if (n.kind === 'clients') {   // users on your network: smaller and green, like the rest of your LAN
      color = n.scope === 'lan' ? C.lan.clone().lerp(C.white, 0.45) : C.clients.clone();
      size = n.scope === 'lan' ? 20 : 30;
    }
    else if (n.kind === 'entry') { color = C.entry.clone(); size = 12; }
    else if (n.kind === 'external') { color = scopeColor(n.scope).clone(); size = 11; }
    else if (n.kind === 'service') { color = C.crit.clone(); size = 11; }
    else { color = lvlColor ? lvlColor.clone() : C.hostOk.clone(); size = 14; }
    if (n.ghost) color.lerp(new THREE.Color('#566180'), 0.55);
    obj.color = color;
    obj.size = size;
    if (n.kind === 'host') {
      if (n.pseudo) color = C.crit.clone();
      obj.color = color;
      obj.wire.material.color.copy(color);
      obj.pick.material.color.copy(color);
      obj.glow.material.color.copy(color);
      if (obj.gauge) {
        const req = Math.max(0.02, Math.min(1, n.memRequestRatio || 0));
        const used = Math.max(0, Math.min(1, n.memRatio || 0));
        obj.gauge.scale.set(HOST_W - 1, HOST_H * req, HOST_D - 1);
        obj.gauge.position.y = -HOST_H / 2 + (HOST_H * req) / 2;
        obj.gauge.material.color.copy(req >= 0.9 ? C.crit : req >= 0.75 ? C.warn : C.up);
        obj.usage.position.y = -HOST_H / 2 + HOST_H * used;
        obj.usage.visible = used > 0;
      }
    } else {
      obj.glow.material.color.copy(color);
      obj.core.material.color.copy(n.kind === 'workload' && !lvlColor && !n.idle ? C.white.clone().lerp(color, 0.35) : color);
      const coreScale = n.kind === 'workload' ? size * 0.55 : n.kind === 'clients' ? size : size * 1.25;
      obj.core.scale.set(coreScale, coreScale, 1);
      obj.glow.scale.set(size * 2.1, size * 2.1, 1);
      const impact = n.risk?.impactLevel;
      obj.halo.visible = !!impact && impact !== 'ok' && lvl === 'ok' && n.kind === 'workload';
      if (obj.halo.visible) {
        obj.halo.material.color.copy(impact === 'critical' ? C.crit : C.warn);
        obj.halo.scale.set(size * 1.35, size * 1.35, 1);
      }
    }
    obj.pulse = lvl === 'critical';
    const ro = n.rollout && { progressing: 'rolling out', stuck: 'rollout stuck', failed: 'rollout failed' }[n.rollout.state];
    const metric = n.kind !== 'host' ? [ro, nodeMetric(n, this.inbound.get(n.id))].filter(Boolean).join(' · ')
      : n.pseudo ? `${n.podCount} pod${n.podCount === 1 ? '' : 's'} waiting for a node`
      : [`${n.podCount} pods`, n.memRequestRatio != null ? `mem ${Math.round(n.memRequestRatio * 100)}% reserved` : '',
        n.cpuRatio != null ? `cpu ${Math.round(n.cpuRatio * 100)}%` : ''].filter(Boolean).join(' · ');
    const lanUsers = n.kind === 'clients' && n.scope === 'lan';
    obj.label.element.className = `lbl ${lvl}${n.ghost ? ' ghost' : ''}${n.kind === 'external' ? ` ext-${n.scope || 'internet'}` : lanUsers ? ' ext-lan' : ''}`;
    const nsBit = n.ghost && n.namespace ? `<span class="m">${esc(n.namespace)}/</span>` : '';
    const scopeTag = n.kind === 'external' ? `<span class="scope">${SCOPE_LABEL[n.scope] || 'INTERNET'}</span> `
      : lanUsers ? '<span class="scope">LAN</span> ' : '';
    obj.label.element.innerHTML = `<div class="n">${nsBit}${esc(n.name)}</div>${metric || scopeTag ? `<div class="m">${scopeTag}${esc(metric || '')}</div>` : ''}`;
    obj.labelY = n.kind === 'host' ? 7 : size * 0.45;
    obj.label.position.set(0, obj.labelY, 0);
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
    for (const e of this.edges) np += e.count * 2;   // room for response comets
    np += this.cascadeLinks.length * 2;              // cascade shock pulses
    this.pCap = np * TAIL;
    const hl = this.cascadeLinks.filter((l) => !l.edge).length;
    this.casPos = new Float32Array(Math.max(1, hl) * SEG * 2 * 3);
    this.casCol = new Float32Array(Math.max(1, hl) * SEG * 2 * 4);
    this.casGeo.setAttribute('position', new THREE.BufferAttribute(this.casPos, 3));
    this.casGeo.setAttribute('color', new THREE.BufferAttribute(this.casCol, 4));
    this.pPos = new Float32Array(this.pCap * 3);
    this.pCol = new Float32Array(this.pCap * 4);
    this.pSize = new Float32Array(this.pCap);
    this.particleGeo.setAttribute('position', new THREE.BufferAttribute(this.pPos, 3));
    this.particleGeo.setAttribute('pcolor', new THREE.BufferAttribute(this.pCol, 4));
    this.particleGeo.setAttribute('size', new THREE.BufferAttribute(this.pSize, 1));
    // Drop lines: workload->pod and pod->host, DROP_SEG segments each.
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
    // Spiral-arm dust, tinted per namespace.
    const { R1, RS, TWIST } = { R1: 48, RS: 32, TWIST: 0.34 };
    for (const arm of L.arms) {
      const c = new THREE.Color(nsColor(arm.namespace, arm.platform));
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
      div.style.setProperty('--c', nsColor(arm.namespace, arm.platform));
      div.innerHTML = `<div class="n">${esc(arm.namespace || 'cluster')}</div>`;
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

  // Failure cascades: for every root cause, the links its impact travels back along
  // (callee -> caller, and host -> the workloads on it). Drawn as backward shock pulses.
  _buildCascade() {
    const prev = new Map(this.cascadeLinks.map((l) => [l.key, l.phases]));
    const edgeById = new Map(this.edges.map((e) => [e.data.id, e]));
    const inc = new Map();
    for (const e of this.edges) {
      if (!inc.has(e.data.target)) inc.set(e.data.target, []);
      inc.get(e.data.target).push(e);
    }
    const links = [];
    this.cascadeByRoot = new Map();
    for (const c of this.graph.cascades || []) {
      const impacted = new Set(c.impacted);
      if (c.userFacing) {   // carry the pulse out to the users (internet and your network)
        for (const [id, o] of this.nodes) if (o.data.kind === 'clients') impacted.add(id);
      }
      const mine = [];
      const add = (l) => {
        l.key = `${c.root}|${l.edge ? l.edge.data.id : `${l.from}>${l.to}`}`;
        l.root = c.root;
        l.level = c.level;
        l.phases = prev.get(l.key) || [hash01(l.key), (hash01(l.key) + 0.5) % 1];
        mine.push(l);
      };
      const frontier = [];
      if (c.kind === 'host') {
        for (const wid of impacted) {
          const n = this.nodes.get(wid)?.data;
          const runsThere = n && n.kind === 'workload' &&
            n.pods.some((p) => (p.hostId || `host:${p.node}`) === c.root && p.level !== 'evicted');
          if (!runsThere) continue;
          if (this.nodes.has(c.root)) {
            add({ from: c.root, to: wid, edge: null, p0: new THREE.Vector3(), p1: new THREE.Vector3(), p2: new THREE.Vector3(), len: 1 });
          }
          frontier.push(wid);
        }
      } else if (edgeById.has(c.root)) {
        const e = edgeById.get(c.root);
        add({ edge: e });
        frontier.push(e.data.source);
      } else {
        frontier.push(c.root);
      }
      const seen = new Set(frontier);
      while (frontier.length) {
        const u = frontier.pop();
        for (const e of inc.get(u) || []) {
          const s = e.data.source;
          if (!impacted.has(s)) continue;
          add({ edge: e });
          if (!seen.has(s)) { seen.add(s); frontier.push(s); }
        }
      }
      this.cascadeByRoot.set(c.root, { cascade: c, links: mine });
      links.push(...mine);
    }
    this.cascadeLinks = links;
  }

  _buildColumns() {
    clearGroup(this.columnsGroup);
    clearGroup(this.beacons);
    this.columnObjs = [];
    for (const col of this.layout.columns) {
      const color = new THREE.Color(COLUMN_COLOR[col.kind]);
      const w = CASCADE.DX * 0.64;
      const h = col.top - col.bottom + CASCADE.DY * 1.7 + (col.kind === 'hosts' ? 14 : 0);
      const pane = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({
        color, transparent: true, opacity: 0, depthWrite: false, side: THREE.DoubleSide, blending: THREE.AdditiveBlending }));
      const corners = [[-w / 2, -h / 2], [w / 2, -h / 2], [w / 2, h / 2], [-w / 2, h / 2]].map(([x, y]) => new THREE.Vector3(x, y, 0));
      const rim = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(corners), new THREE.LineBasicMaterial({
        color, transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending }));
      let crit = 0, warn = 0;
      for (const id of col.ids) {
        const l = this.nodes.get(id)?.data.risk?.level;
        if (l === 'critical') crit++; else if (l === 'warning') warn++;
      }
      const div = document.createElement('div');
      div.className = 'lbl layer column';
      div.style.setProperty('--c', COLUMN_COLOR[col.kind]);
      div.innerHTML = `<div class="n">${esc(col.label)}</div><div class="m">${col.count}${crit ? ` · <span class="c">${crit} critical</span>` : ''}${warn ? ` · <span class="w">${warn} warning</span>` : ''}</div>`;
      const label = new CSS2DObject(div);
      label.position.set(0, h / 2 + 6, 0);
      const grp = new THREE.Group();
      grp.position.set(col.x, (col.top + col.bottom) / 2, col.z - 10);
      grp.add(pane, rim, label);
      this.columnsGroup.add(grp);
      this.columnObjs.push({ pane, rim, label });
    }
    // A light pillar on every root cause.
    this.beaconObjs = [];
    for (const c of this.graph.cascades || []) {
      const anchor = this.nodes.has(c.root) ? c.root : c.root.includes('->') ? c.root.split('->')[1] : null;
      if (!anchor || !this.nodes.has(anchor)) continue;
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: TX.beam(), color: c.level === 'critical' ? C.crit : C.warn,
        transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: 0 }));
      sprite.center.set(0.5, 0);
      this.beacons.add(sprite);
      this.beaconObjs.push({ sprite, anchor, root: c.root, weight: c.weight || 1 });
    }
  }

  // ------------------------------------------------------------------ Gibson (city) view
  // Towers = workloads, floors = pods, districts = namespaces, circuit traces = calls.
  _buildCity() {
    clearGroup(this.cityGroup);
    this.cityMats = [];
    this.towers = new Map();
    this.faceTextures = [];
    this.cityPick = [];
    const L = this.layout.city;
    const { S } = CITY;
    const lineMat = (color, base, wid = null) => {
      const m = new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending });
      this.cityMats.push({ mat: m, base, wid });
      return m;
    };
    // Glass (normal blending, front faces only) so stacked floors read as layers instead of adding up
    // to white; additive only for light sources (text, board traces).
    const fillMat = (color, base, wid = null, map = null, additive = !!map) => {
      const m = new THREE.MeshBasicMaterial({ color, map, transparent: true, opacity: 0, depthWrite: false,
        side: additive ? THREE.DoubleSide : THREE.FrontSide, blending: additive ? THREE.AdditiveBlending : THREE.NormalBlending });
      this.cityMats.push({ mat: m, base, wid });
      return m;
    };
    this._cityLineMat = lineMat;
    this._cityFillMat = fillMat;
    const ground = (w, d, mat, x, z, y = 0) => {
      const m = new THREE.Mesh(new THREE.PlaneGeometry(w, d), mat);
      m.rotation.x = -Math.PI / 2;
      m.position.set(x, y, z);
      this.cityGroup.add(m);
      return m;
    };
    const rect = (x0, z0, x1, z1, mat, y = 0.15) => {
      const pts = [[x0, z0], [x1, z0], [x1, z1], [x0, z1]].map(([x, z]) => new THREE.Vector3(x, y, z));
      const l = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts), mat);
      this.cityGroup.add(l);
      return l;
    };
    const boxEdges = (w, h, d, mat, x, y, z) => {
      const l = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(w, h, d)), mat);
      l.position.set(x, y, z);
      this.cityGroup.add(l);
      return l;
    };
    const label = (html, cls, color, x, y, z) => {
      const div = document.createElement('div');
      div.className = `lbl ${cls}`;
      div.style.setProperty('--c', color);
      div.innerHTML = html;
      const o = new CSS2DObject(div);
      o.position.set(x, y, z);
      this.cityGroup.add(o);
    };

    // The board: circuit traces underfoot, a bright rim.
    const b = L.bounds;
    const bw = b.x1 - b.x0, bd = b.z1 - b.z0;
    const tex = TX.circuitBoard();
    tex.repeat.set(bw / 230, bd / 230);
    ground(bw, bd, fillMat('#4a3fd6', 0.32, null, tex), (b.x0 + b.x1) / 2, (b.z0 + b.z1) / 2, -0.2);
    rect(b.x0, b.z0, b.x1, b.z1, lineMat('#6f7dff', 0.6), 0);

    // Districts: one plate per namespace / project.
    // Connector zones: one tinted plate per row (your network / the internet), labelled at its back
    // end, clear of the connector names along it.
    for (const r of L.rims || []) {
      const sc = scopeColor(r.scope);
      ground(r.w, r.z1 - r.z0, fillMat(sc, 0.07), r.x, (r.z0 + r.z1) / 2, 0.04);
      rect(r.x - r.w / 2, r.z0, r.x + r.w / 2, r.z1, lineMat(sc, 0.75));
      label(`<div class="n">${r.scope === 'lan' ? 'Your network' : 'Internet'}</div><div class="m">${r.scope === 'lan' ? 'private addresses, outside the cluster' : 'public addresses'}</div>`,
        'district', `#${sc.getHexString()}`, r.x - r.w / 2 + 2, 0.3, r.z0 + 1);
    }
    for (const d of L.districts) {
      const c = new THREE.Color(nsColor(d.namespace, d.platform));
      ground(d.x1 - d.x0, d.z1 - d.z0, fillMat(c, 0.06), (d.x0 + d.x1) / 2, (d.z0 + d.z1) / 2, 0.05);
      rect(d.x0, d.z0, d.x1, d.z1, lineMat(c, 0.8));
      label(`<div class="n">${esc(d.namespace || 'cluster')}</div><div class="m">${d.count}</div>`, 'district',
        nsColor(d.namespace, d.platform), d.x0 + 2, 0.3, d.z1 - 1);
    }

    // The internet gateway, on the board's front edge: the only way between your network and the
    // internet. A faint portal fills the arch; every internet-bound trace passes through it.
    const gm = lineMat('#4fd1ff', 0.9);
    for (const sx of [-16, 16]) boxEdges(3, 18, 3, gm, L.gate.x + sx, 9, L.gate.z);
    boxEdges(38, 3, 3, gm, L.gate.x, 19.5, L.gate.z);
    const portal = new THREE.Mesh(new THREE.PlaneGeometry(29, 18), fillMat('#4fd1ff', 0.08, null, null, true));
    portal.position.set(L.gate.x, 9, L.gate.z);
    this.cityGroup.add(portal);
    label('<div class="n">Internet gateway</div><div class="m">in and out of your network</div>', 'district', '#4fd1ff',
      L.gate.x - 18, 26, L.gate.z);
    // The internet: its own plane below and beyond the board, holding the end users and every internet
    // service. Your network stays on the board.
    if (L.internet) {
      const I = L.internet;
      ground(I.x1 - I.x0, I.z1 - I.z0, fillMat(C.internet, 0.05), (I.x0 + I.x1) / 2, (I.z0 + I.z1) / 2, I.y);
      rect(I.x0, I.z0, I.x1, I.z1, lineMat(C.internet, 0.7), I.y + 0.1);
      // A funnel from the gateway's two pillars down to the plane's back corners: the plane hangs off
      // the gateway (its only way in), so these always land on something, however wide the plane is.
      const sm = lineMat(C.internet, 0.25);
      for (const [px, x] of [[L.gate.x - 16, I.x0], [L.gate.x + 16, I.x1]]) {
        const l = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(px, 0, L.gate.z),
          new THREE.Vector3(x, I.y, I.z0)]), sm);
        this.cityGroup.add(l);
      }
      // Centred on the plane's front edge (labels are centred on their anchor), clear of the traces.
      // Two zones by role: who comes in, and what the cluster calls out to. Their captions name the
      // plane too (one label per zone keeps the front edge readable).
      const role = { in: ['Internet · coming in', 'users of your exposed services', '#cfe9ff'],
        out: ['Internet · called from the cluster', 'your workloads connect out to these', '#ffd75e'] };
      for (const z of I.zones || []) {
        const [name, sub, col] = role[z.role];
        ground(z.x1 - z.x0, z.z1 - z.z0, fillMat(col, 0.04), (z.x0 + z.x1) / 2, (z.z0 + z.z1) / 2, I.y + 0.05);
        rect(z.x0, z.z0, z.x1, z.z1, lineMat(col, 0.45), I.y + 0.12);
        label(`<div class="n">${name}</div><div class="m">${sub}</div>`, 'district', col, (z.x0 + z.x1) / 2, I.y + 0.3, z.z1 + 7);
      }
    }

    // Edge connectors (external dependencies) and chip pads (hosts).
    for (const n of this.graph.nodes) {
      const pos = L.pos.get(n.id);
      if (!pos) continue;
      if (n.kind === 'external') {
        const sc = scopeColor(n.scope);
        boxEdges(12, 1.2, 7, lineMat(sc, 0.9), pos.x, pos.y + 0.6, pos.z);
        ground(12, 7, fillMat(sc, 0.14), pos.x, pos.z, pos.y + 0.1);
      } else if (n.kind === 'host') {
        ground(S * 1.3, S * 0.9, fillMat(n.pseudo ? C.crit : C.up, 0.08), pos.x, pos.z, 0.08);
        rect(pos.x - S * 0.65, pos.z - S * 0.45, pos.x + S * 0.65, pos.z + S * 0.45, lineMat(n.pseudo ? C.crit : C.up, 0.6));
      }
    }

    for (const n of this.graph.nodes) {
      if (n.kind === 'workload' && L.pos.get(n.id)) this._buildTower(n, L.pos.get(n.id));
    }
  }

  _buildTower(n, p) {
    const { TOWER, FLOOR, PLINTH } = CITY;
    const lineMat = this._cityLineMat, fillMat = this._cityFillMat;
    const lvl = n.risk?.level || 'ok';
    const frameColor = lvl === 'critical' ? C.crit.clone() : lvl === 'warning' ? C.warn.clone() : TEAL.clone();
    if (n.ghost || n.idle) frameColor.lerp(new THREE.Color('#566180'), 0.6);
    const grp = new THREE.Group();
    grp.position.set(p.x, 0, p.z);
    const rank = { ok: 0, warning: 1, critical: 2 };
    // Floors = running pods; problems on the top floors, where they're seen first.
    const live = n.pods.filter((pd) => podLevel(pd) !== 'evicted' && !(pd.phase === 'Pending' && !pd.node))
      .sort((a, b) => rank[podLevel(a)] - rank[podLevel(b)]);
    const height = PLINTH + live.length * FLOOR;
    // The plinth: dark glass base carrying the name; clicking it selects the workload.
    const plinth = new THREE.Mesh(new THREE.BoxGeometry(TOWER, PLINTH * 0.94, TOWER), fillMat('#0e3140', live.length ? 0.6 : 0.35, n.id));
    plinth.position.y = PLINTH * 0.47;
    plinth.userData = { nodeId: n.id };
    const plinthEdges = new THREE.LineSegments(new THREE.EdgesGeometry(plinth.geometry), lineMat(frameColor, 0.35, n.id));
    plinthEdges.position.y = plinth.position.y;
    grp.add(plinth, plinthEdges);
    this.cityPick.push(plinth);
    const slab = new THREE.BoxGeometry(TOWER, FLOOR * 0.8, TOWER);
    const slabEdges = new THREE.EdgesGeometry(slab);
    live.forEach((pod, i) => {
      const pl = podLevel(pod);
      const color = pl === 'critical' ? C.crit : pl === 'warning' ? C.warn : TEAL;
      const m = new THREE.Mesh(slab, fillMat(color, pl === 'ok' ? 0.16 : 0.42, n.id));
      m.position.y = PLINTH + i * FLOOR + FLOOR * 0.45;
      m.userData = { podKey: podKey(n.id, pod.name), pod, wid: n.id };
      const e = new THREE.LineSegments(slabEdges, lineMat(color, pl === 'ok' ? 0.5 : 1, n.id));
      e.position.y = m.position.y;
      grp.add(m, e);
      this.cityPick.push(m);
    });
    const frame = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(TOWER + 0.8, height + 0.4, TOWER + 0.8)),
      lineMat(frameColor, 0.85, n.id));
    frame.position.y = height / 2;
    grp.add(frame);
    // Two faces of live telemetry (front and right, the sides the camera sees).
    const scale = n.id === this.focusTowerId ? 4 : 1;
    const tex = this._faceTexture(n, scale);
    const faceW = TOWER - 1.4, faceH = height - 0.8;
    this._fitFace(tex, faceW, faceH);
    this.faceTextures.push(tex);
    const faces = [];
    for (const side of [0, 1]) {
      const f = new THREE.Mesh(new THREE.PlaneGeometry(faceW, faceH), fillMat(frameColor.clone().lerp(C.white, 0.45), 1, n.id, tex));
      f.position.set(side ? TOWER / 2 + 0.5 : 0, height / 2, side ? 0 : TOWER / 2 + 0.5);
      if (side) f.rotation.y = Math.PI / 2;
      grp.add(f);
      faces.push(f);
    }
    // Pods the scheduler couldn't place: dashed floors hovering above the roof, never built.
    const pending = n.pods.filter((pd) => pd.phase === 'Pending' && !pd.node);
    pending.forEach((pod, j) => {
      const dm = new THREE.LineDashedMaterial({ color: C.crit, dashSize: 1.1, gapSize: 0.8, transparent: true, opacity: 0, depthWrite: false });
      this.cityMats.push({ mat: dm, base: 0.95, wid: n.id });
      const ghostFloor = new THREE.LineSegments(new THREE.EdgesGeometry(slab), dm);
      ghostFloor.computeLineDistances();
      ghostFloor.position.y = height + FLOOR * (0.9 + j);
      const hit = new THREE.Mesh(slab, fillMat(C.crit, 0.05, n.id));
      hit.position.y = ghostFloor.position.y;
      hit.userData = { podKey: podKey(n.id, pod.name), pod, wid: n.id };
      grp.add(ghostFloor, hit);
      this.cityPick.push(hit);
    });
    // Evicted pods: a dark rubble slab at the base.
    if (n.pods.some((pd) => podLevel(pd) === 'evicted')) {
      const r = new THREE.Mesh(new THREE.BoxGeometry(TOWER + 4, 0.5, TOWER + 4), fillMat(C.idle, 0.35, n.id));
      r.position.y = 0.25;
      grp.add(r);
    }
    this.cityGroup.add(grp);
    this.towers.set(n.id, { group: grp, height: height + pending.length * FLOOR, faceHeight: height, node: n, faces, faceW, faceH, scale });
  }

  // The tower's skin: only real data about this workload, drawn like a terminal.
  _faceLines(n) {
    const ink = '#eafffb', dim = '#8ff5e6', bad = '#ff7d93', warn = '#ffd07f', faint = '#5fd3c2';
    const colorFor = (l) => (l === 'critical' ? bad : l === 'warning' ? warn : l === 'evicted' ? '#7d87a3' : dim);
    const name = (id) => this.nodes.get(id)?.data.name || id.split('/').pop();
    const out = [];
    const T = (t, c = dim, sz = 13, w = 600) => out.push({ t, c, s: sz, w });
    const H = (t) => out.push({ t, c: ink, s: 12, w: 700, gap: 9, rule: true });
    const r = n.risk || {};
    T(n.name.toUpperCase(), ink, 18, 700);
    T(`${n.namespace} · ${n.subkind}`);
    T(`REPLICAS ${n.replicas.ready}/${n.replicas.desired}${n.hpa ? ` · HPA ${n.hpa.min}-${n.hpa.max}` : ''}${n.idle ? ' · SCALED TO 0' : ''}`,
      n.replicas.ready < n.replicas.desired ? warn : dim);
    T(`RISK ${(r.level || 'ok').toUpperCase()} ${Math.round((r.score || 0) * 100)}`, colorFor(r.level));
    if (r.impactLevel && r.impactLevel !== 'ok' && r.impactFrom) {
      const f = r.impactFrom;
      const root = f.includes('->') ? f.split('->').map((x) => name(x)).join(' > ') : f.startsWith('host:') ? f.slice(5) : name(f);
      T(`IMPACT ${Math.round(r.impact * 100)} FROM ${root}`, colorFor(r.impactLevel));
    }
    for (const img of n.images) T(`IMAGE ${img}`, faint, 12, 500);
    for (const sv of n.services) {
      T(`SVC ${sv.name} ${sv.type} ${sv.ports.join(',')}${sv.lbIPs.length ? ' @' + sv.lbIPs[0] : ''} · ${sv.readyEndpoints} READY${sv.notReadyEndpoints ? ` ${sv.notReadyEndpoints} NOT` : ''}`, faint, 12, 500);
    }
    if (n.hosts.length) T(`HOSTS ${n.hosts.join(', ')}`, faint, 12, 500);
    if (n.rollout) {
      const ro = n.rollout;
      T(`ROLLOUT ${ro.state.toUpperCase()}${ro.revision ? ' · REV ' + ro.revision : ''}${ro.ageSeconds != null ? ' · ' + fmtAge(ro.ageSeconds) + ' AGO' : ''}`,
        ro.state === 'complete' ? faint : ro.state === 'progressing' ? warn : bad, 12, 600);
      for (const c of ro.state === 'complete' ? [] : ro.changes.slice(0, 3)) T(`  ${c}`, faint, 11, 500);
    }
    // Diagnosis first (what is wrong and what to do), then the raw signals and pod issues.
    const seen = new Set();
    const cards = [];
    for (const f of (this.graph.findings || []).filter((x) => x.subject === n.id && x.severity !== 'info').slice(0, 3)) {
      cards.push([f.severity, `${f.title}. NEXT: ${f.fix}`]);
      seen.add(f.title);
    }
    for (const x of r.reasons || []) cards.push([x.weight >= 0.6 ? 'critical' : 'warning', x.text]);
    for (const p of n.pods) for (const i of p.issues || []) if (podLevel(p) === 'critical' || podLevel(p) === 'warning') cards.push([podLevel(p), `${p.name.slice(-5)}: ${i}`]);
    for (const [lvl, text] of cards) {
      if (seen.has(text) || seen.size >= 5) continue;
      seen.add(text);
      out.push({ card: lvl, title: lvl === 'critical' ? 'ERROR' : 'WARNING', t: text });
    }
    H(`PODS ${n.pods.length}`);
    const status = (p) => (p.reason || p.phase).toUpperCase();
    for (const p of n.pods.slice(0, 30)) {
      const pl = podLevel(p);
      T(`${p.name.slice(-11)} ${status(p)} ${p.ready ? 'READY' : 'NOT READY'} R${p.restarts} ${fmtAge(p.ageSeconds)}`, colorFor(pl));
      T(`  ${p.node || 'NO NODE'} · ${p.ip || 'no ip'}${p.qos ? ' · ' + p.qos : ''}`, faint, 12, 500);
      if (p.cpu != null || p.mem != null) {
        T(`  CPU ${fmtCpu(p.cpu)}${p.cpuLimit ? '/' + fmtCpu(p.cpuLimit) : ''} · MEM ${fmtBytes(p.mem)}${p.memLimit ? '/' + fmtBytes(p.memLimit) : ''}`, faint, 12, 500);
      }
      for (const i of p.issues || []) T(`  ! ${i}`, colorFor(pl), 12, 500);
    }
    if (n.pods.length > 30) T(`+${n.pods.length - 30} more pods`, faint, 12, 500);
    const inc = this.graph.edges.filter((e) => e.target === n.id);
    const outE = this.graph.edges.filter((e) => e.source === n.id);
    if (inc.length) H(`CALLED BY ${inc.length}`);
    for (const e of inc.slice(0, 12)) T(`< ${name(e.source)}${edgeMetric(e) ? ' · ' + edgeMetric(e) : ''}`, colorFor(e.risk.level), 12, 500);
    if (outE.length) H(`CALLS ${outE.length}`);
    for (const e of outE.slice(0, 12)) {
      T(`> ${name(e.target)} ${e.ports.join(',')}`, colorFor(e.risk.level), 12, 600);
      T(`  ${edgeMetric(e) || 'no traffic observed'} · ${e.evidence.join('/')}`, faint, 12, 500);
      for (const d of e.details.slice(0, 2)) T(`  ${d}`, faint, 11, 500);
    }
    const c = (this.graph.cascades || []).find((x) => x.root === n.id);
    if (c) {
      H('BLAST RADIUS');
      T(`${c.count} DEGRADED DEPENDENTS${c.userFacing ? ' · REACHES USERS' : ''}`, colorFor(c.level), 12, 600);
      T(c.impacted.map((x) => name(x)).join(', '), faint, 12, 500);
    }
    return out;
  }

  // Render the lines at `scale` (1 = overview, 4 = the focused tower, crisp up close).
  _faceTexture(n, scale = 1) {
    const items = this._faceLines(n);
    const Wpx = 256 * scale, pad = 9 * scale, inner = Wpx - pad * 2;
    const c = document.createElement('canvas');
    c.width = Wpx; c.height = 16;
    let g = c.getContext('2d');
    const font = (sz, w) => `${w} ${sz * scale}px ui-monospace, "SF Mono", Menlo, Consolas, monospace`;
    const wrap = (text, f, width) => {
      g.font = f;
      const lines = [];
      let cur = '';
      for (const word of String(text).split(' ')) {
        const tryLine = cur ? `${cur} ${word}` : word;
        if (g.measureText(tryLine).width <= width) { cur = tryLine; continue; }
        if (cur) lines.push(cur);
        cur = word;
        while (g.measureText(cur).width > width && cur.length > 1) {   // hard-break very long tokens
          let k = cur.length;
          while (k > 1 && g.measureText(cur.slice(0, k)).width > width) k--;
          lines.push(cur.slice(0, k));
          cur = cur.slice(k);
        }
      }
      if (cur) lines.push(cur);
      return lines;
    };
    // Layout pass.
    const ops = [];
    let y = pad;
    for (const it of items) {
      if (it.card) {
        const tf = font(12, 700), bf = font(11, 500);
        const body = wrap(it.t, bf, inner - 12 * scale);
        const h = (16 + body.length * 15 + 8) * scale;
        y += 5 * scale;
        ops.push({ card: it.card, title: it.title, tf, bf, body, y, h });
        y += h + 3 * scale;
        continue;
      }
      if (it.gap) y += it.gap * scale;
      const f = font(it.s, it.w);
      const lines = wrap(it.t, f, inner);
      lines.forEach((ln, i) => { ops.push({ t: ln, f, color: it.c, y, rule: it.rule && i === 0 }); y += (it.s + 5) * scale; });
    }
    c.height = Math.min(8192, Math.ceil(y + pad * 2));
    g = c.getContext('2d');
    g.textBaseline = 'top';
    const glow = scale === 1 ? 3 : 3 * Math.sqrt(scale);
    for (const op of ops) {
      if (op.card) {
        const col = op.card === 'critical' ? '#ff7d93' : '#ffd07f';
        g.shadowColor = col; g.shadowBlur = glow;
        g.strokeStyle = col; g.lineWidth = 1.5 * scale;
        g.strokeRect(pad - 2 * scale, op.y, inner + 4 * scale, op.h);
        g.fillStyle = col;
        g.font = op.tf; g.fillText(op.title, pad + 4 * scale, op.y + 4 * scale);
        g.font = op.bf;
        op.body.forEach((ln, i) => g.fillText(ln, pad + 4 * scale, op.y + (19 + i * 15) * scale));
        continue;
      }
      g.font = op.f; g.fillStyle = op.color; g.shadowColor = op.color; g.shadowBlur = glow;
      g.fillText(op.t, pad, op.y);
      if (op.rule) {
        g.shadowBlur = 0;
        g.fillStyle = 'rgba(143,245,230,0.35)';
        g.fillRect(pad + g.measureText(op.t).width + 8 * scale, op.y + 7 * scale, inner, 1 * scale);
      }
    }
    const tex = new THREE.CanvasTexture(c);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = this.renderer.capabilities.getMaxAnisotropy();
    tex.userData.owned = true;
    tex.userData.px = { w: Wpx, h: c.height };
    return tex;
  }

  // Fit a face texture to its face: short content sits still at the top, long content scrolls.
  _fitFace(tex, faceW, faceH) {
    const worldH = tex.userData.px.h * (faceW / tex.userData.px.w);
    const r = faceH / worldH;
    tex.wrapT = r >= 1 ? THREE.ClampToEdgeWrapping : THREE.RepeatWrapping;
    tex.repeat.set(1, r);
    tex.offset.set(0, 1 - r);
    tex.userData.scroll = r < 1;
    tex.needsUpdate = true;
  }

  // Swap a tower's faces to a given resolution (4 for the focused tower).
  _setFaceRes(id, scale) {
    const t = this.towers.get(id);
    if (!t || !t.faces || t.scale === scale) return;
    const tex = this._faceTexture(t.node, scale);
    this._fitFace(tex, t.faceW, t.faceH);
    const old = t.faces[0].material.map;
    for (const f of t.faces) { f.material.map = tex; f.material.needsUpdate = true; }
    if (old) { old.dispose(); this.faceTextures = this.faceTextures.filter((x) => x !== old); }
    this.faceTextures.push(tex);
    t.scale = scale;
  }

  _updateCity(dt, W) {
    const show = W.t > 0.01;
    this.cityGroup.visible = show;
    if (!show) return;
    const occ = this.occluders;
    for (const m of this.cityMats) {
      const dim = m.wid ? (this.nodes.get(m.wid)?.dim ?? 1) : 1;
      const hidden = occ && m.wid && occ.has(m.wid) ? 0.04 : 1;   // towers standing in front of the focused one
      m.mat.opacity = m.base * W.t * hidden * (m.wid ? 0.15 + 0.85 * dim : (this.highlight ? 0.55 : 1));
    }
    const focused = this.focusTowerId ? this.towers.get(this.focusTowerId)?.faces?.[0]?.material.map : null;
    for (const t of this.faceTextures) {
      if (t.userData.scroll) t.offset.y = (t.offset.y + dt * (t === focused ? 0.006 : 0.02)) % 1;
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
      const bits = [`${c.shown} shown`];
      if (c.critical) bits.push(`${c.critical} critical`);
      if (c.warning) bits.push(`${c.warning} warning`);
      if (c.evicted) bits.push(`${c.evicted} evicted`);
      if (c.unscheduled) bits.push(`${c.unscheduled} unscheduled`);
      let name = L.label;
      if (L.key === 'users' && p.internet) {   // internet dependencies share the top plane with the users
        name = 'Users & internet';
        bits[0] = `${c.shown} user group${c.shown === 1 ? '' : 's'} + ${p.internet} internet service${p.internet === 1 ? '' : 's'}`;
      }
      o.div.innerHTML = `<div class="n">${name}</div><div class="m">${bits.join(' · ')}</div>`;
    }
  }

  layerCounts() {
    const out = { users: { shown: 0, warning: 0, critical: 0 }, services: { shown: 0, warning: 0, critical: 0 },
      pods: { shown: 0, warning: 0, critical: 0 }, hosts: { shown: 0, warning: 0, critical: 0 } };
    const bump = (k, lvl) => {
      out[k].shown++;
      if (lvl === 'warning' || lvl === 'critical' || lvl === 'evicted') out[k][lvl] = (out[k][lvl] || 0) + 1;
    };
    for (const n of this.graph?.nodes || []) {
      if (n.kind === 'clients') bump('users', n.risk?.level);
      else if (n.kind === 'host' && n.pseudo) out.hosts.unscheduled = n.podCount;
      else if (n.kind === 'host') bump('hosts', n.risk?.level);
      else bump('services', n.risk?.level);
    }
    for (const p of this.pods) bump('pods', p.level);
    return out;
  }

  // ------------------------------------------------------------------ view / options / selection
  setView(view) {
    if (!VIEWS.includes(view)) view = 'galaxy';   // an unknown view must never zero every layout weight
    this.view = view;
    if (view !== 'gibson') { this.occluders = null; }
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
    let pos, target = new THREE.Vector3(0, 0, 0);
    if (this.view === 'gibson') {
      // Isometric overview of the whole board, fitted into the visible part of the canvas.
      // Measure what the camera actually sees: the board's corners, the internet plane below it and
      // the tower tops, projected onto the screen axes for the iso direction. The board alone is a
      // rectangle, but with the internet plane jutting out in front a centred-box guess pushes it
      // under the rail.
      const city = this.layout?.city;
      const b = city ? city.bounds : { x0: -150, x1: 150, z0: -150, z1: 150 };
      const pts = [[b.x0, 0, b.z0], [b.x1, 0, b.z0], [b.x0, 0, b.z1], [b.x1, 0, b.z1]];
      const I = city?.internet;
      if (I) pts.push([I.x0, I.y, I.z0], [I.x1, I.y, I.z0], [I.x0, I.y, I.z1], [I.x1, I.y, I.z1]);
      for (const [wid, t] of this.towers || []) {
        const p = this.nodes.get(wid)?.gibTarget;
        if (p) pts.push([p.x, t.height + 6, p.z]);
      }
      const fwd = ISO.clone().negate();
      const ax = new THREE.Vector3().crossVectors(fwd, new THREE.Vector3(0, 1, 0)).normalize();
      const ay = new THREE.Vector3().crossVectors(ax, fwd).normalize();
      let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      const v = new THREE.Vector3();
      for (const [x, y, z] of pts) {
        v.set(x, y, z);
        const sx = v.dot(ax), sy = v.dot(ay);
        x0 = Math.min(x0, sx); x1 = Math.max(x1, sx); y0 = Math.min(y0, sy); y1 = Math.max(y1, sy);
      }
      target = new THREE.Vector3().addScaledVector(ax, (x0 + x1) / 2).addScaledVector(ay, (y0 + y1) / 2);
      const vw = this.container.clientWidth || window.innerWidth;
      const { left = 0, right = 0 } = this.insetPx || {};
      const frac = Math.max(0.4, (vw - left - right) / vw);
      const tanV = Math.tan((GIBSON_FOV * Math.PI) / 360);
      // The bottom ~110px of the canvas is under the legend; keep a margin for perspective.
      const dist = 1.1 * Math.max((x1 - x0) / 2 / (tanV * this.camera.aspect * frac), (y1 - y0) / 2 / (tanV * 0.84));
      this._flyTo(target.clone().addScaledVector(ISO, dist), target, instant ? 0 : 1.2);
      return;
    }
    if (this.view === 'strata') pos = new THREE.Vector3(0, sr * 0.72 + 40, sr * 2.5 + 90);
    else if (this.view === 'cascade') {
      // Face the waterfall, slightly from above and the left so the steps read. Fit the whole width
      // into the part of the canvas the rail and drawer leave visible.
      const { w, h } = this.layout ? this.layout.cascadeExtent : { w: 400, h: 200 };
      const vw = this.container.clientWidth || window.innerWidth;
      const { left = 0, right = 0 } = this.insetPx || {};
      const frac = Math.max(0.35, (vw - left - right) / vw);
      const tanV = Math.tan((this.camera.fov * Math.PI) / 360);
      const dist = Math.max((w / 2 + 105) / (tanV * this.camera.aspect * frac), (h / 2 + 80) / tanV, 220);   // room for edge labels
      pos = new THREE.Vector3(-w * 0.08, h * 0.15 + dist * 0.12, dist);
      target = new THREE.Vector3(0, -10, 0);
    } else pos = new THREE.Vector3(0, ext * 1.45, ext * 1.75);
    this._flyTo(pos, target, instant ? 0 : 1.2);
  }

  focus(id) {
    const obj = this.nodes.get(id);
    if (!obj) return;
    const dir = this.camera.position.clone().sub(this.controls.target).normalize();
    const cur = this.camera.position.distanceTo(this.controls.target);
    if (this.view === 'gibson') {
      const t = this.towers.get(id);
      const p = obj.gibTarget;
      const tanH = Math.tan((GIBSON_FOV * Math.PI) / 360);
      if (!t) {   // not a tower (gate, connector, host): approach it at the isometric angle
        const target = new THREE.Vector3(p.x, p.y + 4, p.z);
        this._flyTo(target.clone().addScaledVector(ISO, 46 / tanH), target, 0.9);
        return;
      }
      // Face the tower's front almost head-on, sized so the whole building fits beside the panels.
      const { TOWER } = CITY;
      const h = t.height;
      const target = new THREE.Vector3(p.x, h / 2, p.z + TOWER / 2);
      const vw = this.container.clientWidth || window.innerWidth;
      const { left = 0, right = 0 } = this.insetPx || {};
      const frac = Math.max(0.35, (vw - left - right) / vw);
      // Fit the height, fit the width beside the panels, and never closer than ~100 units, so a
      // one-pod tower isn't blown up past readability (its face is high-res anyway).
      const dist = Math.max((h / 2 + 4) / tanH, (TOWER / 2 + 5) / (tanH * this.camera.aspect * frac), 100);
      const dir = new THREE.Vector3(0.16, 0.1, 1).normalize();
      const camPos = target.clone().addScaledVector(dir, dist);
      // Towers between the camera and the focused one fade out of the way.
      this.occluders = new Set();
      const ax = camPos.x, az = camPos.z, bx = target.x, bz = p.z;
      for (const [wid, other] of this.towers) {
        if (wid === id) continue;
        const o = this.nodes.get(wid)?.gibTarget;
        if (!o) continue;
        const vx = bx - ax, vz = bz - az;
        const tt = Math.max(0, Math.min(1, ((o.x - ax) * vx + (o.z - az) * vz) / (vx * vx + vz * vz)));
        const dx = ax + vx * tt - o.x, dz = az + vz * tt - o.z;
        if (tt < 0.98 && Math.hypot(dx, dz) < TOWER * 1.1) this.occluders.add(wid);
      }
      if (this.focusTowerId && this.focusTowerId !== id) this._setFaceRes(this.focusTowerId, 1);
      this.focusTowerId = id;
      this._setFaceRes(id, 4);
      this._flyTo(camPos, target, 1.0);
      return;
    }
    if (this.view === 'strata') {
      // Keep the whole stack in frame (users -> hosts); just lean toward the node.
      const target = new THREE.Vector3(obj.strTarget.x * 0.35, 0, obj.strTarget.z * 0.35);
      const sr = this.layout ? this.layout.planes.services.r : 120;
      this._flyTo(target.clone().add(dir.multiplyScalar(Math.max(sr * 2.6 + 60, cur))), target, 0.9);
      return;
    }
    const target = obj.pos.clone();
    const dist = Math.max(90, Math.min(220, cur * 0.6));
    this._flyTo(target.clone().add(dir.multiplyScalar(dist)), target, 0.9);
  }

  _flyTo(pos, target, dur) {
    if (!dur) {
      this.camera.position.copy(pos); this.controls.target.copy(target); this.controls.update();
      this.fly = null; return;
    }
    this.fly = { p0: this.camera.position.clone(), p1: pos, t0: this.controls.target.clone(), t1: target, t: 0, dur };
  }

  // sel: null | {type:'node', id} | {type:'edge', id} | {type:'path', nodes, edges} | {type:'pod', key}
  setSelection(sel) {
    this.selection = sel;
    this._applyHighlight();
    // Leaving a focused tower in the city: drop its high-res face and fly back to the overview.
    if (!sel && this.focusTowerId) {
      this._setFaceRes(this.focusTowerId, 1);
      this.focusTowerId = null;
      this.occluders = null;
      if (this.view === 'gibson') this.resetCamera(false);
    }
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
    if (sel.type === 'cascade') {
      // A failure cascade: the root, its blast radius, the links impact travels along, and the stack below.
      const entry = this.cascadeByRoot?.get(sel.root);
      if (!entry) { this.highlight = null; return; }
      const c = entry.cascade;
      if (this.nodes.has(c.root)) nodes.add(c.root);
      for (const id of c.impacted) nodes.add(id);
      if (c.userFacing) for (const [id, o] of this.nodes) if (o.data.kind === 'clients') nodes.add(id);
      for (const l of entry.links) {
        if (l.edge) { edges.add(l.edge.data.id); nodes.add(l.edge.data.source); nodes.add(l.edge.data.target); }
      }
      for (const p of this.pods) if (nodes.has(p.wid)) { podKeys.add(p.key); hosts.add(p.data.hostId || `host:${p.data.node}`); }
      for (const h of hosts) nodes.add(h);
      this.highlight = { nodes, edges, up, down, podKeys, hosts, cascadeRoot: c.root };
      return;
    }
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
      // A node: everything running on it, and who depends on those workloads.
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
    // Light the whole stack under highlighted workloads: their pods and hosts.
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
    el.addEventListener('dblclick', () => { if (!this._pick()) this.resetCamera(false); });
  }

  _pick() {
    this.raycaster.setFromCamera(this.mouse, this.camera);
    const targets = [];
    const strataVis = this.wv.strata + this.wv.cascade + this.wv.gibson > 0.5;   // hosts are drawn there
    const city = this.wv.gibson > 0.5;
    for (const obj of this.nodes.values()) {
      if (obj.data.kind === 'host' && !strataVis) continue;
      if (city && obj.data.kind === 'workload') continue;   // the tower stands in for the star
      targets.push(obj.pick);
    }
    if (city) targets.push(...this.cityPick);
    else if (this.opts.pods) targets.push(this.podPoints);
    const hits = this.raycaster.intersectObjects(targets, false);
    for (const h of hits) {
      if (h.object === this.podPoints) {
        const p = this.pods[h.index];
        if (p) return { type: 'pod', key: p.key, pod: p.data, wid: p.wid };
      } else if (h.object.userData.podKey) {
        const u = h.object.userData;
        return { type: 'pod', key: u.podKey, pod: u.pod, wid: u.wid };
      } else if (h.object.userData.nodeId) {
        return { type: 'node', id: h.object.userData.nodeId };
      }
    }
    return null;
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
    const W = { g: this.wv.galaxy, s: this.wv.strata, c: this.wv.cascade, t: this.wv.gibson };
    this._wt = W.t;
    // The Gibson lens is narrower (flatter, near-isometric); ease the field of view with the view.
    const fov = 45 - (45 - GIBSON_FOV) * W.t;
    if (Math.abs(fov - this.camera.fov) > 0.01) { this.camera.fov = fov; this._applyShift(); this._updateScale(); }
    const k = 1 - Math.exp(-dt * 3.2);

    if (this.fly) {
      this.fly.t += dt / this.fly.dur;
      const t = ease(Math.min(1, this.fly.t));
      this.camera.position.lerpVectors(this.fly.p0, this.fly.p1, t);
      this.controls.target.lerpVectors(this.fly.t0, this.fly.t1, t);
      if (this.fly.t >= 1) this.fly = null;
    }
    // Drifting a wall of columns is disorienting; only the galaxy and strata drift.
    this.controls.autoRotate = this.opts.rotate && !this.fly && this.time - this.lastInteraction > 8 && !this.selection
      && this.view !== 'cascade' && this.view !== 'gibson';
    this.controls.update();
    if (this.insetTarget != null && Math.abs(this.insetTarget - this.insetShift) > 0.5) {
      this.insetShift += (this.insetTarget - this.insetShift) * Math.min(1, dt * 6);
      this._applyShift();
    }
    if (!this.graph) {
      this.renderer.render(this.scene, this.camera);
      return;
    }
    this._updateCity(dt, W);

    const hi = this.highlight;
    let moving = this.positionsDirty || changing;
    this.positionsDirty = false;
    const hostVis = W.s + W.c + W.t;

    // Nodes.
    const before = new THREE.Vector3();
    for (const obj of this.nodes.values()) {
      before.copy(obj.pos);
      obj.gal.lerp(obj.galTarget, k);
      obj.str.lerp(obj.strTarget, k);
      obj.cas.lerp(obj.casTarget, k);
      obj.gib.lerp(obj.gibTarget, k);
      obj.pos.set(0, 0, 0).addScaledVector(obj.gal, W.g).addScaledVector(obj.str, W.s).addScaledVector(obj.cas, W.c)
        .addScaledVector(obj.gib, W.t);
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
        if (obj.gauge) {
          obj.gauge.material.opacity = 0.45 * hostVis * obj.dim;
          obj.usage.material.opacity = 0.9 * hostVis * obj.dim;
        }
        obj.group.visible = hostVis > 0.01;
      } else {
        const asTower = n.kind === 'workload' ? 1 - W.t : 1;   // in the city the tower replaces the star
        obj.glow.material.opacity = (n.kind === 'clients' ? 0.5 : 0.55) * obj.dim * ghost * (n.idle ? 0.4 : 1) * asTower;
        obj.glow.scale.set(obj.size * 2.1 * pulse, obj.size * 2.1 * pulse, 1);
        obj.core.material.opacity = 0.95 * obj.dim * ghost * asTower;
        if (n.kind === 'workload') {
          const top = (this.towers.get(n.id)?.height || 0) + 4;
          obj.label.position.y = obj.labelY + (top - obj.labelY) * W.t;
        }
        if (obj.halo.visible) {
          // In the cascade view the impact halo is the point: make it louder. (The city has no halos.)
          const hs = obj.size * 1.35 * (1 + 0.35 * W.c * (1 + 0.15 * Math.sin(this.time * 3)));
          obj.halo.scale.set(hs, hs, 1);
          obj.halo.material.opacity = (0.75 + 0.25 * W.c) * obj.dim * (1 - W.t);
          obj.halo.material.rotation += dt * (0.4 + 0.8 * W.c);
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
    this.columnsGroup.visible = W.c > 0.01;
    for (const o of this.columnObjs || []) {
      o.pane.material.opacity = 0.05 * W.c;
      o.rim.material.opacity = 0.35 * W.c;
      o.label.element.style.opacity = W.c > 0.6 ? (W.c - 0.6) / 0.4 : 0;
    }

    // Pods: orbit their star in galaxy and cascade, settle onto the pods plane in strata.
    const tmp = new THREE.Vector3();
    const showPods = this.opts.pods;
    for (let i = 0; i < this.pods.length; i++) {
      const p = this.pods[i];
      const host = this.nodes.get(p.wid);
      if (!host) continue;
      p.str.lerp(p.strTarget, k);
      const r = (host.size * 0.62 + 2.5 + (p.i % 3) * 1.6) * (1 - 0.3 * W.c);
      const a = p.phase + this.time * p.speed + (p.i / p.count) * Math.PI * 2;
      tmp.set(Math.cos(a) * r + host.pos.x, Math.sin(a) * r * p.incl * 0.6 + host.pos.y, Math.sin(a) * r + host.pos.z);
      p.pos.lerpVectors(tmp, p.str, W.s);
      this.podPos[i * 3] = p.pos.x; this.podPos[i * 3 + 1] = p.pos.y; this.podPos[i * 3 + 2] = p.pos.z;
      const inHi = !hi || hi.podKeys.has(p.key);
      const faded = (host.data.ghost ? 0.5 : 1) * (p.level === 'evicted' ? 0.55 : 1);
      this.podCol[i * 4 + 3] = showPods ? (inHi ? 0.95 : 0.1) * faded * (1 - W.t) : 0;
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
    this._updateCascade(W);
    this._updateParticles(dt, W);

    // Selection ring.
    const ringId = this.selection?.type === 'node' ? this.selection.id
      : this.selection?.type === 'cascade' && this.nodes.has(this.selection.root) ? this.selection.root : null;
    const selObj = ringId ? this.nodes.get(ringId) : null;
    if (selObj) {
      this.selRing.position.copy(selObj.pos);
      const s = selObj.size * 1.7 * (1 + 0.04 * Math.sin(this.time * 3));
      this.selRing.scale.set(s, s, 1);
      this.selRing.material.opacity = 0.9 * (1 - W.t);   // the city shows selection by dimming the rest
      this.selRing.material.rotation -= dt * 0.5;
    } else this.selRing.material.opacity = 0;

    if (this.hoverDirty) {
      this.hoverDirty = false;
      const hit = this.mouse.x < -2 ? null : this._pick();
      this.renderer.domElement.style.cursor = hit ? 'pointer' : 'grab';
      this.cb.onHover?.(hit, this.mouseClient);
    }
    if (!this._lblT || this.time - this._lblT > 0.2) { this._lblT = this.time; this._updateLabels(W); }

    if (this.composer && W.t > 0.02) {
      // Less bloom while reading a focused tower, so the glow doesn't blur its text.
      this.bloom.strength = 0.6 * W.t * (this.focusTowerId ? 0.3 : 1);
      this.composer.render();
    } else {
      this.renderer.render(this.scene, this.camera);
    }
    this.labelRenderer.render(this.scene, this.camera);
  }

  _edgeColor(e, t, out) {
    // Calls leaving the cluster fade from cyan into the colour of where they go (LAN / internet);
    // warning and critical still win, so a failing external call is never hidden by its scope colour.
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
      // Galaxy: gentle arcs above the disc. Strata: tall arches over the services plane.
      // Cascade: bow toward the viewer, more for calls that go back up the waterfall.
      const bothOnPlane = Math.abs(sa.strTarget.y - sb.strTarget.y) < 8;
      const backward = sb.casTarget.x <= sa.casTarget.x;
      e.p1.y += W.g * dist * 0.16 + W.s * (dist * (0.16 + (bothOnPlane ? 0.32 : 0.05)) + 4) + W.c * dist * 0.04;
      e.p1.z += W.c * (dist * (backward ? 0.55 : 0.18) + 6);
      e.len = Math.max(1, dist * 1.15) * (1 - W.t) + (e.routeLen || dist) * W.t;
      const visible = this.opts.inferred || !e.inferredOnly;
      const inHi = !hi || hi.edges.has(e.data.id);
      const base = e.inferredOnly ? 0.22 : e.idle ? 0.13 : 0.18 + 0.35 * e.level + (e.lvl !== 'ok' ? 0.25 : 0);
      const alpha = visible ? base * (inHi ? (hi ? 1.6 : 1) : 0.05) : 0;
      for (let s = 0; s < SEG; s++) {
        for (const tt of [s / SEG, (s + 1) / SEG]) {
          curvePoint(e, tt, v, W.t);
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

  // Host -> workload impact links and root-cause beacons (cascade view).
  _updateCascade(W) {
    const show = W.c + W.t > 0.01;
    this.beacons.visible = show;
    this.casLines.visible = show;
    const wc = W.c + W.t;
    if (!show) return;
    const hi = this.highlight;
    const focusRoot = hi?.cascadeRoot;
    for (const b of this.beaconObjs || []) {
      const obj = this.nodes.get(b.anchor);
      b.sprite.position.copy(obj.pos);
      // While reading a focused tower, pillars step back (a root's own pillar would cross its text).
      const on = (!focusRoot || focusRoot === b.root) && !this.focusTowerId;
      const h = 60 + 18 * Math.min(4, b.weight);
      b.sprite.scale.set(9 + 2 * Math.sin(this.time * 2.2 + b.weight), h, 1);
      b.sprite.material.opacity = wc * (on ? 0.75 + 0.2 * Math.sin(this.time * 2.2) : 0.08);
    }
    let vi = 0;
    const v = new THREE.Vector3();
    for (const l of this.cascadeLinks) {
      if (l.edge) continue;
      const a = this.nodes.get(l.from).pos, b = this.nodes.get(l.to).pos;
      l.p0.copy(a); l.p2.copy(b);
      l.p1.copy(a).add(b).multiplyScalar(0.5);
      l.p1.z += a.distanceTo(b) * 0.25 + 8;
      l.len = Math.max(1, a.distanceTo(b) * 1.1);
      const on = !focusRoot || focusRoot === l.root;
      const c = l.level === 'critical' ? C.crit : C.warn;
      for (let s = 0; s < SEG; s++) {
        if (s % 2) continue;   // dashed: an inferred dependency (runs-on), not a call
        for (const tt of [s / SEG, (s + 1) / SEG]) {
          bezier(l.p0, l.p1, l.p2, tt, v);
          this.casPos[vi * 3] = v.x; this.casPos[vi * 3 + 1] = v.y; this.casPos[vi * 3 + 2] = v.z;
          this.casCol[vi * 4] = c.r; this.casCol[vi * 4 + 1] = c.g; this.casCol[vi * 4 + 2] = c.b;
          this.casCol[vi * 4 + 3] = wc * (on ? 0.55 : 0.04);
          vi++;
        }
      }
    }
    this.casGeo.setDrawRange(0, vi);
    this.casGeo.attributes.position.needsUpdate = true;
    this.casGeo.attributes.color.needsUpdate = true;
  }

  _updateDrops(ws) {
    const hi = this.highlight;
    let vi = 0;
    const show = this.opts.pods;
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
    if (show) {
      for (const p of this.pods) {
        const w = this.nodes.get(p.wid);
        const h = this.nodes.get(p.data.hostId || `host:${p.data.node}`);
        const inHi = !hi || hi.podKeys.has(p.key);
        const alpha = ws * (inHi ? (hi ? 0.9 : 0.4) : 0.04) * (p.level === 'evicted' ? 0.4 : 1);
        if (w) push(w.pos, p.pos, C.violet, alpha);
        if (h) push(p.pos, h.pos, h.data.pseudo ? C.crit : C.up, alpha * (h.data.pseudo ? 1.4 : 0.8));
      }
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
        curvePoint(curve, tt, v, this._wt);
        this.pPos[pi * 3] = v.x; this.pPos[pi * 3 + 1] = v.y; this.pPos[pi * 3 + 2] = v.z;
        const c = color || this._edgeColor(curve, tt, col);
        const fall = 1 - j / TAIL;
        this.pCol[pi * 4] = c.r; this.pCol[pi * 4 + 1] = c.g; this.pCol[pi * 4 + 2] = c.b;
        this.pCol[pi * 4 + 3] = alpha * fall;
        this.pSize[pi] = size * (0.45 + 0.55 * fall);
        pi++;
      }
    };
    // Request comets, in request direction. In the cascade view they dim so the failure pulses read.
    const reqAlpha = 1 - 0.55 * W.c - 0.15 * W.t;
    const beads = 1 - 0.45 * W.t;   // in the city, pulses are small beads on the traces
    for (const e of this.edges) {
      if (!this.opts.inferred && e.inferredOnly) continue;
      const inHi = !hi || hi.edges.has(e.data.id);
      if (hi && !inHi) continue;
      const quiet = e.inferredOnly || e.idle;
      const speed = (14 + 46 * e.level) * e.slow * (quiet ? 0.35 : 1);
      const step = (speed * dt) / e.len;
      const size = (quiet ? 2.4 : 3.2 + 3 * e.level) * beads;
      const alpha = (quiet ? 0.35 : 0.95) * reqAlpha;
      for (let i = 0; i < e.phases.length; i++) {
        e.phases[i] = (e.phases[i] + step) % 1;
        emit(e, e.phases[i], size, alpha, null, 0.016 * (e.inferredOnly ? 0.5 : 1));
        if (this.opts.responses && !e.inferredOnly && i % 2 === 0) emit(e, 1 - ((e.phases[i] + 0.37) % 1), size * 0.6, 0.45 * reqAlpha, C.white);
      }
    }
    // Failure pulses travel *against* the requests: from the root cause back to everyone it slows down.
    if (W.c + W.t > 0.01) {
      const focusRoot = hi?.cascadeRoot;
      for (const l of this.cascadeLinks) {
        if (focusRoot && focusRoot !== l.root) continue;
        const curve = l.edge || l;
        const color = l.level === 'critical' ? C.crit : C.warn;
        const step = (26 * dt) / (curve.len || 1);
        for (let i = 0; i < l.phases.length; i++) {
          l.phases[i] = (l.phases[i] + step) % 1;
          // Edge curves run caller -> callee, so walk them backwards; host links already run host -> workload.
          const t = l.edge ? 1 - l.phases[i] : l.phases[i];
          emit(curve, t, 7.5, 0.95 * (W.c + W.t), color, l.edge ? -0.022 : 0.022);
        }
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
    // Plane, column, district and arm captions are fixed furniture: node labels keep clear of them.
    const placed = [];
    for (const el of this.labelRenderer.domElement.querySelectorAll('.lbl.layer, .lbl.district, .lbl.ns')) {
      if (el.style.display === 'none' || el.style.opacity === '0') continue;
      const r = el.getBoundingClientRect();
      if (r.width && r.height) placed.push({ x0: r.left, x1: r.right, y0: r.top, y1: r.bottom });
    }
    const base = this.labelRenderer.domElement.getBoundingClientRect();
    for (const p of placed) { p.x0 -= base.left; p.x1 -= base.left; p.y0 -= base.top; p.y1 -= base.top; }
    items.sort((a, b) => b.prio - a.prio);
    const shown = [];
    const v2 = this._lblV2 || (this._lblV2 = new THREE.Vector3());
    const city = this.wv.gibson > 0.5;                 // tower labels sit on the roof: there is no "below"
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
      const above = at(y), below = city ? null : at(y + drop);
      const box = !hit(above, placed) ? above : below && !hit(below, placed) ? below : null;
      el.classList.toggle('hidden', !box);
      el.classList.add('compact');
      el.style.marginTop = box && box === below ? `${Math.round(drop)}px` : '';
      if (!box) continue;
      placed.push(box);
      if (meta && !it.compact && box === above) shown.push({ el, box, x, y, nw, mw: Math.max(nw, meta.textContent.trim().length * 5.6) });
    }
    for (const s of shown) {      // second pass: the metrics line, where nothing else needs the room
      // With its metrics the label grows to two lines around the same centre: name up, metrics under it.
      const name = { x0: s.x - s.nw / 2 - 2, x1: s.x + s.nw / 2 + 2, y0: s.y - 17, y1: s.y + 1 };
      const line = { x0: s.x - s.mw / 2, x1: s.x + s.mw / 2, y0: s.y + 1, y1: s.y + 15 };
      if (hit(name, placed, s.box) || hit(line, placed, s.box)) continue;
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
      if (n.kind === 'host') show = show && W.s + W.c + W.t > 0.5;
      else if (show && many && !hi && W.c < 0.5) {
        const near = camPos.distanceTo(obj.pos) < 320;
        show = near || n.kind === 'clients' || n.kind === 'entry' || (n.risk && n.risk.level !== 'ok');
      }
      if (this.occluders && this.occluders.has(n.id) && W.t > 0.5) show = false;   // faded out of the way
      const dim = hi && !hi.nodes.has(n.id);
      // Strata packs services on one ring: keep only names unless the node is in focus.
      const compact = (W.s > 0.5 || W.t > 0.5) && this.nodes.size > 14 && !(hi && hi.nodes.has(n.id)) && n.kind !== 'host';
      el.classList.toggle('dim', !!dim && show);
      if (!show) { el.classList.add('hidden'); continue; }
      // What must stay readable when labels collide: the selection, then trouble, then the landmarks.
      const lvl = n.risk?.level;
      const prio = (hi && hi.nodes.has(n.id) ? 100 : 0) + (lvl === 'critical' ? 60 : lvl === 'warning' ? 45 : 0)
        + (n.kind === 'clients' ? (n.scope === 'lan' ? 40 : 58) : n.kind === 'entry' ? 30 : n.kind === 'workload' ? 20 + Math.min(10, n.pods.length)
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
        if (m.map && m.map.userData.owned) m.map.dispose();   // per-object canvas textures (tower faces)
        m.dispose();
      });
    }
  });
}

function clearGroup(group) {
  for (const child of [...group.children]) disposeTree(child);
  group.clear();
}

// Call routes: Gibson traces are polylines; other views use a quadratic curve. An edge's point at t
// is the curve blended into the route by the Gibson view weight, so traces morph between views.
function routeOf(points) {
  if (!points || points.length < 2) return { route: null };
  const route = points.map((p) => new THREE.Vector3(p.x, p.y, p.z));
  const routeCum = [0];
  for (let i = 1; i < route.length; i++) routeCum.push(routeCum[i - 1] + route[i].distanceTo(route[i - 1]));
  return { route, routeCum, routeLen: routeCum[routeCum.length - 1] };
}

const _routeTmp = new THREE.Vector3();
function curvePoint(c, t, out, wt) {
  bezier(c.p0, c.p1, c.p2, t, out);
  if (wt > 0.001 && c.route) out.lerp(polyAt(c, t, _routeTmp), wt);
  return out;
}

function polyAt(c, t, out) {
  const d = Math.max(0, Math.min(1, t)) * c.routeLen;
  let i = 1;
  while (i < c.routeCum.length - 1 && c.routeCum[i] < d) i++;
  const seg = c.routeCum[i] - c.routeCum[i - 1] || 1;
  return out.lerpVectors(c.route[i - 1], c.route[i], (d - c.routeCum[i - 1]) / seg);
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
