# Digest summary contract (`GET /api/summary`)

Task #828. A read-only feed for an external digest (a morning mail, a chat post). It gives three short lists and nothing
else. Code: `app/summary.py`.

## The token

A `digest` role token: project-less (instance-scoped, like `scanner` and `zabbix`), read-only, and it may call **only**
this endpoint. Every other route answers it 401/403/404, MCP refuses it, and a session token or any other role gets a 403
on `/api/summary`. Issue one straight into a 0600 file; only its hash is stored:

```
docker compose exec -T app python -m app.tokens create --kind digest --quiet > digest.token
```

## Request and answer

```
GET /api/summary
Authorization: Bearer <digest token>
```

```json
{
  "generated_at": "2026-11-18T22:00:00+00:00",
  "code_faults":      {"count": 2, "items": [{"title": "...", "project": "<health project slug>", "since": "<iso>", "path": "/<project>/health"}]},
  "releases_waiting": {"count": 1, "items": [{"title": "app to prod (prod) a1b2c3d", "project": "app", "since": "<iso>", "path": "/<account>/approvals"}]},
  "owner_reviews":    {"count": 3, "items": [{"id": 812, "title": "...", "project": "app", "path": "/app/tasks/812"}]}
}
```

- `code_faults`: open health problems whose event the ERP tagged `kind: "code_fault"` (see the Health page; a missing kind
  is ops and is not listed). Newest first.
- `releases_waiting`: deploy requests waiting for an approval (`server_jobs.state = requested`), oldest first.
- `owner_reviews`: tasks with status `review` assigned to the owner, on active projects, oldest first.
- Scope: the digest serves the INSTANCE OWNER's brief. `owner_reviews` and `releases_waiting` cover only the owner's own,
  unarchived projects (`projects.owner_user_id` NULL or an owner user, as `app/access.py` defines it); a designer's private
  project, its review tasks and its deploy requests never appear. `code_faults` are the health project's open code faults.
- `count` is the true total; `items` is capped at 10.
- `path` is a path on this dashboard (prefix it with the dashboard's address); it is never a full URL.

## What it never contains

Titles only. No task bodies, comments, commit subjects (a deploy request's `subject` is outside text and is left out),
tokens, keys or server addresses.
