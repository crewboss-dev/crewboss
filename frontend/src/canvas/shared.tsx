// Pieces both canvas kinds share: the host options, the grade badge, host-filled "extra" containers in the
// side panel, per-browser node nudges (localStorage, best effort), and the shell that lays the canvas and its
// side panel out (side by side when the mount is wide, panel below the canvas when it is narrow).
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { Icon } from "./icons.tsx";

export type CanvasOptions = {
  /** "view" is the only mode today: nothing on either site edits a workflow or an integration from the canvas. */
  mode: "view";
  /** e.g. "/admin/integrations#i-{id}": an integration's name links there ({id} replaced). Unset = plain text. */
  integrationLink?: string;
  /** A step id (workflow) or integration id (integrations) to open on load. */
  open?: string;
  /** kind=integrations: draw this module (overrides the data's `module`). */
  module?: string;
  /** Host content for the side panel — a function, or <template data-mc-step-extra="<id>"> /
   * <template data-mc-integration-extra="<id>"> children of the mount element (cloned in). */
  renderStepExtra?: (id: string) => HTMLElement | null;
  renderIntegrationExtra?: (id: string) => HTMLElement | null;
  templates?: { step: Map<string, HTMLTemplateElement>; integration: Map<string, HTMLTemplateElement> };
};

export const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

export function GradeBadge({ grade, title }: { grade: string; title?: string }) {
  const g = /^[ABCD]$/.test(grade) ? grade : "x";
  return <span className={`mc-grade mc-grade-${g}`} title={title || `Grade ${grade}`} aria-label={`grade ${grade}`}>{grade || "?"}</span>;
}

/** A container the HOST fills: its renderXExtra(id) result, else a clone of its <template>, else whatever a
 * listener for the bubbling `mallen-canvas:extra` event puts in it ({kind, id} in detail). Empty = hidden. */
export function HostExtra({ kind, id, opts }: { kind: "step" | "integration"; id: string; opts: CanvasOptions }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.replaceChildren();
    const fn = kind === "step" ? opts.renderStepExtra : opts.renderIntegrationExtra;
    let node: Node | null = null;
    try { node = fn ? fn(id) : null; } catch (e) { console.error("mallen-canvas: extra renderer failed", e); }
    if (!node) {
      const tpl = opts.templates && opts.templates[kind].get(id);
      if (tpl) node = tpl.content.cloneNode(true);
    }
    if (node) el.appendChild(node);
    el.dispatchEvent(new CustomEvent("mallen-canvas:extra", { bubbles: true, detail: { kind, id } }));
    const htmx = (window as unknown as { htmx?: { process: (e: Element) => void } }).htmx;
    if (htmx && el.childNodes.length) htmx.process(el);
  }, [kind, id, opts]);
  const attrs = kind === "step" ? { "data-step-id": id } : { "data-integration-id": id };
  return <div ref={ref} className={`mc-extra mc-extra-${kind}`} {...attrs} />;
}

// --- per-browser node nudges ------------------------------------------------------------------------------
// Dragged nodes keep an OFFSET from where the layout puts them (so expanding a condition reflows around a
// nudged card instead of fighting it). localStorage may be missing or throw (private mode): then nudges last
// until the page reloads.

type Offsets = Record<string, { dx: number; dy: number }>;

function readOffsets(key: string): Offsets {
  try {
    const raw = window.localStorage.getItem(key);
    const v = raw ? JSON.parse(raw) : {};
    return v && typeof v === "object" ? v : {};
  } catch { return {}; }
}

export function useOffsets(key: string) {
  const [offsets, setOffsets] = useState<Offsets>(() => readOffsets(key));
  useEffect(() => { setOffsets(readOffsets(key)); }, [key]);
  const save = useCallback((next: Offsets) => {
    setOffsets(next);
    try {
      if (Object.keys(next).length) window.localStorage.setItem(key, JSON.stringify(next));
      else window.localStorage.removeItem(key);
    } catch { /* not persisted: fine */ }
  }, [key]);
  const nudge = useCallback((id: string, dx: number, dy: number) => {
    save({ ...offsets, [id]: { dx: Math.round(dx), dy: Math.round(dy) } });
  }, [offsets, save]);
  const reset = useCallback(() => save({}), [save]);
  return { offsets, nudge, reset, moved: Object.keys(offsets).length > 0 };
}

// --- the shell --------------------------------------------------------------------------------------------

export function Shell({ height, panel, panelOpen, onClose, children, label, toolbar }: {
  height: number; panel: ReactNode; panelOpen: boolean; onClose: () => void; children: ReactNode; label: string;
  toolbar?: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  // Escape closes the panel (when focus is inside this canvas, or nowhere in particular)
  useEffect(() => {
    if (!panelOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const root = stageRef.current && stageRef.current.closest(".mallen-canvas");
      const active = document.activeElement;
      if (active && active !== document.body && root && !root.contains(active)) return;
      onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [panelOpen, onClose]);
  // stacked (narrow): bring the panel into view when something opens
  useEffect(() => {
    const p = panelRef.current, s = stageRef.current;
    if (!panelOpen || !p || !s || p.offsetTop <= s.offsetTop + 10) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    requestAnimationFrame(() => p.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "nearest" }));
  }, [panelOpen, panel]);
  return (
    <div className="mc-layout">
      <div ref={stageRef} className="mc-stage" role="region" aria-label={label}>
        {toolbar && <div className="mc-toolbar">{toolbar}</div>}
        <div className="mc-stage-inner" ref={(el) => { if (el) el.style.height = `${height}px`; }}>{children}</div>
      </div>
      <aside ref={panelRef} className="mc-panel" aria-live="polite">{panel}</aside>
    </div>
  );
}

export function PanelHead({ chips, title, onClose }: { chips: ReactNode; title: ReactNode; onClose?: () => void }) {
  return (
    <>
      <div className="mc-panel-top">
        {chips}
        {onClose && (
          <button type="button" className="mc-btn-ghost mc-close" aria-label="Close panel" onClick={onClose}>
            <Icon name="close" className="mc-ico" />
          </button>
        )}
      </div>
      <div className="mc-panel-title">{title}</div>
    </>
  );
}

export function linkFor(template: string | undefined, id: string): string | null {
  return template ? template.split("{id}").join(encodeURIComponent(id)) : null;
}
