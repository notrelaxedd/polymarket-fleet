// Shared pieces of the Data city scene: colours, textures, labels and the View/Builder
// types the stage (../Scene.tsx) drives.
import * as THREE from 'three';
import { CSS2DObject } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import { statusOf } from '../fleet';
import type { FleetNode } from '../fleet';

export const COL = {
  ok: new THREE.Color(0x45c4f5),
  hot: new THREE.Color(0xffa733),
  idle: new THREE.Color(0x2b7fa6),
  off: new THREE.Color(0x4a5a66),
};
export const colorOf = (n: FleetNode) => COL[statusOf(n)];
export const TAU = Math.PI * 2;
export const WHITE = new THREE.Color(0xffffff);
export const frac = (x: number) => x - Math.floor(x);

/* ---------- text helpers (labels never show a dash for missing data) ---------- */
export const pctText = (n: FleetNode, v: number) => (n.online ? `${Math.round(v)}%` : 'n/a');

/* ---------- shared textures / materials ---------- */
function radialTex(stops: [number, string][]) {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d')!;
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  stops.forEach(([o, col]) => grad.addColorStop(o, col));
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  return new THREE.CanvasTexture(c);
}
function ringTexture() {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d')!;
  g.strokeStyle = '#fff';
  g.lineWidth = 3;
  g.setLineDash([18, 10]);
  g.beginPath();
  g.arc(64, 64, 56, 0, TAU);
  g.stroke();
  return new THREE.CanvasTexture(c);
}
export let GLOW: THREE.Texture;
export let RING: THREE.Texture;
/** Create the shared textures (needs a document; called once per renderer). */
export function initTextures() {
  GLOW?.dispose();
  RING?.dispose();
  GLOW = radialTex([[0, 'rgba(255,255,255,1)'], [0.25, 'rgba(255,255,255,0.45)'], [1, 'rgba(255,255,255,0)']]);
  RING = ringTexture();
}
export function makeLabel(cls = 'lbl') {
  const el = document.createElement('div');
  el.className = cls;
  return new CSS2DObject(el);
}
export function setText(o: CSS2DObject, text: string, sel = false) {
  if (o.element.textContent !== text) o.element.textContent = text;
  o.element.classList.toggle('is-sel', sel);
}

export function sprite(color: THREE.ColorRepresentation, scale: number, opacity = 1) {
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: GLOW, color, transparent: true, opacity, blending: THREE.AdditiveBlending, depthWrite: false }));
  s.scale.setScalar(scale);
  return s;
}
/* ---------- views ---------- */
export interface View {
  group: THREE.Group;
  pick: THREE.Object3D[];
  /** Camera position (world). */
  cam: [number, number, number];
  target?: [number, number, number];
  spin?: number;
  maxPolar?: number;
  /** Called every frame. `nodes` may be longer or shorter than at build time for one
   * frame before a rebuild; implementations skip indexes they did not build. */
  update(t: number, dt: number, nodes: FleetNode[], sel: string | null): void;
}
export type Builder = (nodes: FleetNode[], renderer: THREE.WebGLRenderer) => View;

