// Copyright (C) 2026 Troy Scafuro
// SPDX-License-Identifier: AGPL-3.0-only

// Procedural canvas textures (no image assets, so the UI works air-gapped).
import * as THREE from 'three';

const cache = new Map();

function make(key, size, draw, tile = false) {
  if (cache.has(key)) return cache.get(key);
  const c = document.createElement('canvas');
  c.width = c.height = size;
  const g = c.getContext('2d');
  draw(g, size);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  if (tile) tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  cache.set(key, tex);
  return tex;
}

// Soft glow halo.
export const glow = () => make('glow', 128, (g, s) => {
  const r = s / 2;
  const grd = g.createRadialGradient(r, r, 0, r, r, r);
  grd.addColorStop(0, 'rgba(255,255,255,1)');
  grd.addColorStop(0.12, 'rgba(255,255,255,0.85)');
  grd.addColorStop(0.3, 'rgba(255,255,255,0.32)');
  grd.addColorStop(0.6, 'rgba(255,255,255,0.08)');
  grd.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grd;
  g.fillRect(0, 0, s, s);
});

// Bright star core with a small diffraction cross.
export const star = () => make('star', 128, (g, s) => {
  const r = s / 2;
  const grd = g.createRadialGradient(r, r, 0, r, r, r * 0.5);
  grd.addColorStop(0, 'rgba(255,255,255,1)');
  grd.addColorStop(0.35, 'rgba(255,255,255,0.9)');
  grd.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grd;
  g.beginPath(); g.arc(r, r, r * 0.5, 0, Math.PI * 2); g.fill();
  g.globalCompositeOperation = 'lighter';
  for (const [w, h] of [[s, 2.2], [2.2, s]]) {
    const lg = g.createLinearGradient(w > h ? 0 : r, w > h ? r : 0, w > h ? s : r, w > h ? r : s);
    lg.addColorStop(0, 'rgba(255,255,255,0)'); lg.addColorStop(0.5, 'rgba(255,255,255,0.55)'); lg.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = lg;
    g.fillRect(r - w / 2, r - h / 2, w, h);
  }
});

export const ring = () => make('ring', 128, (g, s) => {
  const r = s / 2;
  g.strokeStyle = 'rgba(255,255,255,0.95)';
  g.lineWidth = s * 0.035;
  g.beginPath(); g.arc(r, r, r * 0.82, 0, Math.PI * 2); g.stroke();
  g.strokeStyle = 'rgba(255,255,255,0.25)';
  g.lineWidth = s * 0.1;
  g.beginPath(); g.arc(r, r, r * 0.82, 0, Math.PI * 2); g.stroke();
});

export const dashedRing = () => make('dashedRing', 128, (g, s) => {
  const r = s / 2;
  g.strokeStyle = 'rgba(255,255,255,0.9)';
  g.lineWidth = s * 0.03;
  g.setLineDash([s * 0.06, s * 0.05]);
  g.beginPath(); g.arc(r, r, r * 0.86, 0, Math.PI * 2); g.stroke();
});

export const diamond = () => make('diamond', 128, (g, s) => {
  const r = s / 2, d = r * 0.6;
  const grd = g.createRadialGradient(r, r, 0, r, r, r);
  grd.addColorStop(0, 'rgba(255,255,255,0.5)'); grd.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grd; g.fillRect(0, 0, s, s);
  g.beginPath(); g.moveTo(r, r - d); g.lineTo(r + d, r); g.lineTo(r, r + d); g.lineTo(r - d, r); g.closePath();
  g.fillStyle = 'rgba(255,255,255,0.35)'; g.fill();
  g.lineWidth = s * 0.04; g.strokeStyle = 'rgba(255,255,255,1)'; g.stroke();
});

function hexPath(g, cx, cy, rad) {
  g.beginPath();
  for (let i = 0; i < 6; i++) {
    const a = Math.PI / 6 + i * Math.PI / 3;
    const x = cx + rad * Math.cos(a), y = cy + rad * Math.sin(a);
    i ? g.lineTo(x, y) : g.moveTo(x, y);
  }
  g.closePath();
}

export const hexOutline = () => make('hexOutline', 128, (g, s) => {
  const r = s / 2;
  hexPath(g, r, r, r * 0.62);
  g.fillStyle = 'rgba(255,255,255,0.12)'; g.fill();
  g.lineWidth = s * 0.04; g.strokeStyle = 'rgba(255,255,255,0.95)'; g.stroke();
  hexPath(g, r, r, r * 0.3);
  g.fillStyle = 'rgba(255,255,255,0.6)'; g.fill();
});

