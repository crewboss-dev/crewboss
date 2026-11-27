// The side panel's content for both kinds: a workflow STEP (its details, the AI node, the integrations it
// uses) and an integrations-diagram SYSTEM (every integration with that system, weakest first). Read-only.
import { useEffect, useRef, useState, type ReactNode } from "react";

import { type Integration, type IntegrationsExport, vocabLabel } from "./integrations-model.ts";
import { GradeBadge, HostExtra, linkFor, PanelHead, plural, type CanvasOptions } from "./shared.tsx";

const KIND_LABEL: Record<string, string> = {
  trigger: "Trigger", precode: "Pre-code", ai: "AI", logic: "Logic", action: "Action", decision: "Condition", loop: "Loop",
};
export const kindLabel = (k: string) => KIND_LABEL[k] || k;

function Row({ k, children }: { k: string; children: ReactNode }) {
  if (children === "" || children === null || children === undefined) return null;
  return <div className="mc-row"><span className="mc-row-k">{k}</span><span className="mc-row-v">{children}</span></div>;
}

function IntegrationName({ i, opts }: { i: Integration; opts: CanvasOptions }) {
  const href = linkFor(opts.integrationLink, i.id);
  return href ? <a className="mc-link" href={href}>{i.name}</a> : <span>{i.name}</span>;
}

/** One integration's facts. `full` adds the routing rows the integrations page shows (endpoint, how the name
 * is found, the route, keys); the workflow panel shows the short form the owner asked for. */
export function IntegrationFacts({ i, exp, full }: { i: Integration; exp: IntegrationsExport | null; full?: boolean }) {
  return (
    <div className="mc-facts">
      {i.purpose && <p className="mc-purpose">{i.purpose}</p>}
      <Row k="With">{i.peer}{i.us ? ` (from ${i.us})` : ""}</Row>
      {full && <Row k="Data">{vocabLabel(exp, "flows", i.flow)}</Row>}
      {full && <Row k="Who starts">{vocabLabel(exp, "starts", i.starts)}</Row>}
      {full && <Row k="Kind">{vocabLabel(exp, "kinds", i.kind)}</Row>}
      {full && <Row k="Points at"><span className="mc-mono">{i.endpoint}</span></Row>}
      {full && <Row k="Found via">{vocabLabel(exp, "resolves", i.resolves)}</Row>}
      {full && <Row k="Route">{vocabLabel(exp, "paths", i.path)}</Row>}
      <Row k="Sign-in">{vocabLabel(exp, "auths", i.auth)}</Row>
      <Row k="Line">{vocabLabel(exp, "transports", i.transport)}</Row>
      {full && i.keys && i.keys.length > 0 && <Row k="Keys">{i.keys.map((k) => <span key={k} className="mc-chip mc-mono">{k}</span>)}</Row>}
      {i.status && i.status !== "live" && <Row k="Status">{i.status}</Row>}
      {i.why && i.why.length > 0 && (
        <ul className="mc-why" aria-label={`Why grade ${i.grade}`}>{i.why.map((w, n) => <li key={n}>{w}</li>)}</ul>
      )}
      {i.accepted && <p className="mc-accepted"><span className="mc-row-k">Accepted:</span> {i.accepted}</p>}
      {full && i.notes && <p className="mc-note">{i.notes}</p>}
    </div>
  );
}

/** The "Integration" block in a workflow step's panel. One integration shows open; several are compact rows
 * that expand one at a time. */
export function IntegrationBlock({ list, exp, opts }: { list: Integration[]; exp: IntegrationsExport | null; opts: CanvasOptions }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!list.length) return null;
  const single = list.length === 1;
  return (
    <section className="mc-section" aria-label="Integration">
      <div className="mc-section-k">{single ? "Integration" : `Integrations (${list.length})`}</div>
      {list.map((i) => {
        const expanded = single || open === i.id;
        return (
          <div key={i.id} className={`mc-int ${expanded ? "mc-int-open" : ""}`}>
            <div className="mc-int-head">
              <GradeBadge grade={i.grade} />
              <span className="mc-int-name"><IntegrationName i={i} opts={opts} /></span>
              {!single && (
                <button type="button" className="mc-btn-ghost mc-int-toggle" aria-expanded={expanded}
                        aria-label={`${expanded ? "Hide" : "Show"} ${i.name}`} onClick={() => setOpen(expanded ? null : i.id)}>
                  <svg viewBox="0 0 24 24" className={`mc-ico-12 mc-chev ${expanded ? "" : "mc-chev-shut"}`} fill="none"
                       stroke="currentColor" strokeWidth={2.4} strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M6 9l6 6 6-6" /></svg>
                </button>
              )}
            </div>
            {expanded && <IntegrationFacts i={i} exp={exp} />}
          </div>
        );
      })}
    </section>
  );
}

