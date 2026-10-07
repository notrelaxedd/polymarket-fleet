// View 4: liquid orbs. One glass sphere per machine on a centred grid; the liquid
// level is CPU load, it sloshes harder when the machine runs hot.
import * as THREE from 'three';
import { hashOf, statusOf } from '../fleet';
import { COL, RING, SPHERE, colorOf, fitDistance, frac, fresnelMat, gridCells, makeLabel, setText, sprite, tempOr, tempText } from './shared';
import type { Builder } from './shared';

const RAD = 1.9, DX = 5.4, DY = 5.7;
const UPZ = new THREE.Vector3(0, 0, 1);

export const liquid: Builder = (nodes, renderer) => {
  renderer.localClippingEnabled = true;
  const group = new THREE.Group();
  const { cols, rows, cells } = gridCells(nodes.length);
  const items = nodes.map((n, k) => {
    const cx = cells[k][0] * DX, cy = -cells[k][1] * DY + 0.5;
    const g = new THREE.Group();
    g.position.set(cx, cy, 0);
    const glass = new THREE.Mesh(SPHERE, new THREE.MeshPhongMaterial({ color: 0x9fdcf7, transparent: true, opacity: 0.07, shininess: 140, specular: 0xffffff, depthWrite: false }));
    glass.scale.setScalar(RAD);
    glass.userData.id = n.id;
    const rim = new THREE.Mesh(SPHERE, fresnelMat(0xbfe9ff, 2.6, 0.75));
    rim.scale.setScalar(RAD * 1.01);
    const plane = new THREE.Plane(new THREE.Vector3(0, -1, 0), 0);
    const liqMat = new THREE.MeshStandardMaterial({ color: 0x04121a, emissive: colorOf(n).clone(), emissiveIntensity: 0.42, roughness: 0.4, side: THREE.DoubleSide, clippingPlanes: [plane] });
    const liq = new THREE.Mesh(SPHERE, liqMat);
    liq.scale.setScalar(RAD * 0.93);
    const cap = new THREE.Mesh(new THREE.CircleGeometry(1, 48), new THREE.MeshBasicMaterial({ color: 0xffffff, side: THREE.DoubleSide, transparent: true, opacity: 0.85 }));
    const bubbles = [0, 1, 2, 3, 4].map(() => sprite(0xffffff, 0.22, 0.8));
    const ring = new THREE.Sprite(new THREE.SpriteMaterial({ map: RING, color: 0xffffff, transparent: true, depthTest: false }));
    ring.scale.setScalar(RAD * 2.7);
    const lab = makeLabel();
    lab.position.set(0, -RAD - 0.8, 0);
    g.add(liq, cap, glass, rim, ring, lab, ...bubbles);
    group.add(g);
    return { g, glass, rim, plane, liq, liqMat, cap, bubbles, ring, lab, cx, cy, lvl: 0, h: hashOf(n.id) };
  });
  const nrm = new THREE.Vector3(), p0 = new THREE.Vector3(), up = new THREE.Vector3();
  const w = (cols - 1) * DX + RAD * 2, h = (rows - 1) * DY + RAD * 2 + 1.6;
  const dist = Math.max(27, fitDistance(w, h));

  return {
    group, cam: [0, 0.5, dist], spin: 0,
    pick: items.map((i) => i.glass),
    update(t, dt, ns, sel) {
      ns.forEach((n, k) => {
        const it = items[k];
        if (!it) return;
        const st = statusOf(n);
        const goal = n.online ? THREE.MathUtils.clamp(n.cpu / 100, 0.04, 0.96) : 0;
        it.lvl += (goal - it.lvl) * Math.min(1, dt * 1.5);
        const amp = st === 'hot' ? 0.16 : 0.03 + it.lvl * 0.04;
        const sp = st === 'hot' ? 3.2 : 1.3;
        nrm.set(Math.sin(t * sp + it.h * 9) * amp, -1, Math.cos(t * sp * 1.3 + it.h * 5) * amp).normalize();
        const r = RAD * 0.93, yLoc = -r + 2 * r * it.lvl;
        p0.set(it.cx, it.cy + yLoc, 0);
        it.plane.normal.copy(nrm);
        it.plane.constant = -nrm.dot(p0);
        it.liqMat.emissive.lerp(COL[st], 0.08);
        it.liq.visible = it.cap.visible = it.lvl > 0.02;
        up.copy(nrm).negate();
        it.cap.position.set(0, yLoc, 0);
        it.cap.quaternion.setFromUnitVectors(UPZ, up);
        it.cap.scale.setScalar(Math.sqrt(Math.max(0.001, r * r - yLoc * yLoc)) * 0.985);
        (it.cap.material as THREE.MeshBasicMaterial).color.copy(it.liqMat.emissive).multiplyScalar(0.8);
        (it.rim.material as THREE.ShaderMaterial).uniforms.uInt.value = n.online ? 0.75 : 0.3;
        it.bubbles.forEach((b, i) => {
          b.visible = n.online && n.job !== 'idle';
          const f = frac(t * (0.25 + (tempOr(n) - 40) * 0.012) + i * 0.21 + it.h);
          b.position.set(Math.sin(i * 2.4 + it.h * 6) * r * 0.45, -r * 0.8 + f * (yLoc + r * 0.8), 0.6);
          (b.material as THREE.SpriteMaterial).opacity = Math.sin(f * Math.PI) * 0.8;
        });
        it.ring.visible = sel === n.id;
        (it.ring.material as THREE.SpriteMaterial).rotation = t * 0.6;
        setText(it.lab, n.online ? `${n.name} · ${Math.round(n.cpu)}% · ${tempText(n)}` : `${n.name} · off`, sel === n.id);
      });
    },
  };
};
