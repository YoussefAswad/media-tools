# orchestrator

The product: a FastAPI webhook service, a durable SQLite job queue + worker, a per-file dispatch
pipeline, a back-catalogue library index, a bandwidth ledger, per-device household WAN usage,
host/container/SMART monitoring with a debouncing alert engine, ntfy status, and a
server-rendered web UI.

Composes `mkvprune` (as a subprocess) and `mediacore` (as a library).

## Layout

```
core            queue, ledger, wan_usage, metrics, alerts, services, runner, titles
steps/          the per-file pipeline, one class per step   (see CLAUDE.md)
library/        index / estimate / scan / queued            — the Library tab's data
monitor/        disk / docker / system / smart / wan        — one class per monitored source
web/
  deps.py       template env, services accessor, UI auth, page()/partial()
  presenters.py domain objects -> template shapes (tones, human strings, row dicts)
  routes/       one router per tab
  templates/    Jinja2, server-rendered
  static/       poll.js, library.js, yaml.js, app.css
```

## The UI is server-rendered HTML over a polling layer

No client framework and no vendored libraries — the CSP forbids external scripts, and every
view is a Jinja partial the server already knows how to render. `poll.js` re-fetches each
panel on its own interval and swaps its `innerHTML`, preserving scroll position.

**A panel is any element with `data-src`.** These attributes are the whole contract:

| attribute | on | effect |
|---|---|---|
| `data-src` | panel | partial URL to fetch (**required**) |
| `data-interval` | panel | poll milliseconds; `0` = fetch once, refresh only on demand |
| `data-searchable` | panel | append `q` from the `#search` box |
| `data-ranged` | panel | append `range`, or `start`/`end` from the date inputs |
| `data-unit-aware` | panel | append `units` (the GB/GiB choice, remembered per browser) |
| `data-paged` | panel | append `page`; `[data-page]` children inside the panel set it |
| `data-chip-filters` | panel | comma-separated `.filters` group ids — every non-empty `data-*` on each group's active chip becomes a query param |
| `data-countdown-for` | a label | mirrors that panel's poll cycle as "refresh in Ns" |

Adding a filter dimension therefore needs no JavaScript: add a `.filters` group with an id, give
its chips `data-<name>="<value>"`, and name the group in the panel's `data-chip-filters`. The
route receives `<name>` as a query parameter.

Two things worth knowing:

- **The panel with `data-interval="0"` is deliberate.** The Library table must not swap itself
  out from under a selection, so it refreshes only when asked.
- **A panel's `_onSwap` hook** lets a page-specific script re-apply state the innerHTML swap
  wiped (`library.js` uses it to restore checkboxes after paging).
