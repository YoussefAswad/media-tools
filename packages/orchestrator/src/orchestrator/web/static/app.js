// Self-contained UI logic — no external libraries (CSP-safe).
(function () {
  // ---- live-refreshing panels with search + filters ----------------------------------
  var searchEl = document.getElementById("search");
  // default range comes from the server-marked active chip (e.g. "cutoff" when one exists)
  var _rf = document.getElementById("range-filter");
  var _active = _rf && _rf.querySelector(".chip.active");
  // display-unit preference (GB vs GiB) is remembered per browser
  var _units = "";
  try { _units = localStorage.getItem("dl-units") || ""; } catch (e) { /* ignore */ }
  var state = {
    q: "", app: "",
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
    var fid = el.dataset.filtered;
    if (fid) {
      var res = activeData(fid, "result");
      if (res) params.set("result", res);
      var typ = activeData(fid, "type");
      if (typ) params.set("type", typ);
    }
    var sid = el.dataset.sourceFiltered;
    if (sid) {
      var src = activeData(sid, "source");
      if (src) params.set("source", src);
    }
    if (el.hasAttribute("data-unit-aware") && state.units) params.set("units", state.units);
    var qs = params.toString();
    return qs ? base + "?" + qs : base;
  }

  function refresh(el) {
    fetch(buildUrl(el), { headers: { "X-Requested-With": "poll" } })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (html === null || html === el._lastHtml) return;  // unchanged: don't touch the DOM
        // preserve the inner table's horizontal scroll across the swap (on mobile the table
        // itself is the overflow-x scroller, so a naive innerHTML reset would jump it to 0)
        var scroller = el.querySelector("table.jobs, table.actions, table.ledger");
        var sx = scroller ? scroller.scrollLeft : 0;
        el.innerHTML = html;
        el._lastHtml = html;
        if (sx) {
          var next = el.querySelector("table.jobs, table.actions, table.ledger");
          if (next) next.scrollLeft = sx;
        }
      })
      .catch(function () {});
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-src]"));
  function refreshAll() { panels.forEach(refresh); }

  panels.forEach(function (el) {
    refresh(el);
    var interval = parseInt(el.dataset.interval || "3000", 10);
    if (interval > 0) setInterval(function () { refresh(el); }, interval);
  });

  if (searchEl) {
    var t;
    searchEl.addEventListener("input", function () {
      clearTimeout(t);
      t = setTimeout(function () { state.q = searchEl.value.trim(); refreshAll(); }, 250);
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

  // ---- YAML syntax highlighting on the config editor ---------------------------------
  var ta = document.getElementById("yaml-editor");
  var hl = document.getElementById("yaml-highlight");
  if (ta && hl) {
    var esc = function (s) {
      return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    };
    var highlight = function (src) {
      return src.split("\n").map(function (line) {
        var e = esc(line);
        if (/^\s*#/.test(line)) return '<span class="y-com">' + e + "</span>";
        // key:  value
        var m = e.match(/^(\s*(?:- )?)([A-Za-z0-9_.-]+)(:)(.*)$/);
        if (m) {
          var val = m[4].replace(/^(\s+)(.*)$/, function (_a, sp, rest) {
            if (rest === "") return sp;
            var r = rest;
            if (/^#/.test(rest)) return sp + '<span class="y-com">' + rest + "</span>";
            if (/^(true|false|null|~)\s*$/i.test(rest)) r = '<span class="y-bool">' + rest + "</span>";
            else if (/^-?\d+(\.\d+)?\s*$/.test(rest)) r = '<span class="y-num">' + rest + "</span>";
            else r = '<span class="y-str">' + rest + "</span>";
            return sp + r;
          });
          return m[1] + '<span class="y-key">' + m[2] + "</span>" + '<span class="y-punc">:</span>' + val;
        }
        return e;
      }).join("\n");
    };
    var sync = function () {
      hl.innerHTML = highlight(ta.value) + "\n";
      hl.scrollTop = ta.scrollTop;
      hl.scrollLeft = ta.scrollLeft;
    };
    ta.addEventListener("input", sync);
    ta.addEventListener("scroll", function () { hl.scrollTop = ta.scrollTop; hl.scrollLeft = ta.scrollLeft; });
    // keep tabs usable as two spaces
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Tab") {
        e.preventDefault();
        var s = ta.selectionStart, en = ta.selectionEnd;
        ta.value = ta.value.slice(0, s) + "  " + ta.value.slice(en);
        ta.selectionStart = ta.selectionEnd = s + 2;
        sync();
      }
    });
    sync();
  }
})();
