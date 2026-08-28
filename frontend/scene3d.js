/* The two WebGL scenes on the landing page.
   Both are decoration: the canvases are aria-hidden and the page carries its
   whole meaning in text. So every failure path here ends in "show the flat mark
   and move on" rather than in an error — a missing GPU is not a broken page.

   The logo geometry is not loaded from the SVG; it is the same contours as
   assets/vertex-mark.svg, transcribed once into THREE.Shape below with the y
   axis flipped and the origin recentred. One drawing, two renderings — and no
   SVGLoader to ship for a shape that never changes. */
import * as THREE from "/static/vendor/three.module.js";

const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const NAVY = 0x101b3f;
const TEAL = 0x2fb3a8;
const GOLD = 0xc9a227;

/* SVG viewBox 0 0 520 270 → centred, y up. */
const fx = (x) => (x - 256) / 135;
const fy = (y) => (135 - y) / 135;

function markShapes() {
  const v = new THREE.Shape();
  v.moveTo(fx(0), fy(0));
  v.lineTo(fx(148), fy(0));
  v.lineTo(fx(272), fy(206));
  v.bezierCurveTo(fx(264), fy(238), fx(238), fy(260), fx(204), fy(258));
  v.bezierCurveTo(fx(176), fy(256), fx(156), fy(246), fx(148), fy(226));
  v.closePath();

  const a = new THREE.Shape();
  a.moveTo(fx(288), fy(192));
  a.lineTo(fx(354), fy(26));
  a.bezierCurveTo(fx(362), fy(4), fx(380), fy(0), fx(391), fy(20));
  a.lineTo(fx(512), fy(262));
  a.lineTo(fx(398), fy(262));
  a.bezierCurveTo(fx(366), fy(262), fx(342), fy(238), fx(332), fy(202));
  a.bezierCurveTo(fx(326), fy(182), fx(308), fy(180), fx(288), fy(192));
  a.closePath();

  return [v, a];
}

/** A renderer, or null if this browser cannot give us one. */
function makeRenderer(canvas) {
  try {
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.05;
    return renderer;
  } catch (failure) {
    return null;
  }
}

/** Run `frame(seconds)` only while `element` is on screen and the tab is visible. */
function driveWhileVisible(element, frame, renderOnce) {
  if (still) { renderOnce(); return; }

  let running = false;
  let onScreen = false;
  const clock = new THREE.Clock();

  const tick = () => {
    if (!running) return;
    frame(clock.getDelta());
    requestAnimationFrame(tick);
  };
  const sync = () => {
    const shouldRun = onScreen && !document.hidden;
    if (shouldRun === running) return;
    running = shouldRun;
    if (running) { clock.getDelta(); requestAnimationFrame(tick); }
  };

  if ("IntersectionObserver" in window) {
    new IntersectionObserver((entries) => {
      onScreen = entries[0].isIntersecting;
      sync();
    }, { threshold: 0 }).observe(element);
  } else {
    onScreen = true;
  }
  document.addEventListener("visibilitychange", sync);
  sync();
}

/** Keep the drawing buffer matched to the element's CSS size.
    `after` redraws once the box has changed — the still frame under reduced
    motion would otherwise be left blank by the next resize. */
function fitToBox(renderer, camera, canvas, after) {
  const resize = () => {
    const width = canvas.clientWidth || 1;
    const height = canvas.clientHeight || 1;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
    if (after) after();
  };
  resize();
  if ("ResizeObserver" in window) new ResizeObserver(resize).observe(canvas);
  else window.addEventListener("resize", resize);
}