// "End users": a cloud of overlapping circles with a halo, like a cluster of clients.
export const cloud = () => make('cloud', 256, (g, s) => {
  const r = s / 2;
  const grd = g.createRadialGradient(r, r, 0, r, r, r);
  grd.addColorStop(0, 'rgba(255,255,255,0.45)'); grd.addColorStop(0.5, 'rgba(255,255,255,0.12)'); grd.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grd; g.fillRect(0, 0, s, s);
  const blobs = [[0, 0.06, 0.2], [-0.2, 0.1, 0.15], [0.21, 0.1, 0.15], [-0.1, -0.08, 0.15], [0.11, -0.09, 0.16]];
  g.lineWidth = s * 0.016;
  for (const [x, y, rad] of blobs) {
    g.beginPath(); g.arc(r + x * s, r + y * s, rad * s, 0, Math.PI * 2);
    g.fillStyle = 'rgba(255,255,255,0.16)'; g.fill();
    g.strokeStyle = 'rgba(255,255,255,0.95)'; g.stroke();
  }
  for (const rr of [0.36, 0.44]) {
    g.beginPath(); g.arc(r, r, rr * s, 0, Math.PI * 2);
    g.strokeStyle = `rgba(255,255,255,${rr < 0.4 ? 0.5 : 0.25})`; g.lineWidth = s * 0.01; g.stroke();
  }
});

// Translucent plane fill: bright rim, soft centre.
export const planeFill = () => make('planeFill', 256, (g, s) => {
  const r = s / 2;
  const grd = g.createRadialGradient(r, r, 0, r, r, r);
  grd.addColorStop(0, 'rgba(255,255,255,0.35)');
  grd.addColorStop(0.75, 'rgba(255,255,255,0.55)');
  grd.addColorStop(0.97, 'rgba(255,255,255,0.95)');
  grd.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grd;
  g.beginPath(); g.arc(r, r, r, 0, Math.PI * 2); g.fill();
});

// Circuit-board ground for the city view: orthogonal traces with 45° jogs, pads and vias.
// Drawn in white (tinted by the material) and tileable.
export const circuitBoard = () => make('circuitBoard', 1024, (g, s) => {
  let seed = 1337;
  const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
  g.lineCap = 'square';
  for (let i = 0; i < 150; i++) {
    let x = Math.floor(rnd() * 32) * 32, y = Math.floor(rnd() * 32) * 32;
    const lanes = 1 + Math.floor(rnd() * 3);
    g.strokeStyle = `rgba(255,255,255,${0.25 + rnd() * 0.45})`;
    g.lineWidth = rnd() < 0.25 ? 3 : 1.6;
    for (let lane = 0; lane < lanes; lane++) {
      let cx = x + lane * 7, cy = y + lane * 7;
      g.beginPath(); g.moveTo(cx, cy);
      for (let seg = 0; seg < 3 + Math.floor(rnd() * 4); seg++) {
        const len = 32 + Math.floor(rnd() * 5) * 32;
        const dir = Math.floor(rnd() * 4);
        const jog = 10;
        if (dir === 0) { cx += len; g.lineTo(cx, cy); g.lineTo(cx + jog, cy + jog); cx += jog; cy += jog; }
        else if (dir === 1) { cy += len; g.lineTo(cx, cy); }
        else if (dir === 2) { cx -= len; g.lineTo(cx, cy); }
        else { cy -= len; g.lineTo(cx, cy); g.lineTo(cx - jog, cy - jog); cx -= jog; cy -= jog; }
      }
      g.stroke();
      g.fillStyle = 'rgba(255,255,255,0.7)';
      g.fillRect(cx - 3, cy - 3, 6, 6);            // pad at the end of the trace
    }
  }
  for (let i = 0; i < 70; i++) {                    // vias
    g.strokeStyle = 'rgba(255,255,255,0.5)'; g.lineWidth = 1.5;
    g.beginPath(); g.arc(rnd() * s, rnd() * s, 2.5 + rnd() * 2, 0, Math.PI * 2); g.stroke();
  }
}, true);

// Vertical light pillar for root-cause beacons (anchored at the bottom).
export const beam = () => make('beam', 128, (g, s) => {
  const lg = g.createLinearGradient(0, s, 0, 0);
  lg.addColorStop(0, 'rgba(255,255,255,0.95)');
  lg.addColorStop(0.25, 'rgba(255,255,255,0.45)');
  lg.addColorStop(1, 'rgba(255,255,255,0)');
  const hg = g.createLinearGradient(0, 0, s, 0);
  hg.addColorStop(0, 'rgba(0,0,0,1)'); hg.addColorStop(0.5, 'rgba(0,0,0,0)'); hg.addColorStop(1, 'rgba(0,0,0,1)');
  g.fillStyle = lg; g.fillRect(0, 0, s, s);
  g.globalCompositeOperation = 'destination-out';
  g.fillStyle = hg; g.fillRect(0, 0, s, s);
});

export const nebula = () => make('nebula', 256, (g, s) => {
  const r = s / 2;
  const grd = g.createRadialGradient(r, r, 0, r, r, r);
  grd.addColorStop(0, 'rgba(255,255,255,0.55)');
  grd.addColorStop(0.35, 'rgba(255,255,255,0.18)');
  grd.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grd; g.fillRect(0, 0, s, s);
});
