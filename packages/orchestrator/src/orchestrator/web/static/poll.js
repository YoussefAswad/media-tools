// The polling layer: every live panel on every page.
//
// A panel is any element with data-src. See the attribute table in the orchestrator README —
// this file is the only implementation of that contract.
//
// Exposes window.Panels for the page-specific scripts that load after it:
//   Panels.state        the shared filter state (q, app, page, range, start, end, units)
//   Panels.refresh(el)  re-fetch one panel now
//   Panels.refreshAll() re-fetch every panel now
//   Panels.activeData(groupId, key)  the active chip's data-<key> in a .filters group
//
// No external libraries, by design: the CSP forbids them and the whole UI is server-rendered.
(function () {
  var searchEl = document.getElementById("search");
  // default range comes from the server-marked active chip (e.g. "cutoff" when one exists)
  var _rf = document.getElementById("range-filter");
  var _active = _rf && _rf.querySelector(".chip.active");
  // display-unit preference (GB vs GiB) is remembered per browser
  var _units = "";
  try { _units = localStorage.getItem("dl-units") || ""; } catch (e) { /* ignore */ }
  var state = {
    q: "", app: "", page: 1,
    range: (_active && _active.dataset.range) || "30d",
    start: "", end: "",
    units: _units,
  };
  // reflect the remembered unit choice on the toggle before the first fetch
  var _uf = document.getElementById("unit-filter");
  if (_uf) {
    _uf.querySelectorAll(".chip").forEach(function (c) {
      c.classList.toggle("active", (c.dataset.units || "") === state.units);
    });
  }

  function activeData(id, key) {
    var box = id && document.getElementById(id);
    var active = box && box.querySelector(".chip.active");
    return active ? (active.dataset[key] || "") : "";
  }

  function buildUrl(el) {
    var base = el.dataset.src;
    var params = new URLSearchParams();
    if (el.hasAttribute("data-searchable") && state.q) params.set("q", state.q);
    if (state.app) params.set("app", state.app);
    if (el.hasAttribute("data-ranged")) {
      if (state.start || state.end) {
        if (state.start) params.set("start", state.start);
        if (state.end) params.set("end", state.end);
      } else {
        params.set("range", state.range);
      }
    }
    if (el.hasAttribute("data-unit-aware") && state.units) params.set("units", state.units);
    // Generic chip groups: every non-empty data-* on the active chip becomes a query param, so a
    // new filter dimension needs only a `.filters` group and a data-<name> on its chips. This
    // replaced two hardcoded variants (data-filtered read result+type, data-source-filtered read
    // source) that between them did nothing this doesn't.
    var groups = el.dataset.chipFilters;
    if (groups) {
      groups.split(",").forEach(function (id) {
        var box = document.getElementById(id.trim());
        var active = box && box.querySelector(".chip.active");
        if (!active) return;
        Object.keys(active.dataset).forEach(function (k) {
          if (active.dataset[k]) params.set(k, active.dataset[k]);
        });
      });
    }
    if (el.hasAttribute("data-paged") && state.page > 1) params.set("page", state.page);
    var qs = params.toString();
    return qs ? base + "?" + qs : base;
  }

  function refresh(el) {
    fetch(buildUrl(el), { headers: { "X-Requested-With": "poll" } })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (html === null || html === el._lastHtml) return;  // unchanged: don't touch the DOM
        // preserve scroll across the swap: horizontal on the inner table (on mobile the table
        // itself is the overflow-x scroller), and vertical on a .scroll-y container (the
        // monitoring container list refreshes its HTML every poll, which would otherwise jump
        // the reader back to the top)
        var scroller = el.querySelector("table.jobs, table.actions, table.ledger, table.library");
        // the vertical scroller is usually inside the panel, but a couple of panels carry
        // .scroll-y on themselves — that element survives the swap, so restore it directly
        var selfScrolls = el.classList.contains("scroll-y");
        var vscroller = selfScrolls ? el : el.querySelector(".scroll-y");
        var sx = scroller ? scroller.scrollLeft : 0;
        var sy = vscroller ? vscroller.scrollTop : 0;
        el.innerHTML = html;
        el._lastHtml = html;
        if (el._onSwap) el._onSwap();  // let a panel re-apply state the swap just wiped
        if (sx) {
          var next = el.querySelector("table.jobs, table.actions, table.ledger, table.library");
          if (next) next.scrollLeft = sx;
        }
        if (sy) {
          var vnext = selfScrolls ? el : el.querySelector(".scroll-y");
          if (vnext) vnext.scrollTop = sy;
        }
      })
      .catch(function () {});
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-src]"));
  function refreshAll() { panels.forEach(refresh); }

  // "refresh in Ns" countdown: any element with data-countdown-for="<panel id>" tracks that
  // panel's poll cycle, so the label always matches the real interval.
  function attachCountdown(el, interval) {
    if (!el.id) return null;
    var label = document.querySelector('[data-countdown-for="' + el.id + '"]');
    if (!label) return null;
    var next = Date.now() + interval;
    var tick = function () {
      var left = Math.max(0, Math.round((next - Date.now()) / 1000));
      label.textContent = "refresh in " + left + "s";
    };
    tick();
    setInterval(tick, 1000);
    return function () { next = Date.now() + interval; tick(); };
  }

  panels.forEach(function (el) {
    refresh(el);
    var interval = parseInt(el.dataset.interval || "3000", 10);
    if (interval > 0) {
      var reset = attachCountdown(el, interval);
      setInterval(function () {
        refresh(el);
        if (reset) reset();
      }, interval);
    }
  });

  if (searchEl) {
    var t;
    searchEl.addEventListener("input", function () {
      clearTimeout(t);
      t = setTimeout(function () {
        state.q = searchEl.value.trim();
        state.page = 1;  // a new query starts at the first page
        refreshAll();
      }, 250);
    });
  }

  // custom date range: date inputs override the preset range chips
  var fromEl = document.getElementById("date-from");
  var toEl = document.getElementById("date-to");
  var clearEl = document.getElementById("date-clear");
  function applyDates() {
    state.start = fromEl ? fromEl.value : "";
    state.end = toEl ? toEl.value : "";
    if (state.start || state.end) {
      var rf = document.getElementById("range-filter");
      if (rf) rf.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("active"); });
    }
    refreshAll();
  }
  if (fromEl) fromEl.addEventListener("change", applyDates);
  if (toEl) toEl.addEventListener("change", applyDates);
  if (clearEl) clearEl.addEventListener("click", function () {
    if (fromEl) fromEl.value = "";
    if (toEl) toEl.value = "";
    state.start = ""; state.end = "";
    var rf = document.getElementById("range-filter");
    var def = rf && rf.querySelector('[data-range="30d"]');
    if (rf) rf.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("active"); });
    if (def) def.classList.add("active");
    state.range = "30d";
    refreshAll();
  });

  // chip filter groups (app filter is global; result filter is per-panel)
  document.querySelectorAll(".filters").forEach(function (group) {
    group.addEventListener("click", function (e) {
      var btn = e.target.closest(".chip");
      if (!btn) return;
      e.preventDefault();
      group.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("active"); });
      btn.classList.add("active");
      state.page = 1;  // a changed filter invalidates the current page number
      if (group.id === "app-filter") state.app = btn.dataset.app || "";
      if (group.id === "unit-filter") {
        state.units = btn.dataset.units || "";
        try { localStorage.setItem("dl-units", state.units); } catch (e) { /* ignore */ }
      }
      if (group.id === "range-filter") {
        state.range = btn.dataset.range || "30d";
        state.start = ""; state.end = "";  // preset overrides a custom range
        if (fromEl) fromEl.value = "";
        if (toEl) toEl.value = "";
      }
      refreshAll();
    });
  });

  // close an open edit dialog when clicking outside it or pressing Escape. A native <details>
  // only closes by clicking its <summary> again; clicking the summary or inside the (fixed,
  // on mobile) form is still contained in the element, so those don't count as "outside".
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.edit[open]").forEach(function (d) {
      if (!d.contains(e.target)) d.removeAttribute("open");
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    document.querySelectorAll("details.edit[open]").forEach(function (d) {
      d.removeAttribute("open");
    });
  });

  window.Panels = {
    state: state,
    refresh: refresh,
    refreshAll: refreshAll,
    activeData: activeData,
  };
})();
