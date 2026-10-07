// View 6: data city (the default). One tower per machine on a centred grid of lots,
// height is CPU load and lit windows are activity; a district, river, traffic and the
// controller's uplink mast fill the rest. Lots are `G` apart: lot (i, j) sits at
// x = (i + 0.5) * G, z = j * G, and towers take every other lot.
import * as THREE from 'three';
import { CSS2DObject } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import { hashOf, jobName, statusOf } from '../fleet';
import type { FleetNode } from '../fleet';
import { COL, GLOW, RING, WHITE, colorOf, frac, makeLabel, pctText, setText, sprite } from './shared';
import type { Builder } from './shared';
import { buildDistrict } from './cityDistrict';

const G = 6.5;
const CSS_TONE = { ok: 'var(--ok)', hot: 'var(--hot)', idle: 'var(--ok)', off: 'var(--off)' } as const;

function makeHead(compact: boolean) {
  const el = document.createElement('div');
  el.className = 'lbl lbl-head' + (compact ? ' compact' : '');
  el.innerHTML = '<b></b><span></span><span></span>';
  const o = new CSS2DObject(el);
  o.center.set(0.5, 1);
  return o;
}
function setHead(o: CSS2DObject, n: FleetNode, sel: boolean) {
  const vals = [
    n.name,
    n.online ? jobName(n.job) + (n.games.length ? ` · ${n.games[0]}` : '') : n.rebooting ? 'Rebooting' : 'Offline',
    `CPU ${pctText(n, n.cpu)} · RAM ${pctText(n, n.ram)}`,
  ];
  vals.forEach((v, i) => { const c = o.element.children[i]; if (c.textContent !== v) c.textContent = v; });
  o.element.classList.toggle('is-sel', sel);
  o.element.style.setProperty('--c', CSS_TONE[statusOf(n)]);
}

/** Tower lots for n machines: cols = min(6, ceil(sqrt n)), every other lot, centred. */
export function towerLots(n: number) {
  const cols = Math.max(1, Math.min(6, Math.ceil(Math.sqrt(n))));
  const rows = Math.max(1, Math.ceil(n / cols));
  const lots = Array.from({ length: n }, (_, k) => [2 * (k % cols) - cols, 2 * Math.floor(k / cols) - (rows - 1)] as const);
  const iMin = -cols, iMax = cols - 2, jMin = -(rows - 1), jMax = rows - 1;
  // the mast takes a free lot next to the middle tower (odd offset: never a tower lot)
  const mast = [2 * Math.floor((cols - 1) / 2) - cols + 1, 2 * Math.floor((rows - 1) / 2) - (rows - 1) + 1] as const;
  return { cols, rows, lots, iMin, iMax, jMin, jMax, mast };
}

