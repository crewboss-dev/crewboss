"""Account-level SHARED items and their inheritance (owner, 2026-10-02; dashboard task #148).

An account (projects.kind = 'account', e.g. Acme) holds no tasks. It holds what applies to EVERY project under it:
  rules      kind 'rule'   a plain-language rule / AI instruction ("Australian English everywhere")
  AI skills  kind 'skill'  title, purpose (summary) and the instructions (body), e.g. Acme's "Humanizer"
  knowledge  every other live entry stored at the account (decisions, responsibilities, facts, notes, links)
All three are rows of the knowledge table stored at the account's project (migration 0020): authored by the owner on
the account's pages, or by a session whose reach includes the account (scope.py; dash's, for the owner's account). An account's
objectives are its own and are not inherited.

Every project under an account INHERITS them: its Rules / AI skills / Knowledge pages list them read-only under
"Inherited from <account>", and a session's briefing (repo.build_ground) carries the shared items of every account
above a project in its reach. Nothing else: a project never sees another account's items.
"""
from __future__ import annotations

from app import tree
from app.db import get_cursor

NOT_INHERITED = ("objective",)
SECTIONS = ("rules", "skills", "knowledge")
_SECTION_OF = {"rule": "rules", "skill": "skills"}
# The account page each kind is shown on (an inherited item links back to it).
PAGE_OF = {"rule": "/rules", "skill": "/ai-features"}


def page_for(kind: str) -> str:
    return PAGE_OF.get(kind, "/knowledge")


def _empty() -> dict:
    return {s: [] for s in SECTIONS}


def items_for(account_ids: list[int]) -> dict[int, dict]:
    """{account id: {"rules": [...], "skills": [...], "knowledge": [...]}} — live entries only, oldest first (a
    rule list reads in the order it was written)."""
    out = {aid: _empty() for aid in account_ids}
    if not account_ids:
        return out
    with get_cursor() as cur:
        cur.execute("SELECT * FROM knowledge WHERE project_id = ANY(%s) AND archived_at IS NULL "
                    "AND NOT (kind = ANY(%s)) ORDER BY created_at, id", (list(account_ids), list(NOT_INHERITED)))
        for r in cur.fetchall():
            out[r["project_id"]][_SECTION_OF.get(r["kind"], "knowledge")].append(r)
    return out


def items(account_id: int) -> dict:
    return items_for([account_id])[account_id]


def inherited(project: dict, idx: dict[int, dict] | None = None) -> list[dict]:
    """What a project inherits: [{"account": <project row>, "rules", "skills", "knowledge"}] for each account above
    it, nearest first. An account itself inherits nothing (its items are its own)."""
    idx = idx if idx is not None else tree.index()
    accounts = tree.account_ancestors(project, idx)
    by_id = items_for([a["id"] for a in accounts])
    return [dict(by_id[a["id"]], account=a) for a in accounts]


def accounts_for(project_ids, idx: dict[int, dict] | None = None) -> list[dict]:
    """The active accounts whose shared items reach these projects: every account above one of them, plus any
    account among them (its items are shared with its projects). In tree order."""
    idx = idx if idx is not None else tree.index()
    found: dict[int, dict] = {}
    for pid in project_ids:
        p = idx.get(pid)
        if not p or p["archived_at"]:
            continue
        if tree.is_account(p):
            found[p["id"]] = p
        for a in tree.account_ancestors(p, idx):
            found[a["id"]] = a
    order = {p["id"]: i for i, p in enumerate(tree.project_tree(idx=idx))}
    return sorted(found.values(), key=lambda a: order.get(a["id"], 0))


def inherited_account_ids(project_ids, idx: dict[int, dict] | None = None) -> set[int]:
    """Accounts ABOVE these projects (not the projects themselves): what a session may read by inheritance."""
    idx = idx if idx is not None else tree.index()
    out: set[int] = set()
    for pid in project_ids:
        if pid in idx and not idx[pid]["archived_at"]:
            out |= {a["id"] for a in tree.account_ancestors(idx[pid], idx)}
    return out


def counts(account_id: int) -> dict:
    """{"rules": n, "skills": n, "knowledge": n} for an account page's summary."""
    return {k: len(v) for k, v in items(account_id).items()}
