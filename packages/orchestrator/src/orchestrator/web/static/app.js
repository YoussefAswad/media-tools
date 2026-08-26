// Minimal live-refresh: poll each [data-src] element and swap in the returned HTML fragment.
// No external libraries — keeps the artifact fully self-contained (no CDN at runtime).
(function () {
  function refresh(el) {
    fetch(el.dataset.src, { headers: { "X-Requested-With": "poll" } })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) { if (html !== null) el.innerHTML = html; })
      .catch(function () { /* transient; keep last render */ });
  }
  document.querySelectorAll("[data-src]").forEach(function (el) {
    refresh(el);
    var interval = parseInt(el.dataset.interval || "3000", 10);
    setInterval(function () { refresh(el); }, interval);
  });
})();
