// View 7: market globe. One pin per game the trade workers hold (or a single
// "NFL (Polymarket US)" pin when nothing trades); machines orbit, and each worker in
// the trade role draws an arc to its first game's pin.
import * as THREE from 'three';
import { hashOf } from '../fleet';
import type { FleetNode } from '../fleet';
import { RING, SPHERE, TAU, circleLine, frac, fresnelMat, makeLabel, makeOrb, setText, sprite, tintOrb } from './shared';
import type { Builder } from './shared';

const GR = 6, SEG = 28;
export const DEFAULT_MARKET = 'NFL (Polymarket US)';

/** The pins for these nodes: every distinct trade game, sorted, or the default pin. */
export function marketsOf(nodes: FleetNode[]) {
  const games = [...new Set(nodes.flatMap((n) => n.games))].sort();
  return games.length ? games : [DEFAULT_MARKET];
}

export const globe: Builder = (nodes) => {
  const group = new THREE.Group();
  const ball = new THREE.Group();
  const body = new THREE.Mesh(SPHERE, new THREE.MeshStandardMaterial({ color: 0x04101a, roughness: 0.9, emissive: 0x06202e, emissiveIntensity: 0.6 }));
  body.scale.setScalar(GR);
  ball.add(body);
  const dots: number[] = [];
  for (let i = 0; i < 1800; i++) {
    const y = 1 - ((i + 0.5) / 1800) * 2, r = Math.sqrt(1 - y * y), a = i * 2.399963;
    const land = Math.sin(y * 7 + Math.cos(a * 3) * 2) + Math.sin(a * 2 + y * 4) > 0.15;
    if (land) dots.push(Math.cos(a) * r * (GR + 0.04), y * (GR + 0.04), Math.sin(a) * r * (GR + 0.04));
  }
  const dotGeo = new THREE.BufferGeometry();
  dotGeo.setAttribute('position', new THREE.Float32BufferAttribute(dots, 3));
  ball.add(new THREE.Points(dotGeo, new THREE.PointsMaterial({ color: 0x45c4f5, size: 0.09, transparent: true, opacity: 0.85 })));
  for (let i = 0; i < 6; i++) { const m = circleLine(GR + 0.02, 0x1f4a60, 0.5); m.rotation.x = Math.PI / 2; m.rotation.y = (i / 6) * Math.PI; ball.add(m); }
  [-0.6, -0.3, 0, 0.3, 0.6].forEach((s) => { const l = circleLine(Math.cos(Math.asin(s)) * (GR + 0.02), 0x1f4a60, 0.5); l.position.y = s * GR; ball.add(l); });
  const atmo = new THREE.Mesh(SPHERE, fresnelMat(0x45c4f5, 3, 0.9));
  atmo.scale.setScalar(GR * 1.09);
  group.add(ball, atmo);

  // pins spread over the northern hemisphere, the first one over North America
  const markets = marketsOf(nodes);
  const marks = markets.map((name, i) => {
    const lat = 38 - ((i * 23) % 70), lon = -95 + i * 137.5;
    const p = new THREE.Vector3().setFromSphericalCoords(GR + 0.05, THREE.MathUtils.degToRad(90 - lat), THREE.MathUtils.degToRad(lon));
    const pin = new THREE.Mesh(new THREE.SphereGeometry(0.14, 12, 8), new THREE.MeshBasicMaterial({ color: 0xffffff }));
    pin.position.copy(p);
    const pulse = new THREE.Sprite(new THREE.SpriteMaterial({ map: RING, color: 0xffffff, transparent: true, depthWrite: false }));
    pulse.position.copy(p);
    const lab = makeLabel('lbl lbl-zone');
    lab.position.copy(p).multiplyScalar(1.14);
    ball.add(pin, pulse, lab);
    return { name, pin, pulse, lab, world: new THREE.Vector3() };
  });
  const pinOf = (n: FleetNode) => Math.max(0, n.games.length ? markets.indexOf(n.games[0]) : 0);

  const items = nodes.map((n, k) => {
    const h = hashOf(n.id);
    const orb = makeOrb(n, 0.5, h);
    const q = new THREE.Quaternion().setFromEuler(new THREE.Euler((h - 0.5) * 2.2, h * 7, (hashOf(n.id + '3') - 0.5) * 1.6));
    const u = new THREE.Vector3(1, 0, 0).applyQuaternion(q), v = new THREE.Vector3(0, 0, 1).applyQuaternion(q);
    const arcPos = new Float32Array((SEG + 1) * 3);
    const arcGeo = new THREE.BufferGeometry();
    arcGeo.setAttribute('position', new THREE.BufferAttribute(arcPos, 3));
    const arc = new THREE.Line(arcGeo, new THREE.LineBasicMaterial({ color: 0x45c4f5, transparent: true, opacity: 0.7, blending: THREE.AdditiveBlending, depthWrite: false }));
    arc.frustumCulled = false;
    const sparks = [0, 1].map(() => sprite(0xffffff, 0.6));
    const R = 10.5 + (k % 3) * 0.8;
    const orbit = circleLine(R, 0x14303d, 0.6);
    orbit.quaternion.copy(q);
    group.add(orb.g, arc, orbit, ...sparks);
    return { orb, u, v, a: hashOf(n.id + 'a') * TAU, R, arcPos, arc, sparks, h };
  });
  const ctrl = new THREE.Vector3(), tmp = new THREE.Vector3();
  const bez = (out: THREE.Vector3, a: THREE.Vector3, c: THREE.Vector3, b: THREE.Vector3, f: number) =>
    out.set(0, 0, 0).addScaledVector(a, (1 - f) * (1 - f)).addScaledVector(c, 2 * f * (1 - f)).addScaledVector(b, f * f);

  return {
    group, cam: [0, 6, 27], spin: 0.2,
    pick: items.map((i) => i.orb.mesh),
    update(t, dt, ns, sel) {
      ball.rotation.y += dt * 0.07;
      ball.updateMatrixWorld();
      const counts = marks.map(() => 0);
      marks.forEach((m, i) => {
        m.pin.getWorldPosition(m.world);
        const f = frac(t * 0.6 + i * 0.3);
        m.pulse.scale.setScalar(0.4 + f * 1.8);
        (m.pulse.material as THREE.SpriteMaterial).opacity = 1 - f;
      });
      ns.forEach((n, k) => {
        const it = items[k];
        if (!it) return;
        it.a += dt * (n.online ? 0.05 + n.cpu * 0.0022 : 0.01);
        it.orb.g.position.set(0, 0, 0).addScaledVector(it.u, Math.cos(it.a) * it.R).addScaledVector(it.v, Math.sin(it.a) * it.R);
        tintOrb(it.orb, n, t, sel);
        const trading = n.online && n.job === 'trade';
        it.arc.visible = trading;
        it.sparks.forEach((s) => (s.visible = trading));
        if (!trading) return;
        const mi = pinOf(n);
        counts[mi]++;
        const a = it.orb.g.position, b = marks[mi].world;
        ctrl.copy(a).add(b).multiplyScalar(0.5);
        if (ctrl.lengthSq() < 1) ctrl.set(0, 1, 0);
        ctrl.normalize().multiplyScalar(11.5);
        for (let i = 0; i <= SEG; i++) { bez(tmp, a, ctrl, b, i / SEG); it.arcPos.set([tmp.x, tmp.y, tmp.z], i * 3); }
        it.arc.geometry.attributes.position.needsUpdate = true;
        (it.arc.material as THREE.LineBasicMaterial).color.copy(it.orb.mat.emissive);
        (it.arc.material as THREE.LineBasicMaterial).opacity = sel === n.id ? 1 : 0.55;
        it.sparks.forEach((s, i) => {
          const f = frac(t * (0.25 + n.cpu * 0.004) + i * 0.5 + it.h);
          bez(s.position, a, ctrl, b, f);
          (s.material as THREE.SpriteMaterial).opacity = Math.sin(f * Math.PI);
        });
      });
      marks.forEach((m, i) => setText(m.lab, `${m.name} · ${counts[i]}`));
    },
  };
};
