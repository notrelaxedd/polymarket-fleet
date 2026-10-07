// View 1: solar system. The controller is the sun; each role is an orbit (radius
// 5 + 3 * role index), offline machines drift below on an outer ring.
import * as THREE from 'three';
import type { CSS2DObject } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import { hashOf } from '../fleet';
import { SPHERE, TAU, circleLine, fresnelMat, makeLabel, makeOrb, roleIndex, setText, sprite, tintOrb } from './shared';
import type { Builder } from './shared';

const TRAIL = 54;

export const solar: Builder = (nodes, _renderer, roles) => {
  const group = new THREE.Group();
  const radius = (i: number) => 5 + 3 * i;
  const outer = radius(roles.length) + 1;
  const goalOf = (job: string, online: boolean) => {
    const i = roleIndex(roles, job);
    return online && i >= 0 ? radius(i) : outer;
  };

  const sun = new THREE.Mesh(SPHERE, new THREE.MeshBasicMaterial({ color: 0x9fd4ec }));
  sun.scale.setScalar(1.5);
  const corona = new THREE.Mesh(SPHERE, fresnelMat(0x9fdcf7, 1.8, 0.9));
  corona.scale.setScalar(2.2);
  const sunLab = makeLabel('lbl lbl-zone');
  sunLab.position.set(0, -2.6, 0);
  setText(sunLab, 'CONTROLLER');
  group.add(sun, corona, sprite(0x9fdcf7, 11, 0.3), sunLab);

  const ringLabs: CSS2DObject[] = roles.map((j, i) => {
    const R = radius(i);
    group.add(circleLine(R, j.id === 'idle' ? 0x1b3442 : 0x2b5a72, 0.9));
    const band = new THREE.Mesh(new THREE.RingGeometry(R - 0.5, R + 0.5, 128), new THREE.MeshBasicMaterial({ color: 0x45c4f5, transparent: true, opacity: 0.035, side: THREE.DoubleSide, depthWrite: false }));
    band.rotation.x = -Math.PI / 2;
    group.add(band);
    const lab = makeLabel('lbl lbl-zone');
    const a = -0.5 - i * 0.16;
    lab.position.set(Math.cos(a) * R, 0.9, Math.sin(a) * R);
    group.add(lab);
    return lab;
  });

  const items = nodes.map((n) => {
    const h = hashOf(n.id);
    const orb = makeOrb(n, 0.62, h);
    const pos = new Float32Array(TRAIL * 3);
    const col = new Float32Array(TRAIL * 3);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geo.setAttribute('color', new THREE.BufferAttribute(col, 3));
    const trail = new THREE.Line(geo, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));
    trail.frustumCulled = false;
    group.add(orb.g, trail);
    return { orb, trail, pos, col, ang: h * TAU, rad: goalOf(n.job, n.online), init: false };
  });

  const camScale = Math.max(1, (outer + 2) / 23);
  return {
    group, cam: [0, 15 * camScale, 27 * camScale], spin: 0.25,
    pick: items.map((i) => i.orb.mesh),
    update(t, dt, ns, sel) {
      roles.forEach((j, i) => setText(ringLabs[i], `${j.name.toUpperCase()} · ${ns.filter((n) => n.online && n.job === j.id).length}`));
      ns.forEach((n, k) => {
        const it = items[k];
        if (!it) return;
        it.rad += (goalOf(n.job, n.online) - it.rad) * Math.min(1, dt * 1.6);
        it.ang += dt * (n.online ? 0.07 + (n.cpu / 100) * 0.5 : 0.015);
        const y = n.online ? 0 : -2 + Math.sin(t * 0.4) * 1.2;
        it.orb.g.position.set(Math.cos(it.ang) * it.rad, y, Math.sin(it.ang) * it.rad);
        tintOrb(it.orb, n, t, sel);
        const p = it.orb.g.position;
        if (!it.init) { for (let i = 0; i < TRAIL; i++) it.pos.set([p.x, p.y, p.z], i * 3); it.init = true; }
        it.pos.copyWithin(3, 0, (TRAIL - 1) * 3);
        it.pos.set([p.x, p.y, p.z], 0);
        const c = it.orb.mat.emissive, s = n.online ? 1 : 0.15;
        for (let i = 0; i < TRAIL; i++) { const f = (1 - i / TRAIL) ** 2 * s; it.col.set([c.r * f, c.g * f, c.b * f], i * 3); }
        it.trail.geometry.attributes.position.needsUpdate = true;
        it.trail.geometry.attributes.color.needsUpdate = true;
      });
      corona.scale.setScalar(2.2 + Math.sin(t * 1.4) * 0.08);
    },
  };
};
