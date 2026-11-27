# The shared canvas — contract (version 1)

One self-contained component draws **workflows** and **integration diagrams** identically on Crewboss and
the ERP admin site (#393, mallen-erp #391). The Dashboard builds it; the admin site copies the files verbatim.

## Files

Built by `npm run build:canvas` in `frontend/` (part of `npm run build`), **committed** in this repo:

| File | What |
|---|---|
| `app/static/canvas/mallen-canvas.js` | IIFE: React, React Flow, dagre and the canvas bundled in (~420 kB, ~140 kB gzip) |
| `app/static/canvas/mallen-canvas.css` | everything it needs, React Flow's own stylesheet included (~38 kB) |
| `app/static/canvas/VERSION` | `contract=1` and `source=<git sha it was built from>[-dirty]` |

Copy all three together. Sources: `frontend/src/canvas/` (+ the node-tested `frontend/src/workflow-*.ts`),
config `frontend/vite.canvas.config.ts`.

## CSP

Works under `script-src 'self'; style-src 'self'` with no `unsafe-*`: no CDN, no `eval`/`new Function`, no runtime
`<style>` injection, no `style` attributes in markup (positions are set through CSSOM, which CSP allows). Add
`connect-src 'self'` when a mount uses `data-src`. Inline data uses `<script type="application/json">`, which CSP
does not execute. `tests/test_canvas_bundle.py` greps the bundle and mounts both kinds in headless Chromium under that
policy, failing on any console error or CSP violation.

## Mounting

```html
<link rel="stylesheet" href="/static/canvas/mallen-canvas.css">
<div class="mallen-canvas" id="wf1" data-kind="workflow" data-mode="view"></div>
<script type="application/json" data-canvas-for="wf1">{"workflow": {…}, "integrations": {…}}</script>

<div class="mallen-canvas" data-kind="integrations" data-mode="view" data-src="/admin/integrations.json"
     data-module="core" data-integration-link="/admin/integrations#i-{id}"></div>
<script src="/static/canvas/mallen-canvas.js" defer></script>
```

The bundle mounts every `.mallen-canvas[data-kind]` on `DOMContentLoaded`, again after every `htmx:afterSwap` (inside
the swapped target), and unmounts one on `htmx:beforeCleanupElement`. Mounting replaces the element's children.

| Attribute | Meaning |
|---|---|
| `data-kind` | `workflow` (default) or `integrations` |
| `data-mode` | `view` — the only mode in v1: nothing on the canvas edits anything |
| `data-src` | URL of the JSON (fetched same-origin, cookies sent). Without it: the inline JSON below |
| `<script type="application/json" data-canvas-for="<id>">` | inline JSON for the mount with that id (anywhere in the page), or a JSON script as the mount's direct child |
| `data-integration-link` | e.g. `/admin/integrations#i-{id}`: integration names become links, `{id}` replaced (URL-encoded). Unset = plain text |
| `data-open` | a step id (workflow) or integration id (integrations) to open in the side panel on load |
| `data-module` | kind=integrations: draw this module (overrides the data's `module`; hides the module switcher) |
| `data-theme` | `light` / `dark` forces a theme for this mount (see Theming) |
| `data-mc-state` | set BY the bundle: `loading`, then `ready` |

Integrations also honour **`?open=<integration id>`** in the page URL: the canvas whose module holds that id opens
the system's panel with that row highlighted; opening/closing a system keeps `?open=` in step (history.replaceState).

JavaScript API — `window.MallenCanvas`:

```js
MallenCanvas.version           // "1"
MallenCanvas.mount(el, data?, opts?)   // (re)mount; data defaults to inline JSON / data-src; re-calling re-renders
MallenCanvas.unmount(el)
MallenCanvas.mountAll(scope?)  // mount every unmounted .mallen-canvas[data-kind] under scope (default document)
```
`opts` overrides the attributes: `{integrationLink, open, module, renderStepExtra, renderIntegrationExtra}`.

## Data — kind=workflow

```json
{
  "workflow": { /* ONE catalog workflow object, as in docs/catalog-schema.md / exports/catalog.json */ },
  "integrations": { /* the ERP's integrations export, or any subset of it with the same shape; optional */ },
  "run":    {"failedStep": "call-xero", "error": "…", "path": ["a", "b"]},           // optional, see below
  "extras": {"tasks": true, "base": "/mallen-erp", "openTasks": {"step": 2},
             "ruleCounts": {"step": {"rules": 1, "instructions": 0}},
             "skills": {"step": {"id": "qualify-reply", "name": "Qualify reply"}}}   // optional, Dashboard only
}
```

The bundle converts the catalog to a graph and lays it out (structured scopes, dagre fallback) itself, so both sites
draw the same picture. Conditions and loops are collapsible groups (chevron, or ←/→ on a focused pill; collapsed
shows "N Actions"). Click or Enter on a step opens the side panel: kind, system, label, detail, `code_ref`, the AI
node (model rule, prompt name/location/text, injected context, max tokens, output) and the **Integration block**.

**Integration block.** A step's integrations: its own `integrations` ids when it names some, else
`integrations.systems[step.system]`; unknown ids are dropped (the scanner reports them). One shows open; several show
as compact rows (weakest first) that expand one at a time: name (linked via `data-integration-link`), purpose, peer,
sign-in (`vocab.auths`), line (`vocab.transports`), grade badge A–D and the reasons (`why`), `accepted` when set. A
step card that uses integrations carries a small badge with the weakest grade.

**Clean by default (owner rule, #393: issue tracking stays out of admin).** Without `run` and `extras` the canvas
shows steps, decisions, loops and integrations only — no task badges, no "+" request connectors, no run/failure
marks, no task or rule links. Crewboss turns its extras on: `extras.tasks: true` (badges + "+"),
`extras.skills` / `ruleCounts` (card meta and panel chips, linked under `extras.base`), `run` (the run view).

## Data — kind=integrations

```json
{"integrations": { /* the export: version, vocab, integrations[], systems, summary */ }, "module": "core"}
```
`module` (or `data-module`) picks the module to draw; without one the biggest is drawn with a module switcher above.
The module sits in the middle; systems that send data TO it on the left, systems it sends to on the right, two-way
systems balance the shorter side zone by zone; each side grouped by zone under its `vocab.zones` heading. One line per
system, coloured by its WEAKEST grade, arrowheads the way the data goes (both ends = both ways), labelled with what
moves. Click a system (or its line, or Enter on it): the side panel lists every integration with that system, weakest
first, with all its facts (data flow, who starts, endpoint, how the name resolves, route, sign-in, line, key NAMES,
status, reasons, accepted, notes). The other systems dim. **Escape** closes the panel (as does the ✕ or a click on
empty canvas). Leftover (not live) systems are dashed.

## Host content in the side panel

Each step panel ends with an empty `<div class="mc-extra mc-extra-step" data-step-id="<id>">`; each integration in a
system panel ends with `<div class="mc-extra mc-extra-integration" data-integration-id="<id>">` (the admin site's
Set/Replace key forms go here). Empty ones are hidden. The bundle fills them, first match wins:

1. `opts.renderStepExtra(id)` / `opts.renderIntegrationExtra(id)` → an `HTMLElement` (or `null`), via `mount()`;
2. a clone of `<template data-mc-step-extra="<id>">` / `<template data-mc-integration-extra="<id>">` — direct
   children of the mount element, collected before mounting (CSP-safe: no script);
3. nothing — then a bubbling `mallen-canvas:extra` event (`detail: {kind: "step"|"integration", id}`) fires on the
   container every time it is (re)created, so a host listener can fill it.

After filling, `htmx.process(container)` runs when htmx is on the page, so htmx forms inside work. Host content keeps
the host's own CSS (it is outside the canvas's scoped rules only in that the bundle does not style it).

## Events

| Event | Where | Detail |
|---|---|---|
| `wf:node-click` / `wf:step-tasks` / `wf:step-new-task` | `window`, both ways | `{id}` — a step was selected (by the canvas, or the host selects one by dispatching it) |
| `wf:close` | `window` | the step panel closed |
| `mallen-canvas:open` | the mount element (bubbles) | `{kind: "integrations", peer, ids}` — a system's panel opened |
| `mallen-canvas:extra` | the extra container (bubbles) | `{kind, id}` — see above |

## Interaction

Infinite desktop: drag the background (or one finger) to pan; scroll pans; pinch or Ctrl/⌘+scroll zooms; +/−/fit
buttons; minimap (hidden under 560 px wide). Cards, condition/loop pills, systems and the hub can be dragged; each
move is kept per browser in `localStorage` (`mallen-canvas:wf:<workflow id>` / `mallen-canvas:int:<module>`, as
offsets from the layout, best effort — private mode simply forgets) and a reset button appears in the controls.
Layout: canvas and panel side by side; the panel moves under the canvas when the mount is narrower than 760 px
(container query, so it follows the mount's width, not the window's). Keyboard: Tab to a card/pill/system, Enter
opens it, Escape closes the panel.

## Theming

Everything is scoped under `.mallen-canvas`; nothing depends on the host's classes (no Tailwind). Set any of these
custom properties on `.mallen-canvas` (or an ancestor) **for both your light and dark themes** — anything unset falls
back to the Mallen tokens, light or dark:

| Variable | Default (light / dark) | Used for |
|---|---|---|
| `--mc-font`, `--mc-mono` | system sans / system mono | text |
| `--mc-text`, `--mc-muted` | `#141413` / `#FAF9F5`, 58% of that | text, secondary text |
| `--mc-bg` | `#F5F4ED` / `#141413` | the dotted desktop |
| `--mc-surface` | `#FAF9F5` / `#1A1A18` | cards, systems, panel |
| `--mc-line`, `--mc-line-strong` | `#E8E6DC` / `#30302E`; ink 25% | borders |
| `--mc-dot`, `--mc-edge`, `--mc-soft` | ink 20% / 38% / 5% | background dots, workflow lines, soft fills |
| `--mc-accent` | terracotta `#C96442` / coral `#D97757` | links |
| `--mc-selected` | terracotta / coral | selection ring, taken path |
| `--mc-focus` | focus blue `#3898EC` | keyboard focus ring |
| `--mc-danger`, `--mc-ok` | `#C62828`, `#2E7D52` | failed step, Yes/No branches |
| `--mc-kind-trigger/-precode/-ai/-logic/-action` | terracotta, slate, AI blue `#2A78D6`, gold, teal | step kind colours |
| `--mc-grade-a/-b/-c/-d` | `#2E7D52`, `#C9A227`, `#E8821E`, `#C62828` | grade badges and lines |
| `--mc-grade-ink`, `--mc-grade-ink-b` | white, `#141413` | text on grade badges (B is light) |
| `--mc-pill-bg`, `--mc-pill-text`, `--mc-pill-line` | ink, ivory, none / ivory 30% | condition pills, the hub |
| `--mc-box`, `--mc-box-line` | ink 3% / 12% | condition/loop containers |
| `--mc-radius`, `--mc-shadow`, `--mc-shadow-float` | 12px, Mallen card/float shadows | surfaces |
| `--mc-panel-width` | `352px` | the side panel |
| `--mc-sticky-top` | `16px` | where the sticky panel stops (the Dashboard and the ERP admin site keep 16px: their scroller starts under the header) |

The defaults are the Claude palette (DESIGN.md §2, #479). Both hosts set every colour from their theme tokens anyway
(the Dashboard in `frontend/src/input.css`, the admin site in `app/static/canvas-theme.css`).

Dark mode applies when `<html>` has class `dark` or `data-theme="dark"`, when the mount has `data-theme="dark"`, or
when the OS prefers dark **unless** `<html>` has class `light` / `data-theme="light"` (or the mount
`data-theme="light"`). A host with its own theme switch should put `light` or `dark` on `<html>`.

## Versioning

`contract=1` in `VERSION` and `MallenCanvas.version` name THIS document. Additive changes (new optional data, new
variables, new events) keep the contract; a change that breaks a mount, a data shape or a variable bumps it, and the
ERP's copy step should check `contract=` before replacing its files. `source=` is the Dashboard commit the files were
built from (the commit that adds the files is one after it).
