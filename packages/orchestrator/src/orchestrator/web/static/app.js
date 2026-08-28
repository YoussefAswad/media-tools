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
    // generic chip groups: every non-empty data-* on the active chip becomes a query param, so a
    // new filter dimension only needs a `.filters` group and a data-<name> on its chips.
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

  // ---- Library tab: a selection that survives paging, filtering and re-renders ---------
  // Decimal (base-1000) formatting, matching the server's `human_bytes` so the number in the
  // confirmation reads the same as the one in the table it came from.
  function fmtBytes(n) {
    n = Math.abs(n || 0);
    var units = ["B", "KB", "MB", "GB", "TB", "PB"], i = 0;
    while (n >= 1000 && i < units.length - 1) { n /= 1000; i += 1; }
    return i === 0 ? Math.round(n) + " B" : n.toFixed(2) + " " + units[i];
  }

  // "+840 MB" / "−1.20 GB" / "" when there is nothing to say. `unknown` counts files that don't
  // report every track's size, so the figure is a floor — say so rather than quoting it flat.
  function deltaPhrase(delta, unknown) {
    if (!delta) return unknown ? "Size change couldn't be estimated for " + unknown + " file(s)." : "";
    var s = "Estimated size change: " + (delta < 0 ? "\u2212" : "+") + fmtBytes(delta);
    s += delta < 0 ? " reclaimed." : " added.";
    if (unknown) s += " " + unknown + " file(s) only partly counted, so the real figure is larger.";
    return s;
  }

  var libPanel = document.getElementById("library-panel");
  if (libPanel) {
    var libForm = document.getElementById("library-form");
    var countEl = document.getElementById("lib-selcount");
    var queueBtn = document.getElementById("lib-queue");
    var clearBtn = document.getElementById("lib-clear");
    // path -> {delta, known}: the delta travels with the selection so a pick spanning several
    // pages can still be totalled without asking the server again.
    var selected = new Map();

    var syncSelection = function () {
      var n = selected.size;
      countEl.textContent = n ? "\u2014 " + n + " selected" : "\u2014 nothing selected";
      queueBtn.disabled = n === 0;
      queueBtn.textContent = n ? "Queue " + n + " selected" : "Queue selected";
      clearBtn.hidden = n === 0;
      var boxes = libPanel.querySelectorAll("input.libsel");
      var wholePage = boxes.length > 0;
      boxes.forEach(function (b) {
        b.checked = selected.has(b.value);
        if (!b.checked) wholePage = false;
      });
      var head = document.getElementById("lib-check-page");
      if (head) head.checked = wholePage;
    };
    libPanel._onSwap = syncSelection;

    var remember = function (box) {
      selected.set(box.value, {
        delta: parseInt(box.dataset.delta, 10) || 0,
        known: box.dataset.known !== "0",
      });
    };

    libPanel.addEventListener("change", function (e) {
      var box = e.target;
      if (box.id === "lib-check-page") {
        libPanel.querySelectorAll("input.libsel").forEach(function (b) {
          if (box.checked) remember(b); else selected.delete(b.value);
        });
      } else if (box.classList && box.classList.contains("libsel")) {
        if (box.checked) remember(box); else selected.delete(box.value);
      } else {
        return;
      }
      syncSelection();
    });

    libPanel.addEventListener("click", function (e) {
      var pg = e.target.closest("[data-page]");
      if (!pg || pg.disabled) return;
      state.page = parseInt(pg.dataset.page, 10) || 1;
      refresh(libPanel);
    });

    clearBtn.addEventListener("click", function () { selected.clear(); syncSelection(); });

    document.getElementById("lib-refresh").addEventListener("click", function () {
      libPanel._lastHtml = null;  // the html may be byte-identical; force the swap anyway
      refreshAll();
    });

    // "Select all matching" is a submit button: it only raises the flag and lets the normal
    // submit run, because form.submit() would skip the submit handler that writes the filters.
    document.getElementById("lib-all").addEventListener("click", function () {
      document.getElementById("lib-flag-all").value = "1";
    });

    // Queueing rewrites every selected file in place and can run for hours — the one action in
    // the app expensive enough to be worth a confirmation, and the only one that had none.
    // Filter-set totals come off the rendered wrapper so "select all matching" can quote the
    // same numbers the table does.
    function confirmQueue(all) {
      var box = libPanel.querySelector(".lib-rows");
      var cap = box ? parseInt(box.dataset.cap, 10) || 0 : 0;
      var n, delta = 0, unknown = 0;
      if (all) {
        n = box ? parseInt(box.dataset.matchingCount, 10) || 0 : 0;
        delta = box ? parseInt(box.dataset.matchingDelta, 10) || 0 : 0;
        unknown = box ? parseInt(box.dataset.matchingUnknown, 10) || 0 : 0;
      } else {
        n = selected.size;
        selected.forEach(function (v) {
          delta += v.delta;
          if (!v.known) unknown += 1;
        });
      }
      if (!n) return false;
      var lines = [
        all ? "Queue all " + n + " matching files?" : "Queue " + n + " selected file(s)?",
      ];
      if (cap && n > cap) lines.push("Only the first " + cap + " will be queued (per-submit cap).");
      var d = deltaPhrase(delta, unknown);
      if (d) lines.push(d);
      lines.push("Each file is remuxed and rewritten in place. This can take hours.");
      return window.confirm(lines.join("\n\n"));
    }

    // The row checkboxes are UI only (no name attribute), so nothing posts unless it is written
    // here — otherwise the visible page would be the only thing submitted.
    libForm.addEventListener("submit", function (e) {
      var allFlag = document.getElementById("lib-flag-all");
      var all = !!allFlag.value;
      if (!confirmQueue(all)) {
        allFlag.value = "";  // a cancelled "select all" must not arm the next ordinary submit
        e.preventDefault();
        return;
      }
      document.getElementById("lib-flag-q").value = state.q;
      document.getElementById("lib-flag-app").value = state.app;
      document.getElementById("lib-flag-status").value = activeData("lib-status", "status");
      document.getElementById("lib-flag-reason").value = activeData("lib-reason", "reason");
      if (all) return;
      selected.forEach(function (_v, path) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = "paths";
        input.value = path;
        libForm.appendChild(input);
      });
    });
  }

  // ---- YAML syntax highlighting on the config editor ---------------------------------
  var ta = document.getElementById("yaml-editor");
  var hl = document.getElementById("yaml-highlight");
  if (ta && hl) {
    var esc = function (s) {
      return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    };
    // color a single scalar value (already html-escaped) as bool / number / string
    var colorScalar = function (v) {
      var t = v.trim();
      if (t === "") return v;
      if (/^(true|false|null|~)$/i.test(t)) return '<span class="y-bool">' + v + "</span>";
      if (/^-?\d+(\.\d+)?$/.test(t)) return '<span class="y-num">' + v + "</span>";
      return '<span class="y-str">' + v + "</span>";
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
        // inline flow map:  - { key: val, key: val }
        var fm = e.match(/^(\s*(?:- )?)(\{)(.*)(\})(\s*)$/);
        if (fm) {
          var inner = fm[3].replace(
            /([A-Za-z0-9_.-]+)(\s*)(:)(\s*)([^,}]*)/g,
            function (_a, k, s1, colon, s2, val2) {
              return '<span class="y-key">' + k + "</span>" + s1 +
                '<span class="y-punc">' + colon + "</span>" + s2 + colorScalar(val2);
            }
          );
          return fm[1] + '<span class="y-punc">{</span>' + inner +
            '<span class="y-punc">}</span>' + fm[5];
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
