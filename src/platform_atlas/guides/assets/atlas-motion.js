/* ══════════════════════════════════════════════════════════════════════
   PLATFORM ATLAS · SHARED GUIDE MOTION LAYER
   ----------------------------------------------------------------------
   anime.js choreography shared by every wizard-shell guide page
   (env-setup, tier-upgrade, architecture-form). Everything here is
   ADDITIVE and NON-INVASIVE: it wraps each page's existing global wizard
   functions and reads state from the DOM, never rewriting the original
   logic. Every effect guards on element existence, so a page that lacks a
   given piece (a checklist, a tier grid, a JSON preview) simply skips it.
   If anime.js is unavailable, or the visitor prefers reduced motion, this
   file returns early and the wizard behaves exactly as it did before —
   no animation, full functionality.

   Load order matters: assets/anime.min.js must run first (it exposes
   window.anime), then this file.

   Motion vocabulary (mirrors design/mockups/env-setup-anime-motion-
   concepts.html, plus two lifted from design/mockups/report-anime-20-
   concepts.html): field spring underline ·
   ambient hero drift · stepper advance pulse · card assembly · tier-
   select elastic settle · directional panel entrance · validation shake ·
   checklist draw-on · toast overshoot · live JSON reveal · generate-
   bundle button morph.
   ══════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var A = window.anime;
  var reduced = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Master switch. When off, leave every wizard function untouched.
  if (!A || reduced) return;

  var $ = function (id) { return document.getElementById(id); };

  // Suppress selection "pops" while a bundle is being bulk-loaded, so
  // reconstructing a saved environment doesn't fire a storm of animations.
  var loading = false;

  // Brand tokens — kept in sync with :root in assets/atlas-guide.css.
  var C = { accent: '#c94415', pass: '#6fa22c', fail: '#c0392b' };

  // Strip the inline opacity/transform anime leaves behind so the
  // stylesheet reclaims ownership of the element once motion settles.
  function clearInline(els, props) {
    var list = (els && els.nodeType) ? [els] : Array.prototype.slice.call(els || []);
    props = props || ['opacity', 'transform'];
    list.forEach(function (el) {
      if (!el) return;
      props.forEach(function (p) { el.style[p] = ''; });
    });
  }

  // Replace a global wizard function, keeping the original for delegation.
  function wrap(name, factory) {
    var orig = window[name];
    if (typeof orig !== 'function') return;
    window[name] = factory(orig);
  }

  // Which step-panel is currently on screen (read from the DOM so we never
  // depend on the wizard's lexically-scoped `state`).
  function activeStep() {
    var el = document.querySelector('.step-panel.active');
    return el ? parseInt(el.id.replace('step-', ''), 10) : 1;
  }

  // A "review" step is any panel that hosts the readiness checklist or the
  // live JSON preview — detected by content so the trigger works whatever
  // number the review step happens to be on a given page.
  function isReviewPanel(n) {
    var p = $('step-' + n);
    if (!p) return false;
    var cl = document.getElementById('checklist');
    var jp = document.getElementById('json-preview');
    return !!((cl && p.contains(cl)) || (jp && p.contains(jp)));
  }

  // ════════════════════════════════════════════════════════════════════
  // 0b · Field spring underline — wraps each .field-input in a span and
  //      springs an accent underline in/out on focus. Skips <select> — a
  //      dropdown has no caret to underline.
  //
  //      The wrap must NEVER happen while the input is focused: reparenting
  //      a focused node (insertBefore/appendChild) blurs it out from under
  //      the browser's own focus handling, so a field wrapped lazily on its
  //      first focusin loses that click's focus entirely — the "required"
  //      validator (wired to onblur) fires on the still-empty field before
  //      the user has typed anything, and the field needs a second click to
  //      actually start accepting input. Wrapping happens up front instead
  //      (an initial sweep, plus a MutationObserver for fields added later
  //      by a page's own re-render, e.g. switching deployment mode or
  //      adding a namespace row) so no input is ever moved while active.
  // ════════════════════════════════════════════════════════════════════
  function ensureFieldUnderline(input) {
    if (input.tagName !== 'INPUT' || input._underlineEl) return input._underlineEl || null;
    var wrapEl = document.createElement('span');
    wrapEl.className = 'field-underline-wrap';
    input.parentNode.insertBefore(wrapEl, input);
    wrapEl.appendChild(input);
    var ul = document.createElement('span');
    ul.className = 'field-underline';
    wrapEl.appendChild(ul);
    input._underlineEl = ul;
    return ul;
  }
  function wrapFieldUnderlines(root) {
    var inputs = (root || document).querySelectorAll('input.field-input');
    for (var i = 0; i < inputs.length; i++) ensureFieldUnderline(inputs[i]);
  }
  wrapFieldUnderlines();
  new MutationObserver(function (mutations) {
    mutations.forEach(function (m) {
      Array.prototype.forEach.call(m.addedNodes, function (node) {
        if (node.nodeType !== 1) return;
        if (node.matches && node.matches('input.field-input')) ensureFieldUnderline(node);
        else if (node.querySelectorAll) wrapFieldUnderlines(node);
      });
    });
  }).observe(document.body, { childList: true, subtree: true });
  document.addEventListener('focusin', function (e) {
    var el = e.target;
    if (!el.classList || !el.classList.contains('field-input')) return;
    var ul = el._underlineEl; // already wrapped by the sweep/observer above
    if (!ul) return;
    A.remove(ul);
    A.animate(ul, { scaleX: [0, 1], duration: 460, ease: 'outElastic(1, .6)' });
  });
  document.addEventListener('focusout', function (e) {
    var ul = e.target._underlineEl;
    if (!ul) return;
    A.remove(ul);
    A.animate(ul, { scaleX: [1, 0], duration: 280, ease: 'outQuad' });
  });

  // ════════════════════════════════════════════════════════════════════
  // 1 · Ambient sidebar drift — three independently-eased gradient blobs
  //     behind the dark step rail.
  // ════════════════════════════════════════════════════════════════════
  function startSidebarDrift() {
    if (!document.querySelector('.side-blob')) return;
    A.createTimeline({ loop: true, alternate: true, ease: 'inOutSine' })
      .add('.sb-a', { translateX: [0, 20], translateY: [0, 22], scale: [1, 1.16], duration: 7200 }, 0)
      .add('.sb-b', { translateX: [0, -18], translateY: [0, -18], scale: [1, 1.10], duration: 8400 }, 0)
      .add('.sb-c', { translateX: [0, 16], translateY: [0, -20], scale: [1, 1.2], duration: 6600 }, 0);
  }

  // ════════════════════════════════════════════════════════════════════
  // Intro — the whole card rises/fades in, then the sidebar brand + steps
  // cascade down the rail.
  // ════════════════════════════════════════════════════════════════════
  function playIntro() {
    var wiz = $('wiz');
    var side = document.querySelectorAll('.side-brand, .side-step');
    if (wiz) {
      A.set(wiz, { opacity: 0, translateY: 18, scale: 0.985 });
      A.animate(wiz, {
        opacity: [0, 1], translateY: [18, 0], scale: [0.985, 1],
        duration: 640, ease: 'outQuad', onComplete: function () { clearInline(wiz); }
      });
    }
    if (side.length) {
      A.set(side, { opacity: 0, translateX: -12 });
      A.animate(side, {
        opacity: [0, 1], translateX: [-12, 0],
        delay: A.stagger(60, { start: 240 }), duration: 420, ease: 'outQuad',
        onComplete: function () { clearInline(side); }
      });
    }
    // Safety net: anime's per-element `complete` can be missed for the last
    // staggered node in some engines, leaving an element stuck at opacity 0.
    // Force the resting state after the intro's max runtime regardless.
    setTimeout(function () { clearInline(wiz); clearInline(side); }, 1100);
  }

  // ════════════════════════════════════════════════════════════════════
  // 3 + 5 · Card assembly — cards rise/fade in from the direction of travel
  // ════════════════════════════════════════════════════════════════════
  function animatePanelCards(panel, dir) {
    if (!panel) return;
    var cards = panel.querySelectorAll('.card');
    if (!cards.length) return;
    A.remove(cards);
    A.set(cards, { opacity: 0, translateY: 14, translateX: (dir || 0) * 22, scale: 0.985 });
    A.animate(cards, {
      opacity: [0, 1], translateY: [14, 0], translateX: [(dir || 0) * 22, 0], scale: [0.985, 1],
      delay: A.stagger(80), duration: 520, ease: 'outQuad',
      onComplete: function () { clearInline(cards); }
    });
  }

  // ════════════════════════════════════════════════════════════════════
  // 2 · Stepper advance — the newly-active dot pulses into its ring
  // ════════════════════════════════════════════════════════════════════
  function pulseStepperDot(n) {
    var li = document.querySelector('.side-step[data-step="' + n + '"]');
    var num = li && li.querySelector('.side-num');
    if (!num) return;
    A.remove(num);
    A.animate(num, {
      scale: [1, 1.16, 1], duration: 460, ease: 'outElastic(1, .6)',
      onComplete: function () { clearInline(num, ['transform']); }
    });
  }

  // ── goToStep: directional card assembly + stepper pulse (concepts 2/3/5),
  //    plus the checklist draw-on and JSON reveal when Review opens.
  wrap('goToStep', function (orig) {
    return function (n) {
      var from = activeStep();
      orig(n);
      var dir = n >= from ? 1 : -1;
      animatePanelCards($('step-' + n), dir);
      if (n !== from) pulseStepperDot(n);
      if (isReviewPanel(n)) { animateChecklistIn(); animateJsonReveal(); }
    };
  });

  // ════════════════════════════════════════════════════════════════════
  // 4 · Tier selection — chosen card gives a small elastic settle
  // ════════════════════════════════════════════════════════════════════
  wrap('selectTier', function (orig) {
    return function (val) {
      var prev = document.querySelector('.tier-card.selected');
      orig(val);
      if (loading) return;
      var card = $('tier-' + val);
      if (!card || card === prev) return;
      A.remove(card);
      A.animate(card, {
        scale: [1, 1.05, 1], duration: 460, ease: 'outElastic(1, .55)',
        onComplete: function () { clearInline(card, ['transform']); }
      });
      var others = Array.prototype.slice.call(document.querySelectorAll('.tier-card'))
        .filter(function (c) { return c !== card; });
      if (others.length) A.animate(others, { opacity: [1, 0.78, 1], duration: 420, ease: 'outQuad' });
    };
  });

  // ════════════════════════════════════════════════════════════════════
  // 6 · Validation shake — a field flashes + shakes the moment it fails
  // ════════════════════════════════════════════════════════════════════
  wrap('showFieldError', function (orig) {
    return function (inputId, errId, msg) {
      var inp = $(inputId);
      var wasError = inp && inp.classList.contains('error');
      orig(inputId, errId, msg);
      // Only react on the transition INTO an error state — not on every
      // keystroke while the field is already invalid.
      if (loading || !msg || !inp || wasError) return;
      A.remove(inp);
      A.animate(inp, {
        translateX: [0, -7, 7, -5, 5, -2, 2, 0], duration: 420, ease: 'inOutSine',
        onComplete: function () { clearInline(inp, ['transform']); }
      });
      var err = $(errId);
      if (err) {
        A.remove(err);
        A.animate(err, {
          opacity: [0, 1], translateY: [-6, 0], duration: 260, ease: 'outQuad',
          onComplete: function () { clearInline(err); }
        });
      }
    };
  });

  // ════════════════════════════════════════════════════════════════════
  // 7 · Checklist — draw-on when Review opens; per-item feedback on change
  // ════════════════════════════════════════════════════════════════════
  function animateChecklistIn() {
    var items = document.querySelectorAll('#checklist .check-item');
    if (!items.length) return;
    A.remove(items);
    A.set(items, { opacity: 0, translateX: -8 });
    A.animate(items, {
      opacity: [0, 1], translateX: [-8, 0],
      delay: A.stagger(70), duration: 300, ease: 'outQuad',
      onComplete: function () { clearInline(items); }
    });
    var dots = document.querySelectorAll('#checklist .check-dot');
    if (dots.length) {
      A.remove(dots);
      A.set(dots, { scale: 0.5 });
      A.animate(dots, {
        scale: [0.5, 1], delay: A.stagger(70, { start: 80 }), duration: 360,
        ease: 'outBack(1.6)', onComplete: function () { clearInline(dots, ['transform']); }
      });
    }
  }

  var lastChecklist = {};
  wrap('renderChecklist', function (orig) {
    return function (checks, info) {
      var prev = lastChecklist;
      orig(checks, info);
      lastChecklist = {};
      (checks || []).forEach(function (c) { lastChecklist[c.text] = c.pass; });
      if (loading) return;
      var items = document.querySelectorAll('#checklist .check-item');
      (checks || []).forEach(function (c, i) {
        var was = prev[c.text];
        var dot = items[i] && items[i].querySelector('.check-dot');
        if (!dot) return;
        if (c.pass && was === false) {           // just turned green
          A.remove(dot);
          A.animate(dot, { scale: [0.5, 1], duration: 420, ease: 'outBack(1.8)',
              onComplete: function () { clearInline(dot, ['transform']); } });
        } else if (!c.pass && was === true) {    // just regressed
          A.remove(dot);
          A.animate(dot, { translateX: [0, -4, 4, -3, 3, 0], duration: 340, ease: 'inOutSine',
              onComplete: function () { clearInline(dot, ['transform']); } });
        }
      });
    };
  });

  // ════════════════════════════════════════════════════════════════════
  // 9 · Live JSON reveal — lines cascade in when Review opens, then the
  //     preview reverts to a plain text node so live edits stay cheap.
  // ════════════════════════════════════════════════════════════════════
  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function animateJsonReveal() {
    var pre = $('json-preview');
    if (!pre) return;
    var text = pre.textContent;
    var lines = text.split('\n');
    if (lines.length <= 1) return;
    pre.innerHTML = lines.map(function (l) {
      return '<span class="json-line" style="display:block;">' + escapeHtml(l || ' ') + '</span>';
    }).join('');
    var spans = pre.querySelectorAll('.json-line');
    A.remove(spans);
    A.set(spans, { opacity: 0, translateX: -8 });
    A.animate(spans, {
      opacity: [0, 1], translateX: [-8, 0],
      delay: A.stagger(22, { start: 60 }), duration: 220, ease: 'outQuad',
      onComplete: function () {
        // Restore a plain text node for cheap live updates — but only if a
        // live buildJSON() hasn't already repainted the preview mid-reveal.
        if (pre.querySelector('.json-line')) pre.textContent = text;
      }
    });
  }

  // ════════════════════════════════════════════════════════════════════
  // 8 · Toast — overshoot in, hold, ease out (owns its own timing so the
  //     stylesheet transition never double-animates it).
  // ════════════════════════════════════════════════════════════════════
  wrap('showToast', function (orig) {
    return function (msg) {
      var t = $('toast');
      if (!t) return orig(msg);
      t.textContent = msg;
      t.style.transition = 'none';
      A.remove(t);
      A.createTimeline()
        .add(t, { translateY: [24, 0], opacity: [0, 1], duration: 420, ease: 'outBack(1.4)' })
        .add(t, { opacity: 1, duration: 2000 })
        .add(t, {
          translateY: [0, 16], opacity: [1, 0], duration: 280, ease: 'inQuad',
          onComplete: function () { t.classList.remove('visible'); clearInline(t); }
        });
    };
  });

  // ════════════════════════════════════════════════════════════════════
  // 10 · Generate-bundle morph — the button narrates the async encryption:
  //      label → spinner → green checkmark, then resets so it's re-usable.
  // ════════════════════════════════════════════════════════════════════
  function busyBtn(btn) {
    if (!btn.dataset.origHtml) btn.dataset.origHtml = btn.innerHTML;
    btn.style.pointerEvents = 'none';
    btn.innerHTML =
      '<span class="atlas-spin" style="display:inline-block;width:13px;height:13px;' +
      'border:2px solid rgba(255,255,255,.4);border-top-color:#fff;border-radius:50%;"></span>' +
      '<span style="margin-left:8px;">Encrypting…</span>';
    var spin = btn.querySelector('.atlas-spin');
    A.remove(spin);
    btn._spin = A.animate(spin, { rotate: '1turn', loop: true, duration: 700, ease: 'linear' });
  }
  function doneBtn(btn) {
    if (btn._spin) { btn._spin.pause(); btn._spin = null; }
    btn.innerHTML =
      '<svg class="btn-icon" viewBox="0 0 16 16" style="stroke:#fff;fill:none;stroke-width:2;' +
      'stroke-linecap:round;stroke-linejoin:round;"><polyline points="3.5 8.5 6.5 11.5 12.5 5"/></svg>' +
      '<span style="margin-left:7px;">Bundle ready</span>';
    btn.style.backgroundColor = C.pass;
    A.remove(btn);
    A.animate(btn, { scale: [1, 1.04, 1], duration: 420, ease: 'outBack(1.6)',
        onComplete: function () { clearInline(btn, ['transform']); } });
  }
  function resetBtn(btn) {
    if (btn._spin) { btn._spin.pause(); btn._spin = null; }
    if (btn.dataset.origHtml != null) btn.innerHTML = btn.dataset.origHtml;
    btn.style.pointerEvents = '';
    btn.style.backgroundColor = '';
  }
  wrap('generateBundle', function (orig) {
    return function () {
      var btns = [$('download-btn'), $('btn-generate')]
        .filter(function (b) { return b && !b.disabled; });
      btns.forEach(busyBtn);
      var p;
      try { p = orig.apply(this, arguments); }
      catch (e) { btns.forEach(resetBtn); throw e; }
      return Promise.resolve(p).then(function (r) {
        btns.forEach(doneBtn);
        setTimeout(function () { btns.forEach(resetBtn); }, 2400);
        return r;
      }, function (err) { btns.forEach(resetBtn); throw err; });
    };
  });

  // ── Suppress the selection pops while a saved bundle is reconstructed. ──
  wrap('loadFromObj', function (orig) {
    return function (obj) {
      loading = true;
      try { orig(obj); }
      finally { setTimeout(function () { loading = false; }, 60); }
    };
  });

  // ════════════════════════════════════════════════════════════════════
  // INIT — runs once, after the wizard's own inline init() has executed.
  // ════════════════════════════════════════════════════════════════════
  startSidebarDrift();
  playIntro();
})();
