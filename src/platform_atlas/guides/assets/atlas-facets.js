/* ══════════════════════════════════════════════════════════════════════
   PLATFORM ATLAS · GUIDE CRYSTAL FACETS  —  atlas-facets.js
   ----------------------------------------------------------------------
   The report's crystal-facet crystal (report.html "Crystal facets", shared
   with the WebUI dashboard), drawn once on a fixed full-viewport canvas
   behind the guide: a low-poly mesh flat-shaded by a slowly orbiting light,
   present along the right edge and erased toward the middle of the page.

   Opt-in: needs <canvas class="atlas-facets" aria-hidden="true">. Every tone
   is a custom property on that canvas (--facet-*, see atlas-guide.css), the
   same names the report uses. Under reduced motion it paints one settled
   frame; it pauses while the tab is hidden and runs at ~30fps.
   ══════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';
  var canvas = document.querySelector('canvas.atlas-facets');
  if (!canvas || !canvas.getContext) return;
  var ctx = canvas.getContext('2d');
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var TAU = Math.PI * 2;
  var clamp = function (v, a, b) { return Math.min(b === undefined ? 1 : b, Math.max(a === undefined ? 0 : a, v)); };
  var lerp = function (a, b, t) { return a + (b - a) * t; };
  var ease = function (x) { return 1 - Math.pow(1 - clamp(x), 3); };
  var smooth = function (a, b, x) { var t = clamp((x - a) / (b - a)); return t * t * (3 - 2 * t); };
  function rng(seed) {
    var a = seed >>> 0;
    return function () {
      a = (a + 0x6d2b79f5) >>> 0; var t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function hash(n) { var x = Math.sin(n * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); }
  function noise(x, y, z) {
    var xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z), s = function (t) { return t * t * (3 - 2 * t); };
    var u = s(x - xi), w = s(y - yi), q = s(z - zi), v = function (a, b, c) { return hash(a * 57 + b * 113 + c * 271); };
    var x00 = lerp(v(xi, yi, zi), v(xi + 1, yi, zi), u), x10 = lerp(v(xi, yi + 1, zi), v(xi + 1, yi + 1, zi), u);
    var x01 = lerp(v(xi, yi, zi + 1), v(xi + 1, yi, zi + 1), u), x11 = lerp(v(xi, yi + 1, zi + 1), v(xi + 1, yi + 1, zi + 1), u);
    return lerp(lerp(x00, x10, w), lerp(x01, x11, w), q);
  }

  // Any CSS colour → [r,g,b], via a 1×1 canvas (normalises every syntax).
  var probe = document.createElement('canvas'); probe.width = probe.height = 1;
  var pctx = probe.getContext('2d', { willReadFrequently: true });
  function color(value, fallback) {
    if (!value || !pctx) return fallback;
    pctx.clearRect(0, 0, 1, 1); pctx.fillStyle = '#000'; pctx.fillStyle = value.trim(); pctx.fillRect(0, 0, 1, 1);
    var d = pctx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2]];
  }

  function makeMesh(w, h) {
    var cols = Math.round(clamp(w / 92, 8, 18)), rows = Math.round(clamp(h / 70, 5, 9)), r = rng(24), pts = [], tris = [];
    for (var j = 0; j <= rows; j++) for (var i = 0; i <= cols; i++) {
      var jx = i > 0 && i < cols ? (r() - 0.5) * 0.72 : 0, jy = j > 0 && j < rows ? (r() - 0.5) * 0.72 : 0;
      pts.push({ u: (i + jx) / cols, v: (j + jy) / rows, ph: r() * TAU });
    }
    for (var jj = 0; jj < rows; jj++) for (var ii = 0; ii < cols; ii++) {
      var a = jj * (cols + 1) + ii, b = a + 1, c = a + cols + 1, d = c + 1;
      if ((ii + jj) % 2) tris.push([a, b, d], [a, d, c]); else tris.push([a, b, c], [b, d, c]);
    }
    return { pts: pts, tris: tris, cell: w / cols };
  }

  var st = { w: 0, h: 0, mesh: null, key: '', time: 0, intro: null, running: false, tone: null };

  function build() {
    var dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    st.w = window.innerWidth; st.h = window.innerHeight;
    if (!st.w || !st.h) return;
    canvas.width = Math.round(st.w * dpr); canvas.height = Math.round(st.h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    var key = Math.round(st.w / 92) + 'x' + Math.round(st.h / 70);
    if (key !== st.key) { st.mesh = makeMesh(st.w, st.h); st.key = key; }
    var cs = getComputedStyle(canvas), num = function (k, d) { var v = parseFloat(cs.getPropertyValue(k)); return isNaN(v) ? d : v; };
    st.tone = {
      a: color(cs.getPropertyValue('--facet-a'), [224, 168, 103]),
      b: color(cs.getPropertyValue('--facet-b'), [77, 180, 234]),
      lo: color(cs.getPropertyValue('--facet-lo'), [10, 16, 30]),
      hi: color(cs.getPropertyValue('--facet-hi'), [23, 34, 57]),
      edge: color(cs.getPropertyValue('--facet-edge'), [233, 237, 245]),
      alpha: num('--facet-alpha', 0.45), tint: num('--facet-tint', 0.15),
      edgeA: num('--facet-edge-alpha', 0.02), edgeSpec: num('--facet-edge-spec', 0.05),
      fadeBottom: num('--facet-fade-bottom', 0),
      gone: num('--facet-sweep-gone', 0.4), solid: num('--facet-sweep-solid', 0.85)
    };
  }

  function draw(dt) {
    var w = st.w, h = st.h, m = st.mesh, T = st.tone;
    if (!w || !h || !m || !T) return;
    st.time += dt; var t = st.time;
    if (st.intro === null) st.intro = t;
    var intro = reduce ? 1 : clamp((t - st.intro) / 1.8);
    var lx = Math.cos(t * 0.3), ly = Math.sin(t * 0.3) * 0.6, lz = 0.9;
    var ll = Math.hypot(lx, ly, lz); lx /= ll; ly /= ll; lz /= ll;
    var lift = ease(intro / 0.9) * (m.cell / 70);
    var P = m.pts.map(function (p) {
      return [p.u * w * 1.02 - w * 0.01, p.v * h * 1.04 - h * 0.02,
        (Math.sin(t * 0.35 + p.ph) * 40 + noise(p.u * 3, p.v * 3, t * 0.06) * 90) * lift];
    });
    ctx.clearRect(0, 0, w, h);
    // The sweep erases everything left of `gone`, so skip faces that can't show.
    var skipBelow = (T.gone - 0.05);
    for (var n = 0; n < m.tris.length; n++) {
      var tri = m.tris[n], A = P[tri[0]], B = P[tri[1]], C = P[tri[2]];
      var mx = (A[0] + B[0] + C[0]) / 3 / w;
      if (mx < skipBelow) continue;
      var appear = reduce ? 1 : ease((intro * 1.6 - (1 - mx) * 0.6) / 0.6);
      if (appear <= 0) continue;
      var ux = B[0] - A[0], uy = B[1] - A[1], uz = B[2] - A[2], vx = C[0] - A[0], vy = C[1] - A[1], vz = C[2] - A[2];
      var nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx, nl = Math.hypot(nx, ny, nz) || 1;
      nx /= nl; ny /= nl; nz /= nl; if (nz < 0) { nx = -nx; ny = -ny; nz = -nz; }
      var d = clamp(nx * lx + ny * ly + nz * lz);
      var tint = Math.min(T.tint, Math.pow(d, 16) * 0.55 + Math.pow(d, 5) * 0.14);
      var k = smooth(0.45, 0.82, mx);
      var hi0 = lerp(T.a[0], T.b[0], k), hi1 = lerp(T.a[1], T.b[1], k), hi2 = lerp(T.a[2], T.b[2], k);
      var b0 = lerp(T.lo[0], T.hi[0], d), b1 = lerp(T.lo[1], T.hi[1], d), b2 = lerp(T.lo[2], T.hi[2], d);
      ctx.beginPath(); ctx.moveTo(A[0], A[1]); ctx.lineTo(B[0], B[1]); ctx.lineTo(C[0], C[1]); ctx.closePath();
      ctx.fillStyle = 'rgba(' + Math.round(lerp(b0, hi0, tint)) + ',' + Math.round(lerp(b1, hi1, tint)) + ',' +
        Math.round(lerp(b2, hi2, tint)) + ',' + (T.alpha * appear) + ')';
      ctx.fill();
      ctx.strokeStyle = 'rgba(' + T.edge.join(',') + ',' + ((T.edgeA + Math.pow(d, 16) * T.edgeSpec) * appear) + ')';
      ctx.lineWidth = 0.8; ctx.stroke();
    }
    ctx.save(); ctx.globalCompositeOperation = 'destination-out';
    // Soft landing at the bottom of the viewport.
    if (T.fadeBottom > 0) {
      var y0 = h * (1 - T.fadeBottom), gy = ctx.createLinearGradient(0, y0, 0, h);
      gy.addColorStop(0, 'rgba(0,0,0,0)'); gy.addColorStop(1, 'rgba(0,0,0,1)');
      ctx.fillStyle = gy; ctx.fillRect(0, y0, w, h - y0);
    }
    // Right → left sweep: solid from `solid` rightward, gone left of `gone`.
    var goneX = w * T.gone, solidX = w * T.solid; if (solidX <= goneX) solidX = goneX + 1;
    var gx = ctx.createLinearGradient(goneX, 0, solidX, 0);
    gx.addColorStop(0, 'rgba(0,0,0,1)'); gx.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = gx; ctx.fillRect(0, 0, solidX, h);
    ctx.restore();
  }

  function run() {
    build();
    if (reduce) { draw(9); return; }
    if (st.running || document.hidden) return;
    st.running = true;
    var last = performance.now(), acc = 0;
    (function loop(now) {
      if (!st.running) return;
      var dt = Math.min(0.1, (now - last) / 1000); last = now; acc += dt;
      if (acc >= 1 / 30) { draw(acc); acc = 0; }   // ~30fps is plenty for a slow orbit
      requestAnimationFrame(loop);
    })(last);
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) st.running = false; else run();
  });
  var rz;
  window.addEventListener('resize', function () {
    clearTimeout(rz);
    rz = setTimeout(function () { build(); if (!st.running) draw(0); }, 120);
  });
  run();
})();
