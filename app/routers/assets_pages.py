"""The Assets section (#778): /assets (For you), /assets/recent, /assets/skills, /assets/rules, /assets/materials.

The asset data model is not designed yet, so these pages are READ-ONLY views over what the projects already keep (app/asset_hub.py),
each row linking to the project page where it lives. Scoping is by the person's filtered project index; there is no project in the URL."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import access, asset_hub
from app.routers.pages import _me_name, render

router = APIRouter(prefix="/assets", include_in_schema=False)


def _greeting() -> str:
    h = datetime.now().hour
    return "Good morning" if h < 12 else "Good afternoon" if h < 18 else "Good evening"


@router.get("", response_class=HTMLResponse)
def assets_home(request: Request):
    who = access.of_request(request)
    idx = access.filtered_index(who)
    now = datetime.now()
    return render(request, "pages/assets_home.html", "assets", greeting=_greeting(), first_name=_me_name(request).split()[0],
                  today=f"{now:%A} {now.day} {now:%B}", types=asset_hub.TYPES, counts=asset_hub.counts(who, idx),
                  places=asset_hub.recent_places(who))


@router.get("/recent", response_class=HTMLResponse)
def assets_recent(request: Request):
    return render(request, "pages/assets_recent.html", "assets_recent", rows=asset_hub.recent_materials(access.of_request(request)))


@router.get("/{kind}", response_class=HTMLResponse)
def assets_type(request: Request, kind: str):
    t = asset_hub.TYPE_BY_KEY.get(kind)
    if t is None:
        from app.repo import NotFound
        raise NotFound("no such asset type")
    return render(request, "pages/assets_type.html", "assets_" + kind, t=t, groups=asset_hub.collect(access.of_request(request), kind))
