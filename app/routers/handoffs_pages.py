"""Handoffs page (#908): GET /<slug>/handoffs, READ-ONLY. The AI does the data entry (MCP tools send_handoff, put_doc, ...); the owner reads.

An account shows every handoff thread of the account (newest activity first, status, from -> to, replies) and its shared docs with their
version history; a project shows the threads it is party to, plus the account's docs. A thread is left out when the viewer cannot see either
end (multi-user, #713). The text shown is data written by AI sessions. Behind ProjectDep like every /{slug}/... page (a project you can't see is
a 404)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import access, account_docs, handoffs, tree
from app.routers.pages import ProjectDep, render

project_router = APIRouter(prefix="/{slug}")


@project_router.get("/handoffs", response_class=HTMLResponse)
def handoffs_page(request: Request, project: dict = ProjectDep):
    account_id = tree.account_root_id(project["id"])
    visible = access.visible_ids(access.of_request(request))
    threads = handoffs.for_page(account_id, None if tree.is_account(project) else project["id"], visible)
    who = access.of_request(request)
    docs_visible = access.can_view(who, account_id)       # shared docs need the whole account (#713): a decision the owner can change
    return render(request, "pages/handoffs.html", "handoffs", threads=threads, docs=account_docs.for_account(account_id) if docs_visible else [],
                  docs_visible=docs_visible, open_count=sum(1 for t in threads if t["status"] == "open"))