/* ======================= hero: the extruded mark ======================= */
function heroScene() {
  const canvas = document.getElementById("heroCanvas");
  const hero = canvas && canvas.closest(".hero");
  if (!canvas) return;

  const renderer = makeRenderer(canvas);
  if (!renderer) { hero.classList.add("nogl"); return; }

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(32, 1, 0.1, 100);
  camera.position.set(0, 0, 9.2);

  const geometry = new THREE.ExtrudeGeometry(markShapes(), {
    depth: 0.34, curveSegments: 14,
    bevelEnabled: true, bevelThickness: 0.045, bevelSize: 0.035, bevelSegments: 4,
  });
  geometry.center();

  const mark = new THREE.Mesh(geometry, new THREE.MeshPhysicalMaterial({
    color: 0xf3f6ff, metalness: 0.32, roughness: 0.24,
    clearcoat: 0.9, clearcoatRoughness: 0.18,
  }));
  const group = new THREE.Group();
  group.add(mark);
  scene.add(group);

  // A faint dust field, so the mark sits in a space rather than on a flat wash.
  const dustCount = 320;
  const dust = new Float32Array(dustCount * 3);
  for (let i = 0; i < dustCount; i += 1) {
    dust[i * 3] = (Math.random() - 0.5) * 16;
    dust[i * 3 + 1] = (Math.random() - 0.5) * 10;
    dust[i * 3 + 2] = (Math.random() - 0.5) * 8 - 3;
  }
  const dustGeometry = new THREE.BufferGeometry();
  dustGeometry.setAttribute("position", new THREE.BufferAttribute(dust, 3));
  const dustField = new THREE.Points(dustGeometry, new THREE.PointsMaterial({
    color: 0x9fb4e8, size: 0.035, transparent: true, opacity: 0.5, depthWrite: false,
  }));
  scene.add(dustField);

  scene.add(new THREE.HemisphereLight(0xdfe8ff, NAVY, 1.1));
  const key = new THREE.DirectionalLight(0xffffff, 2.6);
  key.position.set(4, 5, 6);
  scene.add(key);
  const rim = new THREE.DirectionalLight(TEAL, 2.2);
  rim.position.set(-6, -2, 2);
  scene.add(rim);
  const glow = new THREE.PointLight(GOLD, 26, 22);
  glow.position.set(-2.5, 3.2, -4);
  scene.add(glow);

  const pointer = { x: 0, y: 0 };
  if (!still) {
    window.addEventListener("pointermove", (event) => {
      pointer.x = (event.clientX / window.innerWidth) * 2 - 1;
      pointer.y = (event.clientY / window.innerHeight) * 2 - 1;
    }, { passive: true });
  }

  let elapsed = 0;
  const frame = (delta) => {
    elapsed += delta;
    const narrow = canvas.clientWidth < 900;
    // The mark takes the side the text does not: in RTL the copy sits right, so
    // it goes left, and it slides across when the language toggle flips dir.
    const wanted = narrow ? 0 : (document.documentElement.dir === "rtl" ? -2 : 2);
    group.position.x += (wanted - group.position.x) * Math.min(1, delta * 3);
    // Sized to sit whole in its half of the hero. Bigger reads as an abstract
    // slab rather than as the logo, which defeats the point of extruding it.
    group.scale.setScalar(narrow ? 0.6 : 0.85);

    // The sway is deliberately shallow: past roughly fifteen degrees the V's
    // near face foreshortens and the monogram stops being legible.
    const sway = still ? 0 : Math.sin(elapsed * 0.34) * 0.2;
    group.rotation.y += ((sway + pointer.x * 0.3) - group.rotation.y) * Math.min(1, delta * 2.4);
    group.rotation.x += ((-pointer.y * 0.2) - group.rotation.x) * Math.min(1, delta * 2.4);
    group.position.y = Math.sin(elapsed * 0.6) * 0.09;

    dustField.rotation.y = elapsed * 0.02;
    key.intensity = narrow ? 1.4 : 2.6;
    renderer.render(scene, camera);
  };

  // Reduced motion still gets a picture, just not a moving one: the mark is
  // posed once, at the angle the animation spends most of its time near.
  const poseAndDraw = () => {
    group.position.x = canvas.clientWidth < 900
      ? 0 : (document.documentElement.dir === "rtl" ? -2 : 2);
    group.rotation.set(0.05, -0.2, 0);
    frame(0);
  };
  fitToBox(renderer, camera, canvas, still ? poseAndDraw : null);
  driveWhileVisible(hero, frame, poseAndDraw);
}

