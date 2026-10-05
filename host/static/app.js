/* Fleet dashboard: role select auto-submit, KILL, retire and cancel-all confirms, copy buttons, fragment refresh,
   remembered <details> (intros, disclosures), action menus and the kind switch on the job form.
   Everything works without this file; it only removes clicks and keeps the page fresh. */
(function () {
  "use strict";
  var FLEET_MS = 5000, TOPBAR_MS = 10000, FOCUS_HOLD_MS = 15000, FLASH_MS = 8000;
  var STORE_PREFIX = "fleet.details.";
  var inflight = false, focusedAt = 0, loaded = Date.now();
  var ok = {}, lost = {}, opened = {};
  document.documentElement.classList.add("js");
  document.body.classList.add("js");

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
  function remember(root) {
    var list = keyed(root);
    for (var i = 0; i < list.length; i++) { opened[list[i].getAttribute("data-key")] = list[i].open; }
  }
  function restore(root) {
    var list = keyed(root);
    for (var i = 0; i < list.length; i++) {
      var key = list[i].getAttribute("data-key");
      var want = Object.prototype.hasOwnProperty.call(opened, key) ? opened[key] : stored(key);
      if (want !== null && want !== undefined && list[i].open !== want) { list[i].open = want; }
    }
  }
  // toggle does not bubble: listen in the capture phase
  document.addEventListener("toggle", function (e) {
    var d = e.target;
    if (!d || d.tagName !== "DETAILS") { return; }
    var key = d.getAttribute("data-key");
    if (key) {
      opened[key] = d.open;
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
        remember(target);
        target.innerHTML = html;
        restore(target);
        applySwitches(target);
        lost[id] = false;
        ok[id] = Date.now();
        if (id === "topbar-status") {
          var bar = document.getElementById("topbar");
          if (bar) { bar.classList.toggle("killed", !!target.querySelector("[data-killed]")); }
        }
      })
      .catch(function () { lost[id] = true; });
  }

  function tick() {
    var el = document.getElementById("updated");
    if (!el) { return; }
    var down = !!(lost["fleet-grid"] || lost["trading-live"] || lost["topbar-status"]);
    // Freshness is the page's own region (fleet grid or trading region): a held region must not read as fresh.
    var since = ok[document.getElementById("fleet-grid") ? "fleet-grid" : document.getElementById("trading-live") ? "trading-live" : "topbar-status"] || loaded;
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
  applySwitches(document);

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
})();
