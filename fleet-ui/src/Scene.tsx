// The 3D stage: one WebGL renderer, bloom, CSS labels and orbit controls showing the
// Data city (./scene/city.ts). Nodes are read through `nodesRef` every frame; the city
// is rebuilt when the layout key (the worker ids in order) changes.
import { useEffect, useRef } from 'react';
import type { RefObject } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js';
import { CSS2DRenderer, CSS2DObject } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import type { FleetNode } from './fleet';
import { GLOW, RING, initTextures } from './scene/shared';
import type { View } from './scene/shared';
import { city } from './scene/city';

/** What the city's geometry depends on: the worker ids in order (one tower each). */
export const layoutKeyOf = (nodes: FleetNode[]) => nodes.map((n) => n.id).join(',');

function disposeView(v: View) {
  v.group.traverse((o) => {
    if (o instanceof CSS2DObject) o.element.remove();
    const m = o as THREE.Mesh;
    m.geometry?.dispose();
    const mats = Array.isArray(m.material) ? m.material : m.material ? [m.material] : [];
    mats.forEach((x) => {
      const tex = x as unknown as { map?: THREE.Texture | null; emissiveMap?: THREE.Texture | null };
      [tex.map, tex.emissiveMap].forEach((t) => { if (t && t !== GLOW && t !== RING) t.dispose(); });
      x.dispose();
    });
  });
}

interface Props {
  nodesRef: RefObject<FleetNode[]>;
  layoutKey: string;
  selected: string | null;
  onSelect: (id: string) => void;
}

export default function Scene({ nodesRef, layoutKey, selected, onSelect }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const selRef = useRef(selected);
  const pickRef = useRef(onSelect);
  const keyRef = useRef(layoutKey);
  selRef.current = selected;
  pickRef.current = onSelect;
  keyRef.current = layoutKey;

  useEffect(() => {
    const el = host.current!;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
    } catch {
      el.classList.add('no-gl');
      return;
    }
    initTextures();
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 0.95;
    el.appendChild(renderer.domElement);
    const labels = new CSS2DRenderer();
    labels.domElement.className = 'labels';
    el.appendChild(labels.domElement);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x04070b);
    scene.fog = new THREE.FogExp2(0x04070b, 0.008);
    const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 400);
    scene.add(new THREE.AmbientLight(0x8fb8cc, 0.9));
    const key = new THREE.DirectionalLight(0xffffff, 1.6);
    key.position.set(8, 14, 10);
    scene.add(key);

    const starPos: number[] = [];
    for (let i = 0; i < 1600; i++) { const v = new THREE.Vector3().randomDirection().multiplyScalar(70 + Math.random() * 90); starPos.push(v.x, v.y, v.z); }
    const starGeo = new THREE.BufferGeometry();
    starGeo.setAttribute('position', new THREE.Float32BufferAttribute(starPos, 3));
    const stars = new THREE.Points(starGeo, new THREE.PointsMaterial({ color: 0x9fc4d6, size: 0.45, transparent: true, opacity: 0.8, fog: false }));
    scene.add(stars);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.enablePan = false;
    controls.minDistance = 10;
    controls.maxDistance = 95;
    const calm = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    const composer = new EffectComposer(renderer);
    composer.addPass(new RenderPass(scene, camera));
    composer.addPass(new UnrealBloomPass(new THREE.Vector2(1, 1), 0.6, 0.65, 0.3));
    composer.addPass(new OutputPass());

    let current: View | null = null;
    let builtKey = '';
    let touched = false;
    /** (Re)build the city. A rebuild after a layout change keeps the camera once the
     * owner has moved it, and reframes it otherwise. */
    const build = () => {
      const again = current !== null;
      if (current) { scene.remove(current.group); disposeView(current); }
      builtKey = keyRef.current;
      current = city(nodesRef.current ?? [], renderer);
      scene.add(current.group);
      const target = new THREE.Vector3(...(current.target ?? [0, 0, 0]));
      const dist = target.distanceTo(new THREE.Vector3(...current.cam));
      controls.maxDistance = Math.max(95, dist * 1.6);
      if (again && touched) {
        // keep the owner's angle and zoom, follow the new centre
        camera.position.add(target.clone().sub(controls.target));
        controls.target.copy(target);
      } else {
        const narrow = window.innerWidth > 1020 ? 1.28 : el.clientWidth < 620 ? 1.2 : 1;
        camera.position.set(...current.cam).sub(target).multiplyScalar(narrow).add(target);
        controls.target.copy(target);
        if (!again) controls.autoRotate = !calm && !!current.spin;
      }
      controls.autoRotateSpeed = current.spin ?? 0;
      controls.maxPolarAngle = current.maxPolar ?? Math.PI;
      controls.update();
    };
    build();

    const resize = () => {
      const w = el.clientWidth, h = el.clientHeight;
      if (!w || !h) return;
      renderer.setSize(w, h);
      labels.setSize(w, h);
      composer.setSize(w, h);
      camera.aspect = w / h;
      if (window.innerWidth > 1020) camera.setViewOffset(w, h, 28, Math.min(120, h * 0.14), w, h);
      else camera.clearViewOffset();
      camera.updateProjectionMatrix();
    };
    const ro = new ResizeObserver(resize);
    ro.observe(el);
    resize();

    const ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
    let downAt = [0, 0];
    const hit = (e: PointerEvent) => {
      const r = renderer.domElement.getBoundingClientRect();
      ptr.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
      ray.setFromCamera(ptr, camera);
      const h = current ? ray.intersectObjects(current.pick, false)[0] : undefined;
      return h ? (h.object.userData.id as string) : null;
    };
    const onDown = (e: PointerEvent) => { downAt = [e.clientX, e.clientY]; controls.autoRotate = false; touched = true; };
    const onWheel = () => { touched = true; };
    const onUp = (e: PointerEvent) => {
      if (Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 6) return;
      const id = hit(e);
      if (id) pickRef.current(id);
    };
    const onMove = (e: PointerEvent) => { renderer.domElement.style.cursor = hit(e) ? 'pointer' : 'grab'; };
    renderer.domElement.addEventListener('pointerdown', onDown);
    renderer.domElement.addEventListener('pointerup', onUp);
    renderer.domElement.addEventListener('pointermove', onMove);
    renderer.domElement.addEventListener('wheel', onWheel, { passive: true });

    const clock = new THREE.Clock();
    let raf = 0, t = 0;
    const loop = () => {
      raf = requestAnimationFrame(loop);
      const dt = Math.min(0.05, clock.getDelta()) * (calm ? 0.25 : 1);
      t += dt;
      stars.rotation.y += dt * 0.004;
      if (keyRef.current !== builtKey) build();
      current?.update(t, dt, nodesRef.current ?? [], selRef.current);
      controls.update();
      composer.render();
      labels.render(scene, camera);
    };
    loop();

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      renderer.domElement.removeEventListener('pointerdown', onDown);
      renderer.domElement.removeEventListener('pointerup', onUp);
      renderer.domElement.removeEventListener('pointermove', onMove);
      renderer.domElement.removeEventListener('wheel', onWheel);
      if (current) disposeView(current);
      starGeo.dispose();
      controls.dispose();
      composer.dispose();
      renderer.dispose();
      el.replaceChildren();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);


  return <div ref={host} className="scene" role="img" aria-label="Live 3D view of the fleet. The machine list beside it has the same information." />;
}
