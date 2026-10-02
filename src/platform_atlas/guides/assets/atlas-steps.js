/* ══════════════════════════════════════════════════════════════════════
   PLATFORM ATLAS · GUIDE STEP TRANSITION  —  atlas-steps.js
   ----------------------------------------------------------------------
   Shared by the wizard guides (env-setup, tier-upgrade, architecture-form).
   AtlasSteps.swap(current, next, dir) replaces the old instant panel toggle:
   the outgoing panel fades out (.leaving, ~130ms), the page jumps to the top
   while nothing is visible, then the incoming panel slides in from the
   direction of travel (.entering; dir 1 forward, -1 back). The motion itself
   is CSS in atlas-guide.css and uses opacity + transform only.

   Loaded in <head>, with no dependencies, so a page's own init can call it
   immediately. Each page's goToStep() falls back to a plain toggle if this
   file is missing. Reduced motion skips the exit fade.
   ══════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';
  var EXIT_MS = 130;
  var pending = null;   // an in-flight swap, finished first so a fast second click can't strand a panel

  function swap(current, next, dir) {
    if (!next) return;
    if (pending) pending();
    var timer = null;
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function go() {
      clearTimeout(timer);
      pending = null;
      if (current && current !== next) current.classList.remove('active', 'leaving');
      // 'instant', not 'auto': the stylesheet sets scroll-behavior:smooth, which 'auto' would inherit.
      if (window.scrollY > 0) window.scrollTo({ top: 0, behavior: 'instant' });
      next.style.setProperty('--dir', dir);
      next.classList.add('active');
      Array.prototype.forEach.call(document.querySelectorAll('.step-panel.entering'), function (el) {
        el.classList.remove('entering');
      });
      if (current && current !== next) {
        next.classList.add('entering');
        setTimeout(function () { next.classList.remove('entering'); }, 720);
      }
    }

    if (!current || current === next || reduce) { go(); return; }
    current.classList.add('leaving');
    pending = go;
    timer = setTimeout(go, EXIT_MS);
  }

  window.AtlasSteps = { swap: swap };
})();
