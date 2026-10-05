/* Fleet dashboard: role select auto-submit, KILL, retire and cancel-all confirms, copy buttons, fragment refresh,
   remembered <details> (intros, disclosures), #anchor links (open the group they name, pick the job kind, land
   below the sticky bar), action menus and the kind switch on the job form.
   Everything works without this file; it only removes clicks and keeps the page fresh. */
(function () {
  "use strict";
  var FLEET_MS = 5000, TOPBAR_MS = 10000, FOCUS_HOLD_MS = 15000, FLASH_MS = 8000;
  var STORE_PREFIX = "fleet.details.";
  var inflight = false, focusedAt = 0, loaded = Date.now();
  // opened: the owner's choice per data-key (a toggle by hand, or a link that revealed it); shown: the state this
  // script last set or saw, so the toggle events it caused (or a server-rendered open fired) are not taken as a
  // choice; served: whether the server forced the group open at the last restore
  var ok = {}, lost = {}, opened = {}, shown = {}, served = {};
  var has = function (o, k) { return Object.prototype.hasOwnProperty.call(o, k); };
  document.documentElement.classList.add("js");
  document.body.classList.add("js");

  // ---- the sticky top bar's height (html scroll-padding-top), so a link to #anchor lands below the bar
  function measureBar() {
    var bar = document.getElementById("topbar");
    if (bar) { document.documentElement.style.setProperty("--topbar-h", bar.offsetHeight + "px"); }
  }
  window.addEventListener("resize", measureBar);

  // ---- remembered <details>: open state by data-key, across fragment refreshes (memory) and reloads (localStorage)
  function stored(key) {
    try {
      var v = window.localStorage.getItem(STORE_PREFIX + key);
      return v === "1" ? true : v === "0" ? false : null;
    } catch (err) { return null; }
  }
  function store(key, isOpen) {
    try { window.localStorage.setItem(STORE_PREFIX + key, isOpen ? "1" : "0"); } catch (err) { /* no storage: memory only */ }
  }
  function keyed(root) {
    return root && root.querySelectorAll ? root.querySelectorAll("details[data-key]") : [];
  }
  // the element the URL fragment names (a banner links to /trading#exchange), or null
  function hashTarget() {
    var h = window.location.hash;
    if (!h || h.length < 2) { return null; }
    try { return document.getElementById(decodeURIComponent(h.slice(1))); } catch (err) { return null; }
  }
  // the disclosure an id names: the details itself, or the one its <section id> wraps (/trading#exchange)
  function owned(t) {
    if (!t) { return null; }
    if (t.tagName === "DETAILS") { return t; }
    for (var c = t.firstElementChild; c; c = c.nextElementSibling) {
      if (c.tagName === "DETAILS" && !c.classList.contains("menu")) { return c; }
    }
    return null;
  }
  // a stored "closed" never hides an inline error or the element the URL points at
  function pinned(d) {
    if (d.querySelector(".inline-error, [aria-invalid=\"true\"]")) { return true; }
    var t = hashTarget();
    return !!(t && (d === t || d.contains(t) || d === owned(t)));
  }
  // A group the server renders open for a reason (data-server-open: an error, the kill, exchange down with live
  // orders, ledger problems, a model's Assign) stays open on load and when it newly opens on a refresh; only a fold
  // by hand after that closes it again. Otherwise the owner's choice wins, then the stored one, then the server's.
  function restore(root) {
    var list = keyed(root);
    for (var i = 0; i < list.length; i++) {
      var d = list[i], key = d.getAttribute("data-key"), want;
      var forced = d.open && d.hasAttribute("data-server-open");
      if (forced && !served[key]) { delete opened[key]; }
      served[key] = forced;
      if (has(opened, key)) {
        want = opened[key];
      } else {
        want = stored(key);
        if (want === false && d.open && (forced || pinned(d))) { want = null; }
      }
      if (want !== null && want !== undefined && d.open !== want) { d.open = want; }
      shown[key] = d.open;
    }
  }
  // open every <details> around the URL fragment's target (and the disclosure a section id wraps), so a link
  // to a section shows it; true when it opened one
  function reveal(d) {
    if (!d || d.open || d.tagName !== "DETAILS" || d.classList.contains("menu")) { return false; }
    d.open = true;
    var key = d.getAttribute("data-key");
    if (key) { opened[key] = true; shown[key] = true; }
    return true;
  }
  function revealTarget() {
    var t = hashTarget(), changed = reveal(owned(t));
    for (var el = t; el; el = el.parentElement) { changed = reveal(el) || changed; }
    return changed;
  }
  // toggle does not bubble: listen in the capture phase. Only a change the script did not make is the owner's
  // choice (the parser and a fragment swap fire toggle for every details rendered open, the script for its own).
  document.addEventListener("toggle", function (e) {
    var d = e.target;
    if (!d || d.tagName !== "DETAILS") { return; }
    var key = d.getAttribute("data-key");
    if (key && shown[key] !== d.open) {
      shown[key] = opened[key] = d.open;
      store(key, d.open);
    }
    if (d.open && d.classList.contains("menu")) { closeMenus(d); }
  }, true);

  // ---- action menus: one open at a time, closed by a click outside or Escape
  function closeMenus(except) {
    var menus = document.querySelectorAll("details.menu[open]");
    for (var i = 0; i < menus.length; i++) {
      if (menus[i] !== except) { menus[i].open = false; }
    }
  }
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") { return; }
    var open = document.querySelector("details.menu[open]");
    if (open) {
      open.open = false;
      var s = open.querySelector("summary");
      if (s) { s.focus(); }
    }
  });

  // ---- the kind switch: a select[data-switch] shows only the [data-switch-for] parts whose data-when lists its value
  function applySwitch(select) {
    var name = select.getAttribute("data-switch"), value = select.value;
    var parts = document.querySelectorAll('[data-switch-for="' + name + '"]');
    for (var i = 0; i < parts.length; i++) {
      var when = (parts[i].getAttribute("data-when") || "").split(/\s+/);
      parts[i].hidden = when.indexOf(value) < 0;
    }
  }
  function applySwitches(root) {
    var selects = root.querySelectorAll ? root.querySelectorAll("select[data-switch]") : [];
    for (var i = 0; i < selects.length; i++) { applySwitch(selects[i]); }
  }
  // a link to one of the switched parts (/jobs#model_search) picks that kind in its select; true when it did
  function switchToTarget() {
    var t = hashTarget(), part = t && t.closest ? t.closest("[data-switch-for]") : null;
    if (!part) { return false; }
    var name = part.getAttribute("data-switch-for"), kind = (part.getAttribute("data-when") || "").split(/\s+/)[0];
    var selects = document.querySelectorAll("select[data-switch]"), changed = false;
    for (var i = 0; i < selects.length; i++) {
      var s = selects[i], before = s.value;
      if (s.getAttribute("data-switch") !== name || !kind || before === kind) { continue; }
      s.value = kind;
      if (s.value !== kind) { s.value = before; continue; }
      applySwitch(s);
      changed = true;
    }
    return changed;
  }
  // open and switch around the URL fragment's target; when that (or, on load, hiding the other kinds) moved the
  // layout, bring the target back to just under the top bar (html scroll-padding-top)
  function showTarget(onLoad) {
    var moved = revealTarget();
    moved = switchToTarget() || moved;
    if (onLoad) { applySwitches(document); }
    var t = hashTarget();
    if ((moved || onLoad) && t && t.scrollIntoView) { t.scrollIntoView(); }
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form && form.getAttribute && form.getAttribute("data-confirm") && !window.confirm(form.getAttribute("data-confirm"))) {
      e.preventDefault();
      return;
    }
    inflight = true;
  });
  document.addEventListener("focusin", function () { focusedAt = Date.now(); });

  document.addEventListener("change", function (e) {
    var el = e.target;
    if (el && el.matches && el.matches("select[data-switch]")) {
      applySwitch(el);
      return;
    }
    if (el && el.matches && el.matches("select[data-autosubmit]") && el.form) {
      inflight = true;
      el.blur();
      el.form.submit();
    }
  });

  document.addEventListener("click", function (e) {
    var t = e.target;
    if (t && t.closest && !t.closest("details.menu")) { closeMenus(null); }
    var kill = t && t.closest ? t.closest("a[data-kill]") : null;
    if (kill) {
      e.preventDefault();
      if (window.confirm("Kill trading now? Trade workers stop and their open orders are cancelled.")) {
        inflight = true;
        document.getElementById("kill-form").submit();
      }
      return;
    }
    var copy = t && t.closest ? t.closest("button[data-copy]") : null;
    if (copy && navigator.clipboard) {
      var src = document.getElementById(copy.getAttribute("data-copy"));
      if (!src) { return; }
      navigator.clipboard.writeText(src.textContent.trim()).then(function () {
        var old = copy.textContent;
        copy.textContent = "Copied";
        setTimeout(function () { copy.textContent = old; }, 1500);
      });
    }
  });

  function busy(target) {
    var a = document.activeElement;
    if (inflight) { return true; }
    // an open action menu inside the region holds the refresh: the owner is choosing an action
    if (target.querySelector("details.menu[open]")) { return true; }
    if (!a || !target.contains(a)) { return false; }
    if (a.tagName === "INPUT" || a.tagName === "TEXTAREA") { return true; }
    // A select keeps focus after its picker is dismissed (the link forms on /trading): hold the refresh only while it is likely open.
    return a.tagName === "SELECT" && Date.now() - focusedAt < FOCUS_HOLD_MS;
  }

  function refresh(id, url) {
    var target = document.getElementById(id);
    if (!target || busy(target)) { return; }
    fetch(url, { credentials: "same-origin", headers: { "Accept": "text/html" } })
      .then(function (r) { if (!r.ok) { throw new Error(String(r.status)); } return r.text(); })
      .then(function (html) {
        if (busy(target)) { return; }
        // the owner's choices are already in `opened` (the toggle handler); the swap restores them
        target.innerHTML = html;
        restore(target);
        applySwitches(target);
        lost[id] = false;
        ok[id] = Date.now();
        if (id === "topbar-status") {
          var bar = document.getElementById("topbar");
          if (bar) { bar.classList.toggle("killed", !!target.querySelector("[data-killed]")); }
          measureBar();
        }
      })
      .catch(function () { lost[id] = true; });
  }

  function tick() {
    // Home's region (#home-live: stats, Needs attention, Recent) refreshes from here every FLEET_MS, like the fleet grid;
    // tick.homeAt is the last attempt, so a failed or held fetch is retried after FLEET_MS, not every second.
    var home = document.getElementById("home-live");
    if (home && Date.now() - (tick.homeAt || loaded) >= FLEET_MS) {
      tick.homeAt = Date.now();
      refresh("home-live", "/fragments/home");
    }
    var el = document.getElementById("updated");
    if (!el) { return; }
    var down = !!(lost["fleet-grid"] || lost["trading-live"] || lost["home-live"] || lost["topbar-status"]);
    // Freshness is the page's own region (fleet grid, trading region or Home): a held region must not read as fresh.
    var own = document.getElementById("fleet-grid") ? "fleet-grid" : document.getElementById("trading-live") ? "trading-live" : home ? "home-live" : "topbar-status";
    var since = ok[own] || loaded;
    el.textContent = down ? "connection lost" : "updated " + Math.max(0, Math.round((Date.now() - since) / 1000)) + " s ago";
    el.classList.toggle("lost", down);
    document.body.classList.toggle("conn-lost", down);
  }

  function refreshAll() {
    refresh("fleet-grid", "/fragments/fleet");
    refresh("trading-live", "/fragments/trading");
    refresh("topbar-status", "/fragments/topbar");
  }

  window.addEventListener("pageshow", function (e) {
    if (e.persisted) { inflight = false; loaded = Date.now(); closeMenus(null); refreshAll(); }
  });

  restore(document);
  window.addEventListener("hashchange", function () { showTarget(false); });

  var flash = document.querySelector(".flash");
  if (flash) { setTimeout(function () { flash.remove(); }, FLASH_MS); }

  if (document.getElementById("fleet-grid")) {
    setInterval(function () { refresh("fleet-grid", "/fragments/fleet"); }, FLEET_MS);
  }
  if (document.getElementById("trading-live")) {
    setInterval(function () { refresh("trading-live", "/fragments/trading"); }, FLEET_MS);
  }
  if (document.getElementById("topbar-status")) {
    setInterval(function () { refresh("topbar-status", "/fragments/topbar"); }, TOPBAR_MS);
  }
  setInterval(tick, 1000);
  tick();
  // the first tick filled the freshness line under the bar: measure the bar, then open, switch and land on the
  // URL fragment's target
  measureBar();
  showTarget(true);
})();