export const city: Builder = (nodes) => {
  const group = new THREE.Group();
  const L = towerLots(nodes.length);
  const lotX = (i: number) => (i + 0.5) * G;
  const cx = (lotX(L.iMin) + lotX(L.iMax)) / 2, cz = ((L.jMin + L.jMax) / 2) * G;
  const riverJ = L.jMin - 3.5; // two lots south of the first tower row's street
  const taken = new Set(L.lots.map(([i, j]) => `${i},${j}`));
  taken.add(`${L.mast[0]},${L.mast[1]}`);

  const district = buildDistrict(group, {
    G, taken, riverZ: riverJ * G,
    near: { x0: lotX(L.iMin) - 7.5, x1: lotX(L.iMax) + 7.5, z0: L.jMin * G - 4, z1: L.jMax * G + 4 },
  });

  // uplink mast on its own plaza
  const mast = new THREE.Group();
  mast.position.set(lotX(L.mast[0]), 0, L.mast[1] * G);
  const steel = new THREE.MeshStandardMaterial({ color: 0x1c2f3a, metalness: 0.7, roughness: 0.4 });
  const base = new THREE.Mesh(new THREE.BoxGeometry(3.2, 0.7, 3.2), steel);
  base.position.y = 0.35;
  const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.26, 10, 6), steel);
  pole.position.y = 5.7;
  mast.add(base, pole);
  [6.2, 8.2].forEach((y) => { const arm = new THREE.Mesh(new THREE.BoxGeometry(1.6, 0.08, 0.08), steel); arm.position.y = y; mast.add(arm); });
  const tip = sprite(0xbfe9ff, 1.6, 0.9);
  tip.position.y = 10.9;
  const waves = [0, 1].map(() => { const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: RING, color: 0x8fd0f2, transparent: true, depthWrite: false })); s.position.y = 10.9; mast.add(s); return s; });
  const mastLab = makeLabel('lbl lbl-zone');
  mastLab.position.y = 12.4;
  setText(mastLab, 'CONTROLLER UPLINK');
  mast.add(tip, mastLab);
  group.add(mast);

  // the towers
  const geo = new THREE.BoxGeometry(3.8, 1, 3.8);
  const slabGeo = new THREE.BoxGeometry(5.6, 0.3, 5.6);
  const slabEdges = new THREE.EdgesGeometry(slabGeo);
  geo.translate(0, 0.5, 0);
  const towerEdges = new THREE.EdgesGeometry(geo);
  const items = nodes.map((n, k) => {
    const cvs = document.createElement('canvas');
    cvs.width = 64; cvs.height = 128;
    const tex = new THREE.CanvasTexture(cvs);
    tex.wrapT = THREE.RepeatWrapping;
    tex.magFilter = THREE.NearestFilter;
    const side = new THREE.MeshStandardMaterial({ color: 0x0a1319, roughness: 0.5, metalness: 0.4, emissive: colorOf(n).clone(), emissiveMap: tex, emissiveIntensity: 1.1 });
    const top = new THREE.MeshStandardMaterial({ color: 0x0a1319, emissive: colorOf(n).clone(), emissiveIntensity: 0.5 });
    const tower = new THREE.Mesh(geo, [side, side, top, top, side, side]);
    const [i, j] = L.lots[k];
    tower.position.set(lotX(i), 0, j * G);
    tower.userData.id = n.id;
    const edgeMat = new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.9 });
    tower.add(new THREE.LineSegments(towerEdges, edgeMat));
    const slab = new THREE.Mesh(slabGeo, new THREE.MeshStandardMaterial({ color: 0x0a1319, roughness: 0.5, metalness: 0.5 }));
    slab.position.set(tower.position.x, 0.15, tower.position.z);
    slab.add(new THREE.LineSegments(slabEdges, edgeMat));
    group.add(slab);
    const pool = new THREE.Mesh(new THREE.PlaneGeometry(15, 15), new THREE.MeshBasicMaterial({ map: GLOW, color: colorOf(n).clone(), transparent: true, opacity: 0.3, blending: THREE.AdditiveBlending, depthWrite: false }));
    pool.rotation.x = -Math.PI / 2;
    pool.position.set(tower.position.x, 0.05, tower.position.z);
    const beacon = sprite(0xffa733, 1.4, 0);
    const beam = new THREE.Mesh(new THREE.CylinderGeometry(0.1, 0.1, 40, 8), new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.35, blending: THREE.AdditiveBlending, depthWrite: false }));
    beam.visible = false;
    const lab = makeHead(nodes.length > 12);
    group.add(tower, pool, beacon, beam, lab);
    return { cvs, tex, side, top, tower, pool, beacon, beam, lab, edgeMat, hgt: 0.5, next: k * 0.09, h: hashOf(n.id) };
  });
  const drawWindows = (cvs: HTMLCanvasElement, lit: number) => {
    const g = cvs.getContext('2d')!;
    g.fillStyle = '#000'; g.fillRect(0, 0, 64, 128);
    for (let r = 0; r < 12; r++) for (let c = 0; c < 5; c++) {
      const on = Math.random() < lit;
      g.fillStyle = on ? (Math.random() < 0.25 ? '#ffffff' : '#8a8a8a') : '#101010';
      g.fillRect(5 + c * 12, 5 + r * 10.4, 7, 5);
    }
  };

  // camera: the prototype's framing of a 4 x 3 block, scaled for bigger blocks
  const s = Math.max(1, L.cols / 4, L.rows / 3.2);
  const target: [number, number, number] = [cx, 3.5, cz];
  return {
    group, cam: [cx + 27.25 * s, 3.5 + 17.5 * s, cz + 36 * s], target, spin: 0.25, maxPolar: 1.46,
    pick: items.map((i) => i.tower),
    update(t, dt, ns, sel) {
      district.update(t);
      ns.forEach((n, k) => {
        const it = items[k];
        if (!it) return;
        const st = statusOf(n);
        const goal = n.online ? 2.4 + n.cpu * 0.11 : 0.9;
        it.hgt += (goal - it.hgt) * Math.min(1, dt * 1.8);
        it.tower.scale.y = it.hgt;
        it.tex.repeat.y = Math.max(0.1, it.hgt / 5);
        if (t > it.next) { drawWindows(it.cvs, n.online ? (n.job === 'idle' ? 0.12 : 0.25 + (n.cpu / 100) * 0.65) : 0); it.tex.needsUpdate = true; it.next = t + 0.7 + it.h * 0.6; }
        it.side.emissive.lerp(COL[st], 0.08);
        it.top.emissive.copy(it.side.emissive);
        it.top.emissiveIntensity = st === 'off' ? 0.04 : 0.45;
        it.edgeMat.color.copy(it.side.emissive).lerp(WHITE, st === 'off' ? 0 : 0.45);
        it.edgeMat.opacity = st === 'off' ? 0.6 : 0.95;
        const pm = it.pool.material as THREE.MeshBasicMaterial;
        pm.color.copy(it.side.emissive);
        pm.opacity = st === 'off' ? 0.04 : st === 'idle' ? 0.3 : 0.4 + (n.cpu / 100) * 0.25;
        const p = it.tower.position;
        it.beacon.position.set(p.x, it.hgt + 0.35, p.z);
        (it.beacon.material as THREE.SpriteMaterial).opacity = st === 'hot' ? 0.5 + 0.5 * Math.sin(t * 6 + it.h * 9) : 0;
        it.beam.visible = sel === n.id;
        it.beam.position.set(p.x, it.hgt + 20, p.z);
        it.lab.position.set(p.x, it.hgt + 1.1, p.z);
        setHead(it.lab, n, sel === n.id);
      });
      (tip.material as THREE.SpriteMaterial).opacity = 0.55 + 0.45 * Math.sin(t * 3);
      waves.forEach((w, i) => {
        const f = frac(t * 0.45 + i * 0.5);
        w.scale.setScalar(0.6 + f * 5);
        (w.material as THREE.SpriteMaterial).opacity = (1 - f) * 0.7;
      });
    },
  };
};
