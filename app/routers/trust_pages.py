"""/trust (#976): the OPEN page a person lands on after the browser warns them about the LAN address. It shows the fingerprint of this
server's connection and how to click through in each browser. No sign-in (web_auth._OPEN_EXACT, see Temp_Files/976-b-to-a.txt), served on
plain http (port 80) as well as https, so a person can read it before they have clicked through anything. It reveals nothing a browser's own
certificate viewer would not show."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import front_door
from app.templating import templates

router = APIRouter(include_in_schema=False)


@router.get("/trust", response_class=HTMLResponse)
def trust(request: Request):
    host = front_door._host_of(request) or (front_door.lan_hosts() or [""])[0]
    return templates.TemplateResponse(request, "pages/trust.html", {
        "fingerprint": front_door.lan_fingerprint(), "address": host, "lan": bool(front_door.lan_hosts()),
        "https_url": front_door.lan_url(), "not_after": front_door.lan_not_after()},
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})
