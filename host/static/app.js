/* Fleet dashboard: role select auto-submit, KILL, retire and cancel-all confirms, copy buttons, fragment refresh.
   Everything works without this file; it only removes clicks and keeps the page fresh. */
(function () {
  "use strict";
  var FLEET_MS = 5000, TOPBAR_MS = 10000, FOCUS_HOLD_MS = 15000, FLASH_MS = 8000;
  var inflight = false, focusedAt = 0, loaded = Date.now();
  var ok = {}, lost = {};
  document.documentElement.classList.add("js");
  document.body.classList.add("js");

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
    if (el && el.matches && el.matches("select[data-autosubmit]") && el.form) {
      inflight = true;
      el.blur();
      el.form.submit();
    }
  });

  document.addEventListener("click", function (e) {
    var kill = e.target.closest ? e.target.closest("a[data-kill]") : null;
    if (kill) {
      e.preventDefault();
      if (window.confirm("Kill trading now? Trade workers stop and their open orders are cancelled.")) {
        inflight = true;
        document.getElementById("kill-form").submit();
      }
      return;
    }
    var copy = e.target.closest ? e.target.closest("button[data-copy]") : null;
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
        target.innerHTML = html;
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
    if (e.persisted) { inflight = false; loaded = Date.now(); refreshAll(); }
  });

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
