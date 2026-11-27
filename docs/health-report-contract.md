# Health report contract (servers push their app health to Crewboss)

Task #404 (owner, 2026-10-15): "each server should host its own health checks and push those to dev, and have a cache
period in case the connection is down or dev is down." Each server runs its own health checks, spools every report
locally, and pushes batches to the dashboard; the dashboard stops pulling. Receiver: `app/health_reports.py`.
Per-project settings: `app/health_feeds.yaml`.

## Endpoint

```
POST https://crewboss.example.com/api/health/report
Authorization: Bearer <reporter token>
Content-Type: application/json
```

Reachable only on your private network if you deploy it that way. From a container on the dashboard host itself:
`http://127.0.0.1:8931/api/health/report`.

## The token

A `reporter` token is write-only and bound to ONE project: it can post that project's reports and nothing else (every
other route answers 403, MCP refuses it). It can also be limited to named sources. Issue one per server, straight into
a 0600 file:

```
cd <dashboard checkout> && (umask 077 && docker compose exec -T app \
  python -m app.tokens create --kind reporter --project acme-app --source web-1 --quiet > reporter-web-1.token)
```

Only its hash is stored. A lost token can't be recovered: issue a new one.

## Body: a batch

```json
{
  "reports": [
    {
      "report_id": "5d0c6a6e-3b1f-4f5e-9a51-2a7d0f6c1b9e",
      "source": "web-1",
      "observed_at": "2026-10-15T05:02:00Z",
      "project": "acme-app",
      "health": {
        "v": 1,
        "generated_at": "2026-10-15T05:02:00Z",
        "events": [], "failures": {}, "heartbeats": [], "webhooks": [], "xero": {}, "ai_usage": {},
        "app_sha": "32955758f8c7aeaf2380b580190746640e8eabd9",
        "catalog": {"commit": "757e17cef0c304a1cf1f2f0a7234138188945a3c", "built_at": "2026-10-15T04:58:10Z"}
      }
    }
  ]
}
```

| field | rule |
|---|---|
| `reports` | 1 to 500 reports; the whole body at most 2 MiB (`CREWBOSS_HEALTH_REPORT_MAX_BATCH`, `CREWBOSS_HEALTH_REPORT_MAX_BYTES`) |
| `report_id` | a UUID the server makes when it observes. Resending the same id is safe: it is a `duplicate` |
| `source` | which server/check produced it: lower-case `a-z 0-9 . _ -`, 1 to 63 chars, e.g. `web-1`. A project can have several |
| `observed_at` | when the server observed it, ISO 8601 **with a zone**. Not more than 5 min in the future, not older than the 7-day history |
| `project` | optional; if present it must be the token's project |
| `health` | the same v1 JSON the app's `GET /api/health` serves (the ERP health feed contract): `v: 1` required, every section optional |
| `health.app_sha` | optional: the deployed commit (hex) |
| `health.catalog` | optional: `{commit: <hex>, built_at: <ISO with zone>}`, the commit the admin site's workflow catalog was built from (#402) |
| `health.host` | optional (#828): `{disk_free_pct: number 0-100, disk_path: string <= 200 chars, uptime_s: integer >= 0}`, every key optional. A bad value rejects that report |

## Answer: one result per report

`200` with, in the same order as sent:

```json
{
  "results": [
    {"report_id": "5d0c…", "status": "accepted", "latest": true},
    {"report_id": "7a1e…", "status": "duplicate"},
    {"report_id": "bad",   "status": "rejected", "reason": "report_id must be a UUID"}
  ],
  "accepted": 1, "duplicate": 1, "rejected": 1
}
```

- `accepted`: stored. `latest: true` = it is now the source's latest state; `false` = an older (backfilled) report that
  went into the history only.
- `duplicate`: that `report_id` was already stored. Treat as acknowledged.
- `rejected`: permanently refused, with a `reason`. Don't resend it: log it and drop it.

**The sender drops `accepted` and `duplicate` from its spool**, logs and drops `rejected`, and keeps everything else
for the next try.

Whole-request errors (nothing stored, resend later unless fixed):
`400` the body isn't `{"reports": [...]}` JSON or is empty; `401` no or unknown token; `403` not a reporter token;
`404` `?project=` names another project; `413` too many reports or too many bytes (send a smaller batch);
`5xx` / no answer: the dashboard is down, keep spooling.

## The spool (sender side)

- Observe on the project's interval (`expect_every_seconds`, 120 s for the ERP), give each report a new UUID and
  `observed_at`, append it to the local spool, then try to push the oldest-first batch.
- Keep at least the 7-day history window; the dashboard rejects older reports.
- After an outage, push the spool in order, 500 per batch. The dashboard applies each batch oldest `observed_at` first.

## What the dashboard does with them

- **Latest state per (project, source) = the newest `observed_at`**, not the newest arrival. Older reports go into the
  history (7 days, `CREWBOSS_HEALTH_REPORT_HISTORY_DAYS`) for the Health timeline.
- **Dead-man**: a source whose newest *observed* report is older than 3 x `expect_every_seconds`
  (`CREWBOSS_HEALTH_REPORT_DEADMAN_FACTOR`) raises the Health error "<project> stopped reporting (<source>)", sent once to
  the errors session; a newer report clears it. A replayed spool fills the gap with real history.
- **The ERP section** of the Health page (events, failures, heartbeats, webhooks, Xero, AI usage) is fed from the
  pushed reports once `app/health_feeds.yaml` says `mode: push` for acme-app (with `erp_source` naming the server whose
  reports count). Until then the dashboard still fetches `GET <ERP_HEALTH_URL>/api/health`; that pull path is removed
  once the ERP pushes.
- **Host signal (#828)**: the Machines page shows, per deploy server, a compact row: Reachable (the newest observed report of
  a project that deploys there, whose `source` is the server's name, is younger than the dead-man window), Disk free %
  (from `health.host`), TLS days left (the project's `public_url` in `app/health_feeds.yaml`, https only, checked by
  `app/tls_check.py` and cached for an hour) and Last deploy (finished deploy records). A server whose newest report says
  **less than 10% disk free** raises the error "<project>: low disk on <source>, N% free", sent once to the errors session;
  it clears only when a newer report is above 20% (hysteresis, so a disk hovering at the line raises once). The disk chip shows
  the worst fresh report's free % and path; TLS is never dialled while a page is drawn (cached 1 h good / 5 min bad, refreshed in
  the background). Nothing is installed on the server: it only needs to send `health.host`.
- **Admin catalog check (#402)** reads whichever report is current: `app_sha` + `catalog.commit`.

## Switching the ERP over

1. The ERP ships its sender (spool + push) with a reporter token per server.
2. Its reports appear under Health → App reports (the project shows as "fetched by the dashboard" until step 3).
3. Set `mode: push` (+ `erp_source: web-1`) for acme-app in `app/health_feeds.yaml`, rebuild the dashboard.
4. When it has run clean, remove the pull path (`ERP_HEALTH_URL`, the read token, the fetch in app/erp_health.py).
