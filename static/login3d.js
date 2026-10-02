(function () {
  var canvas = document.getElementById('scene');
  if (!canvas || typeof THREE === 'undefined') return;
  var renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true, alpha: true });
  } catch (e) { canvas.style.display = 'none'; return; }

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

  var scene = new THREE.Scene();
  var camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100);
  camera.position.set(0, 0, 9);

  // Lights: warm key, cool rim, soft fill
  scene.add(new THREE.HemisphereLight(0x9fb4cc, 0x1a1008, 0.9));
  var key = new THREE.PointLight(0xff8a3a, 90, 30); key.position.set(5, 4, 6); scene.add(key);
  var rim = new THREE.PointLight(0x6fa8ff, 60, 30); rim.position.set(-6, -3, 4); scene.add(rim);
  var top = new THREE.DirectionalLight(0xffffff, 0.8); top.position.set(0, 6, 3); scene.add(top);

  // Brake rotor
  var rotor = new THREE.Group();
  var steel = new THREE.MeshStandardMaterial({ color: 0xaab2bb, metalness: 0.85, roughness: 0.32 });
  var dark = new THREE.MeshStandardMaterial({ color: 0x3a424c, metalness: 0.8, roughness: 0.5 });

  function circle(path, r, cw) { path.absarc(0, 0, r, 0, Math.PI * 2, !!cw); return path; }

  // Friction ring with two staggered rings of drilled holes
  var ringShape = new THREE.Shape(); circle(ringShape, 2.6);
  ringShape.holes.push(circle(new THREE.Path(), 1.55, true));
  [[2.25, 24, 0], [1.9, 24, Math.PI / 24]].forEach(function (cfg) {
    for (var i = 0; i < cfg[1]; i++) {
      var a = cfg[2] + (i / cfg[1]) * Math.PI * 2, h = new THREE.Path();
      h.absarc(Math.cos(a) * cfg[0], Math.sin(a) * cfg[0], 0.085, 0, Math.PI * 2, true);
      ringShape.holes.push(h);
    }
  });
  var ring = new THREE.Mesh(new THREE.ExtrudeGeometry(ringShape, {
    depth: 0.22, bevelEnabled: true, bevelSize: 0.03, bevelThickness: 0.03, bevelSegments: 2, curveSegments: 48
  }), steel);
  ring.position.z = -0.11; rotor.add(ring);

  // Hat with bolt holes and centre bore
  var hatShape = new THREE.Shape(); circle(hatShape, 1.5);
  hatShape.holes.push(circle(new THREE.Path(), 0.45, true));
  for (var b = 0; b < 5; b++) {
    var ba = (b / 5) * Math.PI * 2, bh = new THREE.Path();
    bh.absarc(Math.cos(ba) * 0.95, Math.sin(ba) * 0.95, 0.13, 0, Math.PI * 2, true);
    hatShape.holes.push(bh);
  }
  var hat = new THREE.Mesh(new THREE.ExtrudeGeometry(hatShape, {
    depth: 0.35, bevelEnabled: true, bevelSize: 0.03, bevelThickness: 0.03, curveSegments: 48
  }), dark);
  hat.position.z = 0.1; rotor.add(hat);

  // Wall connecting hat to ring
  var wall = new THREE.Mesh(new THREE.CylinderGeometry(1.58, 1.58, 0.3, 64, 1, true), dark);
  wall.rotation.x = Math.PI / 2; wall.position.z = 0.0; rotor.add(wall);

  // Amber glow ring behind the rotor
  var glow = new THREE.Mesh(new THREE.TorusGeometry(3.0, 0.025, 12, 128),
    new THREE.MeshBasicMaterial({ color: 0xf0771f, transparent: true, opacity: 0.55 }));
  glow.position.z = -0.6; scene.add(glow);

  rotor.rotation.set(-0.35, 0.55, 0);
  scene.add(rotor);

  // Drifting dust
  var N = 160, pos = new Float32Array(N * 3);
  for (var i = 0; i < N; i++) {
    pos[i * 3] = (Math.random() - 0.5) * 18;
    pos[i * 3 + 1] = (Math.random() - 0.5) * 11;
    pos[i * 3 + 2] = (Math.random() - 0.5) * 8 - 2;
  }
  var pg = new THREE.BufferGeometry();
  pg.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  var dust = new THREE.Points(pg, new THREE.PointsMaterial({ color: 0xf0771f, size: 0.045, transparent: true, opacity: 0.5 }));
  scene.add(dust);

  // Sizing
  function resize() {
    var w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.position.z = w / h < 1 ? 13 : 9.5;
    camera.updateProjectionMatrix();
  }
  window.addEventListener('resize', resize); resize();

  // Pointer parallax
  var tx = 0, ty = 0;
  window.addEventListener('pointermove', function (e) {
    tx = (e.clientX / window.innerWidth - 0.5) * 0.6;
    ty = (e.clientY / window.innerHeight - 0.5) * 0.4;
  });

  var clock = new THREE.Clock();
  function frame() {
    var t = clock.getElapsedTime();
    if (!reduce) {
      rotor.rotation.z = -t * 0.35;
      glow.rotation.z = t * 0.1;
      dust.rotation.y = t * 0.02;
    }
    rotor.position.y = reduce ? 0 : Math.sin(t * 0.8) * 0.12;
    camera.position.x += (tx * 2 - camera.position.x) * 0.04;
    camera.position.y += (-ty * 2 - camera.position.y) * 0.04;
    camera.lookAt(0, 0, 0);
    renderer.render(scene, camera);
    requestAnimationFrame(frame);
  }
  frame();
})();
