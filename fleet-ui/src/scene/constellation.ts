// View 2: constellation. Each role is a cluster; the cluster centres sit on an ellipse
// around the controller, offline machines gather apart in the lower right.
import * as THREE from 'three';
import type { CSS2DObject } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import { hashOf } from '../fleet';
import { TAU, makeLabel, makeOrb, roleIndex, setText, sprite, tintOrb } from './shared';
import type { Builder } from './shared';

export const constellation: Builder = (nodes, _renderer, roles) => {
  const group = new THREE.Group();
  const m = Math.max(1, roles.length);
  const spread = Math.max(1, m / 5);
  const centres = roles.map((_, i) => {
    const a = Math.PI * 0.95 - (i / m) * TAU;
    return new THREE.Vector3(Math.cos(a) * 11 * spread, Math.sin(a) * 6.5 * spread + 0.5, Math.sin(a * 2 + 0.6) * 4);
  });
  const OFF = new THREE.Vector3(16 * spread, -9 * spread, -7);
  const centreOf = (job: string, online: boolean) => {
    const i = roleIndex(roles, job);
    return online && i >= 0 ? centres[i] : OFF;
  };

  const zoneLabs: CSS2DObject[] = [];
  const hub: THREE.Vector3[] = [];
  roles.forEach((j, i) => {
    const lab = makeLabel('lbl lbl-zone');
    lab.position.copy(centres[i]).add(new THREE.Vector3(0, 4.6, 0));
    group.add(lab);
    zoneLabs.push(lab);
    hub.push(new THREE.Vector3(), centres[i]);
    const shell = new THREE.Mesh(new THREE.IcosahedronGeometry(4.2, 1), new THREE.MeshBasicMaterial({ color: 0x45c4f5, wireframe: true, transparent: true, opacity: 0.05 }));
    shell.position.copy(centres[i]);
    shell.userData.spin = 0.05 + hashOf(j.id) * 0.1;
    group.add(shell);
  });
  if (hub.length) group.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(hub), new THREE.LineDashedMaterial({ color: 0x1f4254, dashSize: 0.4, gapSize: 0.4 })).computeLineDistances());
  group.add(sprite(0x9fdcf7, 3, 0.6));

  const items = nodes.map((n) => {
    const h = hashOf(n.id), h2 = hashOf(n.id + '7');
    const orb = makeOrb(n, 0.7, h);
    const off = new THREE.Vector3().setFromSphericalCoords(2.4 + h * 1.2, Math.acos(2 * h2 - 1), h * TAU * 3);
    orb.g.position.copy(centreOf(n.job, n.online)).add(off);
    group.add(orb.g);
    return { orb, off, h };
  });
  // one segment per member of each cluster (a closed loop), two vertices each
  const cap = (nodes.length + roles.length) * 2 + 2;
  const linePos = new Float32Array(cap * 3);
  const lineGeo = new THREE.BufferGeometry();
  lineGeo.setAttribute('position', new THREE.BufferAttribute(linePos, 3));
  const lines = new THREE.LineSegments(lineGeo, new THREE.LineBasicMaterial({ color: 0x8fd0f2, transparent: true, opacity: 0.55, blending: THREE.AdditiveBlending }));
  lines.frustumCulled = false;
  group.add(lines);
  const tmp = new THREE.Vector3();

  return {
    group, cam: [0, 3, 31 * spread], spin: 0.35,
    pick: items.map((i) => i.orb.mesh),
    update(t, dt, all, sel) {
      const ns = all.slice(0, items.length);
      group.children.forEach((c) => { if (c.userData.spin) { c.rotation.y += dt * c.userData.spin; c.rotation.x += dt * c.userData.spin * 0.6; } });
      let v = 0;
      roles.forEach((j, ri) => {
        const mem = ns.map((n, k) => ({ n, k })).filter((x) => x.n.online && x.n.job === j.id);
        setText(zoneLabs[ri], `${j.name.toUpperCase()} · ${mem.length}`);
        const segs = mem.length > 2 ? mem.length : mem.length - 1;
        for (let i = 0; i < segs && v + 2 <= cap; i++) {
          const a = items[mem[i].k].orb.g.position, b = items[mem[(i + 1) % mem.length].k].orb.g.position;
          linePos.set([a.x, a.y, a.z, b.x, b.y, b.z], v * 3);
          v += 2;
        }
      });
      lineGeo.setDrawRange(0, v);
      lineGeo.attributes.position.needsUpdate = true;
      ns.forEach((n, k) => {
        const it = items[k];
        tmp.copy(centreOf(n.job, n.online)).add(it.off);
        tmp.y += Math.sin(t * 0.6 + it.h * 20) * 0.35;
        if (!n.online) tmp.x += Math.sin(t * 0.15) * 1.5;
        it.orb.g.position.lerp(tmp, Math.min(1, dt * 2));
        tintOrb(it.orb, n, t, sel);
      });
    },
  };
};
