// Inline SVG icons (24-unit, stroke = currentColor) for the shared canvas: step systems, trigger types,
// condition/loop headers, and the few panel controls. Sizes come from CSS (.mc-ico, .mc-ico-12, .mc-ico-16).
import type { ReactNode } from "react";

const P = (d: string) => <path d={d} />;
const ICONS: Record<string, ReactNode> = {
  db: <>{P("M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3Z")}{P("M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6")}{P("M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3")}</>,
  claude: <>{P("M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8Z")}{P("M19 16l.7 1.8 1.8.7-1.8.7L19 21l-.7-1.8-1.8-.7 1.8-.7Z")}</>,
  graph: <>{P("M3 6h18v12H3z")}{P("M3 7l9 6 9-6")}</>,
  email: <>{P("M3 6h18v12H3z")}{P("M3 7l9 6 9-6")}</>,
  xero: <>{P("M12 3v18")}{P("M17 7.5c0-1.9-2.2-3-5-3s-5 1.1-5 3 2.2 2.7 5 3.3 5 1.4 5 3.4-2.2 3.3-5 3.3-5-1.2-5-3.1")}</>,
  stripe: <>{P("M3 6h18v12H3z")}{P("M3 10h18")}{P("M7 15h3")}</>,
  zabbix: <>{P("M12 4l9 16H3Z")}{P("M12 10v4")}{P("M12 17.5v.01")}</>,
  push: <>{P("M6 16V11a6 6 0 1 1 12 0v5l2 2H4Z")}{P("M10 20a2 2 0 0 0 4 0")}</>,
  r2: <>{P("M3 7l9-4 9 4-9 4Z")}{P("M3 7v10l9 4 9-4V7")}{P("M12 11v10")}</>,
  sharepoint: <>{P("M3 6h7l2 2h9v11H3z")}</>,
  mcp: <>{P("M9 3v5")}{P("M15 3v5")}{P("M6 8h12v3a6 6 0 0 1-12 0Z")}{P("M12 17v4")}</>,
  internal: <>{P("M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z")}{P("M12 2v3M12 19v3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M2 12h3M19 12h3M4.9 19.1 7 17M17 7l2.1-2.1")}</>,
  timer: <>{P("M12 21a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z")}{P("M12 9v4l2.5 2")}{P("M9 2h6")}</>,
  webhook: <>{P("M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1")}{P("M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1")}</>,
  button: <>{P("M9 9l10 4-4 1.5L13.5 19Z")}{P("M5 3v3M3 5h3M8 4.5 9.5 6M4.5 8 6 9.5")}</>,
  event: <>{P("M13 2 4 14h7l-1 8 9-12h-7Z")}</>,
  api: <>{P("M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z")}{P("M3 12h18")}{P("M12 3c2.5 2.5 3.8 5.5 3.8 9s-1.3 6.5-3.8 9c-2.5-2.5-3.8-5.5-3.8-9S9.5 5.5 12 3Z")}</>,
  cloud: <>{P("M7 18a4 4 0 0 1-.5-8A6 6 0 0 1 18 9a4.5 4.5 0 0 1-.5 9Z")}</>,
  condition: <>{P("M6 3v6a3 3 0 0 0 3 3h6a3 3 0 0 1 3 3v6")}{P("M18 3v6a3 3 0 0 1-3 3")}{P("M4 5l2-2 2 2M16 19l2 2 2-2")}</>,
  loop: <>{P("M17 2l3 3-3 3")}{P("M4 11V9a4 4 0 0 1 4-4h12")}{P("M7 22l-3-3 3-3")}{P("M20 13v2a4 4 0 0 1-4 4H4")}</>,
  close: <>{P("M6 6l12 12M18 6L6 18")}</>,
  chevron: <>{P("M6 9l6 6 6-6")}</>,
  plus: <>{P("M12 6v12M6 12h12")}</>,
  tasks: <>{P("M9 6h11M9 12h11M9 18h11M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2")}</>,
  link: <>{P("M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1")}{P("M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1")}</>,
  reset: <>{P("M4 4v6h6")}{P("M20 12a8 8 0 0 0-14.9-4L4 10")}{P("M20 20v-6h-6")}{P("M4 12a8 8 0 0 0 14.9 4l1.1-2")}</>,
};
const ICON_ALIAS: Record<string, string> = {
  webpush: "push", syncro: "cloud", crayon: "cloud", pingco: "cloud", azuremaps: "cloud", click: "button",
};

export function Icon({ name, className = "mc-ico" }: { name: string; className?: string }) {
  const icon = ICONS[name] || ICONS[ICON_ALIAS[name] || ""] || ICONS.cloud;
  return (
    <svg viewBox="0 0 24 24" className={className} fill="none" stroke="currentColor" strokeWidth={2}
         strokeLinecap="round" strokeLinejoin="round" aria-hidden>{icon}</svg>
  );
}
