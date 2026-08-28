// Library tab: a selection that survives paging, filtering and re-renders, and the
// confirmation that quotes what queueing it would actually do.
//
// Loaded after poll.js, which it reaches through window.Panels.
(function () {
  var state = window.Panels.state;
  var refresh = window.Panels.refresh;
  var refreshAll = window.Panels.refreshAll;
  var activeData = window.Panels.activeData;

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
    //
    // Persisted for the tab's session: curating 200 files is real work, and losing it to a
    // stray reload or a click through to a job was a genuinely annoying way to lose it. Scoped
    // to sessionStorage, not localStorage — a selection is about this sitting, not forever.
    var STORE = "lib-selection";
    var selected = new Map();
    try {
      var saved = JSON.parse(sessionStorage.getItem(STORE) || "[]");
      if (Array.isArray(saved)) saved.forEach(function (e) { selected.set(e[0], e[1]); });
    } catch (e) { /* private mode, cleared storage, older shape — start empty */ }

    var persist = function () {
      try {
        sessionStorage.setItem(STORE, JSON.stringify(Array.from(selected.entries())));
      } catch (e) { /* storage full or blocked: the selection still works in memory */ }
    };

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
      persist();
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

    clearBtn.addEventListener("click", function () { selected.clear(); syncSelection(); });

    // the scan form starts open on first run (nothing indexed = one useful action)
    var scanToggle = document.getElementById("scan-toggle");
    var scanPanel = document.getElementById("scan-panel");
    if (scanToggle && scanPanel) {
      scanToggle.addEventListener("click", function () {
        scanPanel.hidden = !scanPanel.hidden;
        scanToggle.setAttribute("aria-expanded", String(!scanPanel.hidden));
      });
    }

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
      // whatever is submitted has left the tab: don't carry it into the next visit
      try { sessionStorage.removeItem(STORE); } catch (e) { /* ignore */ }
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
})();
