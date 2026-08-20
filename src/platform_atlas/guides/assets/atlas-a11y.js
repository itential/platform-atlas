/* ══════════════════════════════════════════════════════════════════════
   PLATFORM ATLAS · SHARED GUIDE ACCESSIBILITY LAYER
   ----------------------------------------------------------------------
   Loaded by every wizard-shell guide page (env-setup, tier-upgrade,
   architecture-form). Deliberately SEPARATE from atlas-motion.js, which
   returns early when the visitor prefers reduced motion — none of the
   behaviour here is decorative, so none of it may be skipped.

   Every choice control in these wizards paints its state with a class
   (.selected on a card, .checked on a toggle box, .open on a disclosure,
   .active on a sidebar step). The pages' own handlers set those classes
   and know nothing about ARIA. Rather than edit dozens of call sites —
   and risk the two drifting apart the next time one is touched — this
   observes class changes and mirrors them into the matching ARIA state.
   One source of truth, no duplicated bookkeeping.

   It also does two things the pages could not express in markup alone:
   re-validates a field the moment an existing error is corrected, and
   keeps aria-invalid in step with the .error class.
   ══════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var $$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  /* ── Mirror painted state into announced state ──────────────────── */
  function syncStates() {
    $$('[role="radio"]').forEach(function (el) {
      el.setAttribute('aria-checked', el.classList.contains('selected') ? 'true' : 'false');
    });

    $$('[role="switch"]').forEach(function (el) {
      // The visual is a .toggle-box that gains .checked; the box may sit
      // anywhere inside the control.
      var box = el.querySelector('.toggle-box');
      var on = box ? box.classList.contains('checked') : el.classList.contains('checked');
      el.setAttribute('aria-checked', on ? 'true' : 'false');
    });

    $$('.advanced-toggle').forEach(function (el) {
      el.setAttribute('aria-expanded', el.classList.contains('open') ? 'true' : 'false');
    });

    $$('.side-step').forEach(function (li) {
      var btn = li.querySelector('.side-step-btn');
      if (!btn) return;
      if (li.classList.contains('active')) btn.setAttribute('aria-current', 'step');
      else btn.removeAttribute('aria-current');
      // A step that cannot be reached yet is genuinely unavailable.
      var reachable = li.classList.contains('reachable') || li.classList.contains('active');
      btn.disabled = !reachable;
    });

    $$('.stepper-item').forEach(function (el) {
      if (el.classList.contains('active')) el.setAttribute('aria-current', 'step');
      else el.removeAttribute('aria-current');
    });

    $$('.field-input').forEach(function (el) {
      if (el.classList.contains('error')) el.setAttribute('aria-invalid', 'true');
      else el.removeAttribute('aria-invalid');
    });

    // These pages rebuild whole sections (HA node rows, extra namespaces) by
    // assigning innerHTML, so error slots that appear later need this too —
    // doing it once at startup only covered the ones present at load.
    $$('.field-error, .pp-error').forEach(function (el) {
      if (!el.getAttribute('role')) el.setAttribute('role', 'alert');
    });
  }

  /* Class changes are the trigger. We only ever write aria-* attributes,
     so this can never re-enter itself. */
  var mo = new MutationObserver(syncStates);

  function start() {
    syncStates();
    if (document.body) {
      mo.observe(document.body, { subtree: true, attributes: true, attributeFilter: ['class'] });
    }

    /* ── Clear an error the moment it is fixed ─────────────────────────
       These pages validate on blur, which is right — telling someone their
       hostname is wrong while they are still typing it is hostile. But it
       left a corrected field still painted red until the next blur. Once a
       field has errored, re-run its own validator on input. */
    document.addEventListener('input', function (e) {
      var el = e.target;
      if (!el || !el.classList || !el.classList.contains('error')) return;
      if (typeof el.onblur === 'function') el.onblur(e);
    }, true);

    /* MutationObserver callbacks are microtasks, so a control clicked by
       script would read its old ARIA state for one turn. Syncing on click as
       well makes the announced state correct immediately. */
    document.addEventListener('click', syncStates, false);
    document.addEventListener('change', syncStates, false);

    /* ── Power guard (not accessibility, but it belongs in the always-on
       file rather than the motion layer, which is skipped under reduced
       motion) ──────────────────────────────────────────────────────────
       The ambient aurora is two full-viewport layers on an infinite
       transform animation. It has no reason to keep running while the tab
       is in the background. */
    document.addEventListener('visibilitychange', function () {
      document.documentElement.classList.toggle('atlas-hidden', document.hidden);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
