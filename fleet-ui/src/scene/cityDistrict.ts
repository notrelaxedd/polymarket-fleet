// The data city's surroundings: sky, street grid, filler buildings (kept off the tower
// and mast lots), the river with bridges, street lights, traffic and aircraft.
import * as THREE from 'three';
import { GLOW, frac, sprite } from './shared';

interface DistrictOpts {
  G: number;
  /** "i,j" lots that must stay free (towers and the mast plaza). */
  taken: Set<string>;
  /** z of the river's centre line. */
  riverZ: number;
  /** The low-rise area around the towers. */
  near: { x0: number; x1: number; z0: number; z1: number };
}

const rnd = (k: number) => frac(Math.sin(k * 91.7 + 3.1) * 43758.5453);

export function buildDistrict(group: THREE.Group, { G, taken, riverZ, near }: DistrictOpts) {
  // sky: dark overhead, teal haze and a warm glow on one side of the horizon
  group.add(new THREE.Mesh(new THREE.SphereGeometry(200, 32, 16), new THREE.ShaderMaterial({
    side: THREE.BackSide, depthWrite: false,
    vertexShader: `varying vec3 vP; void main(){ vP = position; gl_Position = projectionMatrix*modelViewMatrix*vec4(position,1.); }`,
    fragmentShader: `varying vec3 vP; void main(){ vec3 d = normalize(vP); float h = clamp(d.y, 0., 1.);
      vec3 c = mix(vec3(.012,.022,.035), vec3(.03,.15,.21), pow(1.-h, 5.));
      c += vec3(.38,.2,.07)*pow(1.-h, 16.)*smoothstep(-.3,.9,d.x)*.55; gl_FragColor = vec4(c, 1.); }`,
  })));

  // ground: one street-grid tile repeated so roads line up with the lots
  const tc = document.createElement('canvas');
  tc.width = tc.height = 256;
  const tg = tc.getContext('2d')!;
  tg.fillStyle = '#070d12'; tg.fillRect(0, 0, 256, 256);
  tg.fillStyle = '#0f222d'; tg.fillRect(28, 28, 200, 200);
  tg.strokeStyle = '#2b5a72'; tg.lineWidth = 2; tg.strokeRect(29, 29, 198, 198);
  tg.strokeStyle = '#0f2531'; tg.lineWidth = 1;
  tg.beginPath(); tg.moveTo(128, 30); tg.lineTo(128, 226); tg.moveTo(30, 128); tg.lineTo(226, 128); tg.stroke();
  tg.fillStyle = '#5aa6c9';
  for (let p = 36; p < 220; p += 24) { tg.fillRect(p, 0, 12, 2); tg.fillRect(p, 254, 12, 2); tg.fillRect(0, p, 2, 12); tg.fillRect(254, p, 2, 12); }
  const gtex = new THREE.CanvasTexture(tc);
  gtex.wrapS = gtex.wrapT = THREE.RepeatWrapping;
  gtex.repeat.set(60, 60);
  gtex.offset.y = 0.5;
  gtex.anisotropy = 8;
  gtex.colorSpace = THREE.SRGBColorSpace;
  const ground = new THREE.Mesh(new THREE.CircleGeometry(195, 72), new THREE.MeshStandardMaterial({ map: gtex, roughness: 0.75, metalness: 0.25 }));
  ground.rotation.x = -Math.PI / 2;
  group.add(ground);

  // surrounding district: low-rise near the towers, taller skyline further out
  const wc = document.createElement('canvas');
  wc.width = wc.height = 64;
  const wg = wc.getContext('2d')!;
  wg.fillStyle = '#000'; wg.fillRect(0, 0, 64, 64);
  for (let r = 0; r < 8; r++) for (let c = 0; c < 6; c++) {
    const v = rnd(r * 13 + c * 7 + 1);
    wg.fillStyle = v < 0.12 ? '#8a8a8a' : '#0c0c0c';
    wg.fillRect(4 + c * 10, 4 + r * 7.6, 6, 4);
  }
  const wtex = new THREE.CanvasTexture(wc);
  wtex.magFilter = THREE.NearestFilter;
  const fSide = new THREE.MeshStandardMaterial({ color: 0x1a2227, roughness: 0.95, metalness: 0, emissive: 0x8a9aa3, emissiveMap: wtex, emissiveIntensity: 0.1 });
  const fRoof = new THREE.MeshStandardMaterial({ color: 0x222c32, roughness: 0.95 });
  group.add(new THREE.HemisphereLight(0x8fc4dc, 0x0a1218, 1.4));
  const box = new THREE.BoxGeometry(1, 1, 1);
  box.translate(0, 0.5, 0);
  const inRiver = (z: number) => Math.abs(z - riverZ) < G;
  const spots: number[][] = [];
  for (let i = -23; i <= 22; i++) for (let j = -23; j <= 23; j++) {
    const x = (i + 0.5) * G, z = j * G, r = Math.hypot(x, z), k = i * 131 + j * 17;
    if (r > 150) continue;
    if (taken.has(`${i},${j}`)) continue;                     // tower lots and the mast plaza
    if (inRiver(z)) continue;                                  // river
    if (rnd(k) > (r > 90 ? 0.62 : 0.9)) continue;              // empty lots
    const isNear = x > near.x0 && x < near.x1 && z > near.z0 && z < near.z1;
    let h = isNear ? 0.4 + rnd(k + 1) * 0.8 : 0.8 + rnd(k + 1) * 2.6;
    if (!isNear && r > 48 && rnd(k + 2) > 0.86) h = 4 + rnd(k + 3) * 4;
    const w = 2.2 + rnd(k + 4) * 1.9, d = 2.2 + rnd(k + 5) * 1.9;
    spots.push([x + (rnd(k + 6) - 0.5) * 0.6, z + (rnd(k + 7) - 0.5) * 0.6, w, h, d]);
    // an annex beside it, only out in the skyline (the near area stays low and tidy)
    if (!isNear && rnd(k + 8) > 0.6) spots.push([x + 1.4, z - 1.3, 1.4, h * 0.55 + 0.4, 1.4]);
  }
  const filler = new THREE.InstancedMesh(box, [fSide, fSide, fRoof, fRoof, fSide, fSide], Math.max(1, spots.length));
  filler.count = spots.length;
  const dummy = new THREE.Object3D();
  spots.forEach(([x, z, w, h, d], i) => { dummy.position.set(x, 0, z); dummy.scale.set(w, h, d); dummy.updateMatrix(); filler.setMatrixAt(i, dummy.matrix); });
  group.add(filler);

  // river with moving glints, and three bridges on the road lines
  const riverMat = new THREE.ShaderMaterial({
    uniforms: { uT: { value: 0 } },
    vertexShader: `varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix*modelViewMatrix*vec4(position,1.); }`,
    fragmentShader: `uniform float uT; varying vec2 vUv; void main(){
      float w = sin(vUv.x*260. + uT*1.1 + sin(vUv.y*12.+uT*.8)*2.5)*sin(vUv.y*34. - uT*.6);
      float edge = smoothstep(0.,.12,vUv.y)*smoothstep(1.,.88,vUv.y);
      vec3 c = vec3(.008,.05,.075) + vec3(.1,.38,.5)*smoothstep(.8,1.,w)*.55*edge;
      c *= smoothstep(1.,.5, abs(vUv.x-.5)*2.); gl_FragColor = vec4(c, 1.); }`,
  });
  const river = new THREE.Mesh(new THREE.PlaneGeometry(380, 2 * G - 1.4), riverMat);
  river.rotation.x = -Math.PI / 2;
  river.position.set(0, 0.03, riverZ);
  group.add(river);
  const bridgeMat = new THREE.MeshStandardMaterial({ color: 0x14303d, roughness: 0.6 });
  [-2 * G, G, 6 * G].forEach((x) => {
    const b = new THREE.Mesh(new THREE.BoxGeometry(1.5, 0.22, 2 * G - 1), bridgeMat);
    b.position.set(x, 0.22, riverZ);
    group.add(b);
    [-1, 1].forEach((s) => { const l = sprite(0xffd6a0, 0.9, 0.7); l.position.set(x, 0.7, riverZ + s * 4.6); group.add(l); });
  });

  // street lights at intersections
  const lamps: number[] = [];
  for (let i = -13; i <= 13; i++) for (let j = -13; j <= 12; j++) {
    const z = (j + 0.5) * G;
    if (Math.abs(z - riverZ) < 1) continue;
    lamps.push(i * G, 0.55, z);
  }
  const lampGeo = new THREE.BufferGeometry();
  lampGeo.setAttribute('position', new THREE.Float32BufferAttribute(lamps, 3));
  group.add(new THREE.Points(lampGeo, new THREE.PointsMaterial({ map: GLOW, color: 0xffd6a0, size: 1.9, transparent: true, opacity: 0.75, blending: THREE.AdditiveBlending, depthWrite: false })));

  // traffic on the streets and a few aircraft overhead
  const cars = Array.from({ length: 90 }, (_, i) => {
    const s = sprite(i % 3 === 0 ? 0x45c4f5 : 0xdcecf5, 0.42, 0.85);
    group.add(s);
    const alongX = i % 2 === 0;
    const lane = alongX ? (Math.floor(rnd(i) * 9) - 4.5) * G : Math.floor(rnd(i) * 13 - 6) * G;
    const skip = alongX && Math.abs(lane - riverZ) < 4;
    return { s, alongX, lane: skip ? riverZ + 10 * G : lane, sp: (0.02 + rnd(i + 50) * 0.035) * (i % 4 < 2 ? 1 : -1), ph: rnd(i + 99), side: i % 4 < 2 ? 0.28 : -0.28 };
  });
  const planes = [0, 1, 2].map((i) => { const s = sprite(i === 1 ? 0xffd6a0 : 0xdcecf5, 0.9, 1); group.add(s); return { s, r: 42 + i * 16, y: 15 + i * 4, sp: 0.05 - i * 0.012, ph: i * 2.1 }; });

  return {
    update(t: number) {
      riverMat.uniforms.uT.value = t;
      cars.forEach((q) => {
        const u = (frac(q.ph + t * q.sp) - 0.5) * 120;
        if (q.alongX) q.s.position.set(u, 0.16, q.lane + q.side); else q.s.position.set(q.lane + q.side, 0.16, u);
        (q.s.material as THREE.SpriteMaterial).opacity = 0.85 * (1 - Math.abs(u) / 60);
      });
      planes.forEach((q) => {
        const a = q.ph + t * q.sp;
        q.s.position.set(Math.cos(a) * q.r, q.y, Math.sin(a) * q.r);
        (q.s.material as THREE.SpriteMaterial).opacity = Math.sin(t * 5 + q.ph) > 0.3 ? 1 : 0.15;
      });
    },
  };
}
