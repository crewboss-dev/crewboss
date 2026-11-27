# Fold-away: actions that remove or move an item update in place (task #582)

Owner, 2026-10-27: clicking something that then disappears must not send the page back to the top. The item folds
away, what is below slides up into view, focus lands on the next item. Same convention and motion as the ERP's
remove-in-place (ERP #583), so both surfaces behave alike.

Files: `app/fold.py` (server answers), `app/static/js/fold.js` (behaviour), `app/static/css/fold.css` (the collapse),
`tests/test_fold_away.py` + `tests/fold.test.mjs`.

## Adding a new removing action (the recipe)

1. Mark the item `data-fold` (no value). Mark its list `data-fold-section` (add `data-fold-hide-empty` to hide the whole
   section when empty; `data-fold-count` on the number; `data-fold-placeholder` + `hidden` on the "nothing here" row).
2. Make the form/button an htmx request at the item: `hx-post="…" hx-target="closest [data-fold]" hx-swap="outerHTML"`,
   keeping the normal `method`/`action` so it still works without JS.
3. In the route, finish with `fold_or_redirect(request, "Done message.", fallback_url, inserts=[...])` (`app/routers/pages.py`).
   htmx gets an empty 200 + a toast; a plain post gets the 303 + flash cookie as before. If the item MOVES to another list on
   the same page, pass `inserts=[insert_block("#other-list", row_html, "afterbegin")]`; the row folds in as the item folds out.
4. Errors stay what they were: `handle_repo_error` answers htmx with a 4xx + toast and no swap (the item stays).

## A whole view that is re-rendered (board, Focus, plan, list, checklist)

Leave the route as it is (it returns the full fragment). Put `data-fold-root` on the fragment's root and
`data-fold="<section>:<id>"` on each item. fold.js matches old and new by key: keys that vanished fold out, keys that
appeared fold in (so a task moving Now to Done today leaves one list and joins the other), the rest are replaced under
the reader. Scroll is restored and focus lands on the next item (or the section heading when the list emptied).

## Motion

220 ms, height + opacity only, `cubic-bezier(.25,.1,.25,1)` out and its mirror in. `prefers-reduced-motion`: the change is
instant. Plain form posts that must reload (the 303 path) save and restore `#page`'s scroll (`crewboss_keep_scroll`).
