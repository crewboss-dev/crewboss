# Planning dates (task #915)

Owner, 2026-11-22: "Need to make sure we start when we need to. We should have deadlines in crewboss, or planning dates ...
There will be times we need to wait for something or someone."

The owner does not fill these in (rule k79): the AI does the data entry over MCP or REST, and the pages only display them.

## Milestones

A milestone is a named date in one project (`milestones`: id, project_id, name, due_on, note, created_by, created_at,
archived_at). Names are unique per project, ignoring case, while not archived. Archive, never delete.

Its **status is derived**, never stored:

| status | when |
|---|---|
| `done` | it has tasks and every one is done |
| `late` | `due_on` is before today and a task is still open |
| `at_risk` | an open task's `start_by` is before today and the task is still `backlog` or `ready` |
| `on_track` | otherwise |

Won't-have tasks are not counted. `counts` is `{total, done, open}`.

## Task dates

New nullable columns on `tasks` (migration `0112_planning_dates.sql`):

| field | meaning |
|---|---|
| `milestone_id` | one milestone of the task's **own project** (a database trigger refuses any other) |
| `due_on` | the task's own deadline |
| `lead_days` | effort plus expected waiting, 0..365 |
| `start_by` | the day work must start. Stored |
| `start_by_manual` | true when `start_by` was given explicitly |
| `waiting_on` | who or what: `owner`, `ai`, a project slug, or free text (`Google Play review`, `customer: Acme`, `lawyer`) |
| `waiting_for` | one line on what for |
| `waiting_until` | the date it is expected back |
| `start_reminded_for`, `chase_reminded_for` | the date a reminder was last sent for |

**Effective due date: computed at read time.** A task's `due_on`, else its milestone's `due_on`. Rows carry it as
`due_effective` with `due_source` (`own` or `milestone`). Nothing is copied, so moving a milestone moves the tasks that follow it.

**start_by: stored, kept current.** Unless it was set explicitly it is `due_effective - lead_days`, recomputed whenever the
task's `due_on`, `lead_days` or milestone change, and when its milestone's date moves (`planning.recompute_milestone_tasks`).
Giving `start_by` pins it (`start_by_manual`); clearing it hands it back to the computation. A task with no due date or no
`lead_days` has no computed `start_by`.

**Waiting.** Clearing `waiting_on` clears `waiting_for` and `waiting_until` too (a CHECK constraint enforces it). Setting
`waiting_for` or `waiting_until` needs a `waiting_on`.

**Validation.** Dates are ISO `YYYY-MM-DD` (a real date, years 2000..2100); `lead_days` is an integer 0..365; `waiting_on` is one
line of at most 120 characters, `waiting_for` 200, a milestone name 120, its note 500. Anything else is a 400 (REST) or an error (MCP).

**Display flags** on every task row: `due_state` is `late` (effective due before today, not done) or `start_reached` (`start_by` is
today or earlier and the task is `backlog`/`ready`); `waiting_overdue` is `waiting_until` before today.

## MCP and REST

MCP (`app/mcp_server.py`):

- `add_task(..., due_on, lead_days, start_by, milestone, waiting_on, waiting_for, waiting_until)`; `milestone` is an id or a name.
- `update_task(task_id, ..., the same fields, clear=[...])`. `clear` names fields to empty: `due_on`, `lead_days`, `start_by`,
  `milestone`, `waiting` (which clears the three waiting fields together).
- `add_milestone(project, name, due_on, note="")`, `update_milestone(milestone_id, name, due_on, note, archived)`,
  `list_milestones(project=None, include_archived=False)` with `status` and `counts`.
- `get_task`, `list_tasks`, `my_tasks` return the new fields.

REST: the same fields on `POST /api/tasks` and `PATCH /api/tasks/{id}` (plus `clear`), and `GET/POST /api/milestones`,
`PATCH /api/milestones/{id}`.

**Scope** is unchanged (`app/scope.py`): a session touches only milestones of projects in its reach; another account's milestone id
is NotFound exactly like a missing one. The overseer may READ milestones of every active project (`list_milestones`) and may file a
request task naming the target project's milestone (`add_task`, as before), but cannot add or change another project's milestones.

## The overseer's plan

`get_global_view` (`app/plan.py`):

- `default_order` ranking: production fault, then **late or start-by reached**, then owner reviews / open questions, then **due
  within 7 days or waiting overdue**, then Must / Should / Could, then the existing tie-breakers. Factors read `late: due ...`,
  `start-by reached (...)`, `due within 7 days (...)`, `waiting overdue: ...`.
- Slim task rows (compact or not) carry `due_on` (the effective date), `start_by`, `milestone`, `waiting_on`, `waiting_for`,
  `waiting_until`, only when set.
- `this_week`: open tasks whose `start_by` or due date is within the next 7 days, plus those already past (a late task, a
  `start_by` reached and not started, so they never fall off the list), plus waiting items whose `waiting_until` has passed. Each row
  says why (`due`, `late`, `start by`, `chase`), earliest first, at most 30.

## Reminders (`app/planning_reminders.py`)

A loop in the app process (`main._planning_reminders`, every `CREWBOSS_PLANNING_REMINDER_SECONDS`, default 900; 0 = off) sends at
most one push per task per date through `app/push.py`:

- `Start today: <title> — to make <milestone> (<date>)` when `start_by` is today or earlier and the task is still backlog/ready.
- `Chase: <title> — waiting on <who> for <what>, expected back <date>` when `waiting_until` is before today and the task is open.

The date a push was sent for is stored on the task, so it never repeats; changing the date clears it and re-arms the reminder.
Everything due in one pass is ONE push (a digest, "N planning reminders", five lines and "+N more"). Nothing is sent before
`CREWBOSS_PLANNING_REMINDER_HOUR` (default 07:00, instance timezone). While push is off or no device is subscribed nothing is marked,
so the first phone to subscribe gets what is due. Pushes go to every subscribed device of the install, as other Crewboss pushes do
(per-person delivery is #713).

## Pages

Task cards and the task window show a date chip (red when late, amber when the start-by has been reached) and a "Waiting on X until Y"
badge; a project's Summary and Board show a compact milestone strip (name, date, status, done/total).