/* ============ transform: scattered page → aligned spreadsheet ============ */
function flowScene() {
  const canvas = document.getElementById("flowCanvas");
  const box = canvas && canvas.closest(".flowbox");
  if (!canvas) return;

  const renderer = makeRenderer(canvas);
  if (!renderer) { box.classList.add("nogl"); return; }

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100);
  camera.position.set(0, 0.2, 9);
  camera.lookAt(0, 0, 0);

  const columns = canvas.clientWidth < 520 ? 8 : 12;
  const rows = 10;
  const count = columns * rows;

  // Two arrangements for the same cells: the page as it arrives — text runs of
  // uneven length on a tilted sheet — and the table it becomes.
  const scattered = new Float32Array(count * 3);
  const aligned = new Float32Array(count * 3);
  const tint = new Float32Array(count * 3);
  const from = new THREE.Color(0xe8ecf7);
  const to = new THREE.Color(TEAL);

  for (let row = 0; row < rows; row += 1) {
    const runLength = 3 + Math.floor(Math.random() * (columns - 3));
    for (let column = 0; column < columns; column += 1) {
      const i = row * columns + column;
      const inRun = column < runLength;
      scattered[i * 3] = (column - columns / 2 + 0.5) * 0.34 - 0.6 + (Math.random() - 0.5) * 0.12;
      scattered[i * 3 + 1] = (rows / 2 - row - 0.5) * 0.42 + (Math.random() - 0.5) * 0.06;
      scattered[i * 3 + 2] = inRun ? 0 : -2.6 - Math.random() * 2;
      aligned[i * 3] = (column - columns / 2 + 0.5) * 0.46;
      aligned[i * 3 + 1] = (rows / 2 - row - 0.5) * 0.40;
      aligned[i * 3 + 2] = 0;
      const shade = from.clone().lerp(to, row === 0 ? 1 : column / columns * 0.55);
      tint[i * 3] = shade.r; tint[i * 3 + 1] = shade.g; tint[i * 3 + 2] = shade.b;
    }
  }

  const cells = new THREE.InstancedMesh(
    new THREE.BoxGeometry(0.34, 0.2, 0.05),
    new THREE.MeshStandardMaterial({ metalness: 0.1, roughness: 0.45, vertexColors: false }),
    count,
  );
  cells.instanceColor = new THREE.InstancedBufferAttribute(tint, 3);
  scene.add(cells);

  const sheet = new THREE.Mesh(
    new THREE.PlaneGeometry(5.6, 4.6),
    new THREE.MeshStandardMaterial({ color: 0x1a2648, roughness: 1 }),
  );
  scene.add(sheet);

  scene.add(new THREE.HemisphereLight(0xcddcff, NAVY, 1.3));
  const lamp = new THREE.DirectionalLight(0xffffff, 2.2);
  lamp.position.set(2, 4, 5);
  scene.add(lamp);

  const dummy = new THREE.Object3D();
  let progress = 0;
  let elapsed = 0;

  const frame = (delta) => {
    elapsed += delta;
    // Progress is read off the box's own position in the viewport, so the
    // transformation happens as the reader scrolls past the paragraph
    // describing it rather than on a timer they cannot see.
    const rect = box.getBoundingClientRect();
    const travel = window.innerHeight + rect.height;
    const seen = window.innerHeight - rect.top;
    const target = still ? 1 : Math.min(1, Math.max(0, (seen / travel - 0.18) / 0.5));
    progress += (target - progress) * Math.min(1, delta * 3.5);

    for (let i = 0; i < count; i += 1) {
      const ease = Math.min(1, Math.max(0, progress * 1.35 - (i % columns) * 0.02));
      dummy.position.set(
        scattered[i * 3] + (aligned[i * 3] - scattered[i * 3]) * ease,
        scattered[i * 3 + 1] + (aligned[i * 3 + 1] - scattered[i * 3 + 1]) * ease,
        scattered[i * 3 + 2] + (aligned[i * 3 + 2] - scattered[i * 3 + 2]) * ease,
      );
      dummy.rotation.set((1 - ease) * 0.5, (1 - ease) * 0.7, 0);
      dummy.scale.setScalar(0.7 + ease * 0.3);
      dummy.updateMatrix();
      cells.setMatrixAt(i, dummy.matrix);
    }
    cells.instanceMatrix.needsUpdate = true;

    const lean = (1 - progress) * 0.32;
    cells.rotation.set(lean * 0.6, -lean, lean * 0.25);
    sheet.rotation.copy(cells.rotation);
    sheet.position.z = -0.3;
    cells.position.y = Math.sin(elapsed * 0.5) * 0.04;

    renderer.render(scene, camera);
  };

  // Without motion the picture that carries the meaning is the finished table,
  // so the still frame is the end of the transformation, not its start.
  const settledDraw = () => { progress = 1; frame(0); };
  fitToBox(renderer, camera, canvas, still ? settledDraw : null);
  driveWhileVisible(box, frame, settledDraw);
}

heroScene();
flowScene();
