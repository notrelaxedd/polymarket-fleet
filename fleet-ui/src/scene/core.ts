// View 3: core and satellites. A living core heats up with the fleet's average
// temperature; each machine is a satellite on a beam, sparks flow while it works.
import * as THREE from 'three';
import { hashOf, statusOf } from '../fleet';
import { frac, makeLabel, makeOrb, setText, sprite, tintOrb } from './shared';
import type { Builder } from './shared';

const SPARK_WHITE = new THREE.Color(0xffffff);

export const core: Builder = (nodes) => {
  const group = new THREE.Group();
  const coreMat = new THREE.ShaderMaterial({
    uniforms: { uT: { value: 0 }, uA: { value: new THREE.Color(0x0b3a52) }, uB: { value: new THREE.Color(0x45c4f5) }, uHeat: { value: 0 } },
    vertexShader: `uniform float uT; varying float vD; varying vec3 vN; varying vec3 vV;
      void main(){ float d = sin(position.x*1.9+uT*1.3)*sin(position.y*2.3+uT)*sin(position.z*1.7+uT*.8);
        d += .5*sin(position.x*4.1-uT*1.7)*sin(position.z*3.7+uT*1.1); vD = d;
        vec3 p = position + normal*d*.34; vec4 mv = modelViewMatrix*vec4(p,1.);
        vN = normalize(normalMatrix*normal); vV = normalize(-mv.xyz); gl_Position = projectionMatrix*mv; }`,
    fragmentShader: `uniform vec3 uA; uniform vec3 uB; uniform float uHeat; varying float vD; varying vec3 vN; varying vec3 vV;
      void main(){ float f = pow(1.-abs(dot(normalize(vN), normalize(vV))), 2.2);
        vec3 hot = vec3(1., .62, .18); vec3 b = mix(uB, hot, uHeat);
        vec3 c = mix(uA, b*.55, smoothstep(-.7, 1., vD)) + f*vec3(.6,.85,1.)*.55; gl_FragColor = vec4(c, 1.); }`,
  });
  const coreMesh = new THREE.Mesh(new THREE.IcosahedronGeometry(3, 24), coreMat);
  const cage = new THREE.Mesh(new THREE.IcosahedronGeometry(4.4, 1), new THREE.MeshBasicMaterial({ color: 0x45c4f5, wireframe: true, transparent: true, opacity: 0.16 }));
  const cage2 = new THREE.Mesh(new THREE.IcosahedronGeometry(5.3, 2), new THREE.MeshBasicMaterial({ color: 0x45c4f5, wireframe: true, transparent: true, opacity: 0.05 }));
  const lab = makeLabel('lbl lbl-core');
  lab.position.set(0, 6.9, 0);
  group.add(coreMesh, cage, cage2, sprite(0x45c4f5, 16, 0.18), lab);

  // satellites on a Fibonacci sphere; the shell grows a little for a big fleet
  const R = 11 + Math.max(0, Math.sqrt(nodes.length) - 4) * 1.5;
  const items = nodes.map((n, k) => {
    const h = hashOf(n.id);
    const orb = makeOrb(n, 0.62, h);
    const y = 1 - ((k + 0.5) / nodes.length) * 2, r = Math.sqrt(1 - y * y), a = k * 2.399963;
    const dir = new THREE.Vector3(Math.cos(a) * r, y * 0.8, Math.sin(a) * r).normalize();
    orb.g.position.copy(dir).multiplyScalar(R);
    const beam = new THREE.Line(new THREE.BufferGeometry().setFromPoints([dir.clone().multiplyScalar(3.3), orb.g.position.clone()]), new THREE.LineBasicMaterial({ color: 0x45c4f5, transparent: true, opacity: 0.5, blending: THREE.AdditiveBlending }));
    const sparks = [0, 1, 2, 3].map(() => sprite(0xffffff, 0.55));
    group.add(orb.g, beam, ...sparks);
    return { orb, dir, beam, sparks, h };
  });

  return {
    group, cam: [0, 6 * (R / 11), 27 * (R / 11)], spin: 0.5,
    pick: items.map((i) => i.orb.mesh),
    update(t, dt, ns, sel) {
      coreMat.uniforms.uT.value = t;
      const on = ns.filter((n) => n.online);
      const warm = on.filter((n) => n.temp !== null);
      const avg = warm.length ? warm.reduce((s, n) => s + (n.temp ?? 0), 0) / warm.length : 45;
      coreMat.uniforms.uHeat.value = THREE.MathUtils.clamp((avg - 55) / 25, 0, 1);
      cage.rotation.y += dt * 0.12; cage.rotation.x += dt * 0.05;
      cage2.rotation.y -= dt * 0.06;
      setText(lab, `${on.length}/${ns.length} ONLINE`);
      ns.forEach((n, k) => {
        const it = items[k];
        if (!it) return;
        tintOrb(it.orb, n, t, sel);
        const st = statusOf(n), m = it.beam.material as THREE.LineBasicMaterial;
        m.color.copy(it.orb.mat.emissive);
        m.opacity = st === 'off' ? 0.06 : st === 'idle' ? 0.18 : 0.35 + (n.cpu / 100) * 0.5;
        const busy = st === 'ok' || st === 'hot';
        it.sparks.forEach((s, i) => {
          s.visible = busy;
          const f = frac(t * (0.15 + (n.cpu / 100) * 0.55) + i / 4 + it.h);
          s.position.copy(it.dir).multiplyScalar(R - f * (R - 3.3));
          (s.material as THREE.SpriteMaterial).color.copy(it.orb.mat.emissive).lerp(SPARK_WHITE, 0.5);
          (s.material as THREE.SpriteMaterial).opacity = Math.sin(f * Math.PI);
        });
      });
    },
  };
};
