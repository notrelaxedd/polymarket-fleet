// Shared pieces of the seven 3D views: colours, textures, materials, labels, the orb
// used by most views, the View/Builder types and a grid layout helper.
import * as THREE from 'three';
import { CSS2DObject } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import { statusOf } from '../fleet';
import type { FleetNode, Role } from '../fleet';

export type ViewId = 'solar' | 'constellation' | 'core' | 'liquid' | 'rack' | 'city' | 'globe';

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
export const tempText = (n: FleetNode) => (n.temp === null ? 'n/a' : `${Math.round(n.temp)}°`);
export const pctText = (n: FleetNode, v: number) => (n.online ? `${Math.round(v)}%` : 'n/a');
/** Temperature for effects; a machine that reports none counts as mild. */
export const tempOr = (n: FleetNode, dflt = 50) => n.temp ?? dflt;

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
export const SPHERE = new THREE.SphereGeometry(1, 40, 28);

export function fresnelMat(color: number, power = 2.6, intensity = 1.2) {
  return new THREE.ShaderMaterial({
    uniforms: { uColor: { value: new THREE.Color(color) }, uPow: { value: power }, uInt: { value: intensity } },
    vertexShader: `varying vec3 vN; varying vec3 vV;
      void main(){ vec4 mv = modelViewMatrix*vec4(position,1.); vN = normalize(normalMatrix*normal); vV = normalize(-mv.xyz); gl_Position = projectionMatrix*mv; }`,
    fragmentShader: `uniform vec3 uColor; uniform float uPow; uniform float uInt; varying vec3 vN; varying vec3 vV;
      void main(){ float f = pow(1.-abs(dot(normalize(vN), normalize(vV))), uPow); gl_FragColor = vec4(uColor*f*uInt, f); }`,
    transparent: true, blending: THREE.AdditiveBlending, depthWrite: false,
  });
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
export const tag = (n: FleetNode) => (n.online ? `${n.name} · ${tempText(n)}` : `${n.name} · off`);

export function sprite(color: THREE.ColorRepresentation, scale: number, opacity = 1) {
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: GLOW, color, transparent: true, opacity, blending: THREE.AdditiveBlending, depthWrite: false }));
  s.scale.setScalar(scale);
  return s;
}
export function ringSprite(color: THREE.ColorRepresentation = 0xffffff, depthTest = false) {
  return new THREE.Sprite(new THREE.SpriteMaterial({ map: RING, color, transparent: true, depthTest, depthWrite: false }));
}

export function circleLine(r: number, color: number, opacity = 1) {
  const pts: THREE.Vector3[] = [];
  for (let i = 0; i <= 160; i++) pts.push(new THREE.Vector3(Math.cos((i / 160) * TAU) * r, 0, Math.sin((i / 160) * TAU) * r));
  return new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color, transparent: true, opacity }));
}

/* ---------- orb (solar, constellation, core, globe) ---------- */
export interface Orb { g: THREE.Group; mesh: THREE.Mesh; mat: THREE.MeshStandardMaterial; halo: THREE.Sprite; ring: THREE.Sprite; lab: CSS2DObject; r: number; h: number }

export function makeOrb(n: FleetNode, r: number, h: number): Orb {
  const g = new THREE.Group();
  const mat = new THREE.MeshStandardMaterial({ color: 0x061018, emissive: colorOf(n).clone(), emissiveIntensity: 1, roughness: 0.35, metalness: 0.2 });
  const mesh = new THREE.Mesh(SPHERE, mat);
  mesh.scale.setScalar(r);
  mesh.userData.id = n.id;
  const shell = new THREE.Mesh(SPHERE, fresnelMat(0xffffff, 3, 0.55));
  shell.scale.setScalar(r * 1.12);
  const halo = sprite(colorOf(n), r * 4.6, 0.4);
  const ring = ringSprite();
  ring.scale.setScalar(r * 3.6);
  ring.visible = false;
  const lab = makeLabel();
  lab.position.set(0, -r - 0.75, 0);
  g.add(mesh, shell, halo, ring, lab);
  return { g, mesh, mat, halo, ring, lab, r, h };
}

export function tintOrb(o: Orb, n: FleetNode, t: number, sel: string | null) {
  const st = statusOf(n);
  const load = n.online ? n.cpu / 100 : 0;
  o.mat.emissive.lerp(COL[st], 0.08);
  (o.halo.material as THREE.SpriteMaterial).color.copy(o.mat.emissive);
  o.mat.emissiveIntensity = st === 'off' ? 0.1 : st === 'idle' ? 0.32 : 0.45 + load * 0.75;
  (o.halo.material as THREE.SpriteMaterial).opacity = st === 'off' ? 0 : st === 'idle' ? 0.1 : 0.16 + load * 0.3;
  const pulse = n.online ? 1 + 0.05 * Math.sin(t * (2 + load * 6) + o.h * 9) : 1;
  o.mesh.scale.setScalar(o.r * pulse);
  o.ring.visible = sel === n.id;
  (o.ring.material as THREE.SpriteMaterial).rotation = t * 0.8;
  setText(o.lab, tag(n), sel === n.id);
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
export type Builder = (nodes: FleetNode[], renderer: THREE.WebGLRenderer, roles: Role[]) => View;

/** Index of a role in the list, or -1. */
export const roleIndex = (roles: Role[], job: string) => roles.findIndex((r) => r.id === job);

/** Grid layout for n items: cols = min(6, ceil(sqrt(n))), centred on the origin.
 * Returns [col, row] offsets in cells from the centre (row grows downwards). */
export function gridCells(n: number, maxCols = 6) {
  const cols = Math.max(1, Math.min(maxCols, Math.ceil(Math.sqrt(n))));
  const rows = Math.max(1, Math.ceil(n / cols));
  const cells = Array.from({ length: n }, (_, k) => {
    const r = Math.floor(k / cols);
    // centre a short last row
    const inRow = r === rows - 1 ? n - r * cols : cols;
    return [(k % cols) - (inRow - 1) / 2, r - (rows - 1) / 2] as const;
  });
  return { cols, rows, cells };
}

/** Distance at which a box of the given width/height fits a 42 degree camera. */
export function fitDistance(w: number, h: number, margin = 1.25) {
  const tan = Math.tan(THREE.MathUtils.degToRad(21));
  return (Math.max(h, w / 1.6) / 2 / tan) * margin;
}
