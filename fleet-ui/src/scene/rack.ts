// View 5: rack twin. Machines are 1U units stacked in a rack (up to 12 per rack; a
// bigger fleet gets more racks side by side). The selected unit slides out.
import * as THREE from 'three';
import { hashOf, jobShort, statusOf } from '../fleet';
import { COL, colorOf, frac, makeLabel, setText, sprite, tempText } from './shared';
import type { Builder } from './shared';

const U = 0.98, PER_RACK = 12, RACK_DX = 9.6;

export const rack: Builder = (nodes) => {
  const group = new THREE.Group();
  const n = nodes.length;
  const racks = Math.max(1, Math.ceil(n / PER_RACK));
  const perRack = Math.max(1, Math.ceil(n / racks));
  const slots = Math.max(6, perRack);
  const H = slots * U + 0.7;
  const floorY = -H / 2 - 0.15;
  const rackX = (r: number) => (r - (racks - 1) / 2) * RACK_DX;

  const metal = new THREE.MeshStandardMaterial({ color: 0x141c22, metalness: 0.75, roughness: 0.42 });
  for (let r = 0; r < racks; r++) {
    const x0 = rackX(r);
    [[-3.85, -2.8], [3.85, -2.8], [-3.85, 2.8], [3.85, 2.8]].forEach(([x, z]) => {
      const post = new THREE.Mesh(new THREE.BoxGeometry(0.28, H, 0.28), metal);
      post.position.set(x0 + x, 0, z);
      group.add(post);
    });
    [-H / 2, H / 2].forEach((y) => { const p = new THREE.Mesh(new THREE.BoxGeometry(8.1, 0.22, 6), metal); p.position.set(x0, y, 0); group.add(p); });
    const frame = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(8.3, H + 0.3, 6.2)), new THREE.LineBasicMaterial({ color: 0x2b6f8f, transparent: true, opacity: 0.6 }));
    frame.position.x = x0;
    group.add(frame);
  }
  const grid = new THREE.GridHelper(80, 80, 0x1d4a5f, 0x0d1d26);
  grid.position.y = floorY + 0.05;
  group.add(grid);
  const floor = new THREE.Mesh(new THREE.CircleGeometry(40, 48), new THREE.MeshStandardMaterial({ color: 0x05090d, roughness: 0.6, metalness: 0.4 }));
  floor.rotation.x = -Math.PI / 2; floor.position.y = floorY;
  group.add(floor);

  const gc = document.createElement('canvas');
  gc.width = 256; gc.height = 32;
  const g2 = gc.getContext('2d')!;
  g2.fillStyle = '#0b1116'; g2.fillRect(0, 0, 256, 32);
  g2.fillStyle = '#1e2a33';
  for (let x = 70; x < 200; x += 5) for (let y = 6; y < 28; y += 5) g2.fillRect(x, y, 3, 3);
  const grille = new THREE.CanvasTexture(gc);
  const bodyGeo = new THREE.BoxGeometry(7.2, 0.82, 5.2);
  const faceGeo = new THREE.PlaneGeometry(7.2, 0.82);

  const items = nodes.map((node, k) => {
    const r = Math.floor(k / perRack), slot = k % perRack;
    const g = new THREE.Group();
    g.position.set(rackX(r), ((slots - 1) / 2 - slot) * U, 0);
    const bodyMat = new THREE.MeshStandardMaterial({ color: 0x18222a, metalness: 0.7, roughness: 0.45, emissive: colorOf(node).clone(), emissiveIntensity: 0.1 });
    const body = new THREE.Mesh(bodyGeo, bodyMat);
    body.userData.id = node.id;
    const face = new THREE.Mesh(faceGeo, new THREE.MeshBasicMaterial({ map: grille }));
    face.position.z = 2.605;
    const barMat = new THREE.MeshBasicMaterial({ color: colorOf(node).clone() });
    const bar = new THREE.Mesh(new THREE.BoxGeometry(1, 0.1, 0.04), barMat);
    bar.position.set(-3.2, -0.22, 2.63);
    const led = new THREE.Mesh(new THREE.SphereGeometry(0.09, 12, 8), new THREE.MeshBasicMaterial({ color: 0x45c4f5 }));
    led.position.set(3.2, 0.12, 2.63);
    const disk = new THREE.Mesh(new THREE.SphereGeometry(0.06, 10, 6), new THREE.MeshBasicMaterial({ color: 0xffffff }));
    disk.position.set(2.9, 0.12, 2.63);
    const lab = makeLabel();
    lab.position.set(0.2, 0.02, 2.75);
    const plume = [0, 1, 2].map(() => sprite(0xffa733, 1.6, 0));
    g.add(body, face, bar, led, disk, lab, ...plume);
    group.add(g);
    return { g, body, bodyMat, bar, barMat, led, disk, lab, plume, h: hashOf(node.id) };
  });

  const s = Math.max(1, slots / 12, (racks * RACK_DX) / 22);
  const tx = 0.8;
  return {
    group, cam: [tx + 12.2 * s, 3.5 * s, 16 * s], target: [tx, 0, 0], spin: 0,
    pick: items.map((i) => i.body),
    update(t, dt, ns, sel) {
      ns.forEach((n, k) => {
        const it = items[k];
        if (!it) return;
        const st = statusOf(n), load = n.online ? n.cpu / 100 : 0;
        it.g.position.z += ((sel === n.id ? 2.7 : 0) - it.g.position.z) * Math.min(1, dt * 6);
        it.bodyMat.emissive.lerp(COL[st], 0.08);
        const heat = n.temp === null ? 0.3 : THREE.MathUtils.clamp((n.temp - 36) / 45, 0.05, 1);
        it.bodyMat.emissiveIntensity = st === 'off' ? 0.02 : heat * (st === 'hot' ? 0.4 : 0.14);
        it.barMat.color.copy(it.bodyMat.emissive).multiplyScalar(st === 'off' ? 0.3 : 1.1);
        const w = Math.max(0.04, load * 4.6);
        it.bar.scale.x = w; it.bar.position.x = -3.2 + w / 2;
        (it.led.material as THREE.MeshBasicMaterial).color.set(st === 'off' ? 0x26323a : st === 'hot' ? 0xffa733 : 0x45c4f5).multiplyScalar(n.online ? 1.5 : 1);
        it.disk.visible = n.online && Math.sin(t * (6 + load * 30) + it.h * 40) > 0.2;
        it.plume.forEach((p, i) => {
          const f = frac(t * 0.35 + i / 3 + it.h);
          p.position.set(Math.sin(i * 2 + it.h * 8) * 2.4, f * 2.2, -2.8 - f * 3.5);
          (p.material as THREE.SpriteMaterial).opacity = st === 'hot' ? Math.sin(f * Math.PI) * 0.35 : 0;
        });
        setText(it.lab, n.online ? `${n.name}  ${tempText(n)}  ${jobShort(n.job)}` : `${n.name}  offline`, sel === n.id);
      });
    },
  };
};