export type StepInfo = {
  id: string; kind: string; label: string; detail?: string; system?: string; question?: string;
  code_ref?: { file?: string; lines?: string; function?: string } | null;
  ai?: { model_rule?: string; prompt_name?: string; prompt_location?: { file?: string; lines?: string } | null;
         prompt_text?: string; context_injected?: string[]; max_tokens?: number | null; output?: string } | null;
};

export function StepPanel({ step, error, chips, integrations, exp, opts, onClose }: {
  step: StepInfo; error?: string | null; chips?: { text: string; href: string; tone: string }[];
  integrations: Integration[]; exp: IntegrationsExport | null; opts: CanvasOptions; onClose: () => void;
}) {
  const ai = step.ai;
  return (
    <div className={`mc-card ${error ? "mc-card-failed" : ""}`}>
      <PanelHead onClose={onClose}
                 chips={<><span className="mc-chip">{kindLabel(step.kind)}</span>{step.system && <span className="mc-muted mc-micro">{step.system}</span>}</>}
                 title={step.kind === "decision" && step.question ? step.question : step.label} />
      {step.kind === "decision" && step.question && step.question !== step.label && <div className="mc-muted mc-small">{step.label}</div>}
      {step.detail && <p className="mc-detail">{step.detail}</p>}
      {error && (
        <div className="mc-error" role="alert"><div className="mc-section-k">Error</div><div>{error}</div></div>
      )}
      {chips && chips.length > 0 && (
        <div className="mc-chips">{chips.map((c) => <a key={c.href + c.text} className={`mc-chip mc-chip-${c.tone}`} href={c.href}>{c.text}</a>)}</div>
      )}
      {step.code_ref && step.code_ref.file && (
        <div className="mc-code">
          <span className="mc-mono">{step.code_ref.file}{step.code_ref.lines ? `:${step.code_ref.lines}` : ""}</span>
          {step.code_ref.function && <> · <span className="mc-mono">{step.code_ref.function}()</span></>}
        </div>
      )}
      {ai && (
        <section className="mc-section" aria-label="AI node">
          <div className="mc-section-k">AI node</div>
          <Row k="Model rule">{ai.model_rule}</Row>
          <Row k="Prompt">{ai.prompt_name}{ai.prompt_location && ai.prompt_location.file &&
            <span className="mc-mono mc-muted"> ({ai.prompt_location.file}{ai.prompt_location.lines ? `:${ai.prompt_location.lines}` : ""})</span>}</Row>
          {ai.prompt_text && (
            <details className="mc-details"><summary>Prompt text</summary><pre className="mc-pre">{ai.prompt_text}</pre></details>
          )}
          {ai.context_injected && ai.context_injected.length > 0 && (
            <Row k="Injected context"><ul className="mc-list">{ai.context_injected.map((c, n) => <li key={n}>{c}</li>)}</ul></Row>
          )}
          <Row k="Max tokens">{ai.max_tokens == null ? "" : String(ai.max_tokens)}</Row>
          <Row k="Output">{ai.output}</Row>
        </section>
      )}
      <IntegrationBlock list={integrations} exp={exp} opts={opts} />
      <HostExtra kind="step" id={step.id} opts={opts} />
    </div>
  );
}

export function SystemPanel({ peer, zone, links, exp, opts, focus, onClose }: {
  peer: string; zone: string; links: Integration[]; exp: IntegrationsExport | null; opts: CanvasOptions;
  focus?: string | null; onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!focus || !ref.current) return;
    const el = ref.current.querySelector(`[data-mc-int="${CSS.escape(focus)}"]`);
    if (el) requestAnimationFrame(() => el.scrollIntoView({ block: "nearest" }));
  }, [focus, peer]);
  return (
    <div className="mc-card" ref={ref}>
      <PanelHead onClose={onClose}
                 chips={<><span className="mc-chip">{vocabLabel(exp, "zones", zone)}</span><span className="mc-muted mc-micro">{plural(links.length, "integration")}</span></>}
                 title={peer} />
      {links.map((i) => (
        <section key={i.id} className={`mc-section mc-int mc-int-open ${focus === i.id ? "mc-int-focus" : ""}`} data-mc-int={i.id}
                 aria-label={i.name}>
          <div className="mc-int-head">
            <GradeBadge grade={i.grade} />
            <span className="mc-int-name"><IntegrationName i={i} opts={opts} /></span>
          </div>
          <IntegrationFacts i={i} exp={exp} full />
          <HostExtra kind="integration" id={i.id} opts={opts} />
        </section>
      ))}
    </div>
  );
}
