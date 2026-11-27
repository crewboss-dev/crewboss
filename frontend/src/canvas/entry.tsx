// The shared Mallen canvas bundle (docs/canvas-contract.md): ONE self-contained IIFE + one stylesheet that draws
// workflows and integration diagrams identically on Crewboss and the ERP admin site.
//
//   <div class="mallen-canvas" id="c1" data-kind="workflow|integrations" data-src="/x.json" data-mode="view"
//        data-integration-link="/admin/integrations#i-{id}" data-open="<step or integration id>" data-module="core">
//     <template data-mc-step-extra="<step id>">…host content for that step's panel…</template>
//   </div>
//   or inline data: <script type="application/json" data-canvas-for="c1">{…}</script>
//
// Mounts every .mallen-canvas[data-kind] on DOMContentLoaded and after each htmx swap; unmounts before htmx
// removes one. window.MallenCanvas.mount(el, data?, opts?) / unmount(el) for anything else.
// CSP: script-src 'self' / style-src 'self' — no eval, no injected <style>, no inline style attributes in markup.
import "@xyflow/react/dist/style.css";
import "./canvas.css";

import { Component, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";

import { IntegrationsCanvas, type IntegrationsCanvasData } from "./integrations-view.tsx";
import type { CanvasOptions } from "./shared.tsx";
import { WorkflowCanvas, type WorkflowCanvasData } from "./workflow-view.tsx";

export const CONTRACT_VERSION = "1";
type Kind = "workflow" | "integrations";
type Mounted = { root: Root; templates: CanvasOptions["templates"] };
const mounted = new Map<HTMLElement, Mounted>();

class Boundary extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: unknown) { return { error: e instanceof Error ? e.message : String(e) }; }
  componentDidCatch(e: unknown) { console.error("mallen-canvas:", e); }
  render() {
    return this.state.error
      ? <div className="mc-card mc-hint mc-empty-state" role="alert">This diagram couldn't be drawn ({this.state.error}).</div>
      : this.props.children;
  }
}

function inlineData(el: HTMLElement): unknown | undefined {
  const own = el.querySelector<HTMLScriptElement>(":scope > script[type='application/json']");
  const byId = el.id ? document.querySelector<HTMLScriptElement>(`script[type='application/json'][data-canvas-for="${CSS.escape(el.id)}"]`) : null;
  const script = byId || own;
  if (!script) return undefined;
  return JSON.parse(script.textContent || "{}");
}

function collectTemplates(el: HTMLElement): NonNullable<CanvasOptions["templates"]> {
  const step = new Map<string, HTMLTemplateElement>(), integration = new Map<string, HTMLTemplateElement>();
  el.querySelectorAll<HTMLTemplateElement>(":scope > template[data-mc-step-extra], :scope > template[data-mc-integration-extra]").forEach((t) => {
    if (t.dataset.mcStepExtra) step.set(t.dataset.mcStepExtra, t);
    if (t.dataset.mcIntegrationExtra) integration.set(t.dataset.mcIntegrationExtra, t);
  });
  return { step, integration };
}

function optionsFrom(el: HTMLElement, templates: CanvasOptions["templates"], given?: Partial<CanvasOptions>): CanvasOptions {
  const d = el.dataset;
  return {
    mode: "view",
    integrationLink: d.integrationLink || undefined,
    open: d.open || undefined,
    module: d.module || undefined,
    templates,
    ...(given || {}),
  };
}

function render(el: HTMLElement, m: Mounted, kind: Kind, data: unknown, opts: CanvasOptions) {
  const body = kind === "integrations"
    ? <IntegrationsCanvas data={(data || {}) as IntegrationsCanvasData} opts={opts} />
    : <WorkflowCanvas data={(data || {}) as WorkflowCanvasData} opts={opts} />;
  m.root.render(<Boundary>{body}</Boundary>);
  el.dataset.mcState = "ready";
}

/** Mount (or re-mount) one canvas. `data` defaults to the inline JSON or data-src; `opts` overrides the
 * data-* attributes (and is the only way to pass renderStepExtra / renderIntegrationExtra functions). */
export function mount(el: HTMLElement, data?: unknown, opts?: Partial<CanvasOptions>): void {
  if (!el) return;
  el.classList.add("mallen-canvas");
  const kind: Kind = el.dataset.kind === "integrations" ? "integrations" : "workflow";
  let m = mounted.get(el);
  if (!m) {
    let json: unknown;
    try { json = data === undefined ? inlineData(el) : data; } catch (e) { console.error("mallen-canvas: bad inline JSON", e); }
    const templates = collectTemplates(el);
    el.replaceChildren();
    m = { root: createRoot(el), templates };
    mounted.set(el, m);
    if (data === undefined) data = json;
  }
  const options = optionsFrom(el, m.templates, opts);
  const src = el.dataset.src;
  if (data === undefined && src) {
    const mm = m;
    el.dataset.mcState = "loading";
    mm.root.render(<div className="mc-card mc-hint mc-empty-state">Loading…</div>);
    fetch(src, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then((json) => { if (mounted.get(el) === mm) render(el, mm, kind, json, options); })
      .catch((e) => {
        console.error("mallen-canvas: couldn't load", src, e);
        if (mounted.get(el) === mm) mm.root.render(<div className="mc-card mc-hint mc-empty-state" role="alert">Couldn't load this diagram.</div>);
      });
    return;
  }
  render(el, m, kind, data, options);
}

export function unmount(el: HTMLElement): void {
  const m = mounted.get(el);
  if (!m) return;
  m.root.unmount();
  mounted.delete(el);
  delete el.dataset.mcState;
}

function autoMount(scope: ParentNode = document) {
  const els: HTMLElement[] = [];
  if (scope instanceof HTMLElement && scope.matches(".mallen-canvas[data-kind]")) els.push(scope);
  scope.querySelectorAll<HTMLElement>(".mallen-canvas[data-kind]").forEach((el) => els.push(el));
  for (const el of els) if (!mounted.has(el)) mount(el);
}

declare global {
  interface Window { MallenCanvas?: { version: string; mount: typeof mount; unmount: typeof unmount; mountAll: typeof autoMount } }
}
window.MallenCanvas = { version: CONTRACT_VERSION, mount, unmount, mountAll: autoMount };

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => autoMount());
else autoMount();
document.addEventListener("htmx:afterSwap", (e) => autoMount((e.target as ParentNode) || document));
document.addEventListener("htmx:beforeCleanupElement", (e) => {
  const el = e.target as HTMLElement;
  if (el && mounted.has(el)) unmount(el);
});
