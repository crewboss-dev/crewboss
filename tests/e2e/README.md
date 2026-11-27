# End-to-end smoke suite

One fast pass through the real UI (Playwright, Python, headless Chromium) that catches the regressions that otherwise get found by hand. It boots
the app in-process against the throwaway Postgres from `tests/conftest.py` (never the live containers), seeds two projects and four tasks per test,
and drives desktop (1440x900) and phone (390x844, touch) browsers. About two to three minutes in all.

It is **skipped by the normal suite** (every test here is marked `e2e`): run it with `-m e2e` or `CREWBOSS_E2E=1`.

## Run it

```
scripts/e2e.sh                                  # everything
scripts/e2e.sh -k task_sheet                    # a journey
CREWBOSS_E2E=1 python -m pytest tests/e2e -q    # the same, by hand
```

Needs a Python with the app's requirements (argon2, cbor2, pywebpush, ...) and `playwright`, plus a Chromium headless shell under
`~/.cache/ms-playwright/chromium_headless_shell-*` (`python -m playwright install chromium-headless-shell`). Set `PYTHON=` to pick the interpreter.
Two checkouts can run at once with different `CREWBOSS_TEST_CONTAINER` and `CREWBOSS_TEST_PORT`.
A screenshot of every step that matters goes to `$CREWBOSS_E2E_SHOTS` (default `/tmp/crewboss-e2e-shots`).

## Before the first run: the three build outputs

`app/static/css/app.css`, `app/static/js/htmx.min.js` and `app/static/js/alpine.min.js` are built in the Docker image and are gitignored, so a plain
checkout has none. The suite refuses to start without them (layout assertions mean nothing on an unstyled page). Make them with the repo's own build
(you need `frontend/node_modules`: `cd frontend && npm install`):

```
cd frontend && npm run build:css && npm run build:vendor      # app.css, htmx.min.js, alpine.min.js
```

(Or copy the two vendor scripts out of a running image: `docker cp <app container>:/app/app/static/js/htmx.min.js app/static/js/`, same for
`alpine.min.js`. Build `app.css` yourself rather than copying one: it must match the templates in your checkout.)

Note: the built `app.css` carries one `.mallen-canvas` rule from `frontend/src/input.css`, so `tests/test_no_mallen_refs.py` (which scans files on
disk, built or not) reports `app/static/css/app.css: 1 (baseline 0)` in a checkout where you have built it. That is the build output, not new code:
run that test in a checkout without the build, or delete `app.css` afterwards.

## What every test also checks

`new_page` (conftest) attaches a watcher to every page it makes: **any console error, uncaught exception, same-origin response >= 400 or failed
request fails the test** at teardown. That is journey 8's broad net on every journey. Expected ones go in `ALLOWED` in `conftest.py`, each with its
reason (currently none). The first-run tour (#938) would start on every page of every journey, so `new_page` answers its progress as already finished (`stub_tour`); `new_page(device, tour=True)` lets it run for real. The CA's `/ca/*` side is stubbed in the browser (it needs a signed-in person and a runner): `/ca/state`, `/ca/stream`
(replaced by a handle, `window.__ca_emit`, so a test decides when a live event arrives), the discussions fragment, and per test the conversation
endpoints.

## Journeys covered

| # | Journey | File | Devices |
|---|---|---|---|
| 1 | Signed-out person is redirected to `/auth/login` (keeping `next`); a local session (as `tests/test_local_auth.py` fakes a passkey sign-in) gets the page | `test_e2e_menu_and_signin.py` | desktop |
| 2 | Now, Dashboard, Recent load and are current in the menu | same | both |
| 2 | The menu lists the projects; a project opens; the favourite star toggles, persists across a reload, and un-stars from the Favourites row | same | desktop |
| 2 | App switcher: Build / Assets / Administration, there and back | same | desktop |
| 2 | Header **+ Create** opens, picks a project, creates a task in it | same | both |
| 3 | A task opens as a full-window sheet from the board, the list and Now; address bar shows the task; **Back, Esc, the exit button and the x** each close it, leaving the page unreloaded (a `window` marker survives) with the same scroll position | `test_e2e_task_sheet.py` | both |
| 3 | Forward reopens the sheet; a refresh on the task URL is the full task page | same | both |
| 3 | Moving from one task to the next inside the sheet adds no history entry (one Back leaves) | same | both |
| 3 | `?open=<id>` opens the sheet | same | desktop |
| 4 | CA opens above the task sheet (computed z-index and `elementFromPoint`), its chip names the task, the first message creates a conversation linked to that task. Sheet first, then CA; and CA first, then sheet | `test_e2e_ca.py` | both |
| 4 | On Now, Dashboard and Recent CA is bound to no project (chip empty, nothing linked on send), even with a last-project cookie | same | both |
| 4 | Inside a project CA offers (and links) that project | same | both |
| 4 | A sent message shows once, whether the live stream event beats the POST's answer or follows it | same | desktop |
| 5 | Machines: Runners / Servers tabs, `?tab=` persists on refresh, `/runners` and `/servers` land on the right tab, the deploy-flow diagram shows, the two add actions are separate | `test_e2e_machines_and_views.py` | both |
| 6 | A project's Summary / List / Board pills each open; List and Board keep a filter across the switch | same | both |
| 6 | Saved filters on List and Board: save, list, apply, delete | same | both |
| 7 | Phone: the bottom menu sheet opens (ends at the tab bar), closes by the Menu tab and by a swipe on its grab line | `test_e2e_phone.py` | phone |
| 7 | Phone: the task sheet ends above the tab bar, its head is a drag zone (`touch-action: none`) and a swipe down on it closes the sheet | same | phone |
| 8 | No console errors / failed requests on ~30 pages (project pages, Assets, Administration, CA history, Machines, a task page) | `test_e2e_no_console_errors.py` | both |
| 8 | The watcher itself: a console error, a 404 and an uncaught exception are all seen | same | desktop |
| 9 | First-run how-to tour (#938): starts by itself, every step outlines its real element (and goes to its page), Back, resume after a reload, Skip, How-to restarts it; Tab stays in the card, Enter advances, Esc closes; the menus a step opens stay open; phone bottom sheet that swipes away, reduced motion, dark | `test_e2e_tour.py` | both |

## Known limits

* Sign-in is faked with a session row, as the unit tests do; the browser's passkey prompt is not driven. The OIDC (Microsoft) mode has its own unit tests.
* Moving from one task to the next *inside the sheet* is driven with the same `htmx.ajax` call a link in the CA or a jump result makes, since no
  control in the sheet itself links to another task.
* A test that fails because the app is wrong on `main` is marked `xfail(strict=True)` with the failure as its reason; the strict flag makes it fail
  loudly (XPASS) the day the bug is fixed, so the marker gets removed.
