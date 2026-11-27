"""Answering access.StepUpRequired (#809, #976). htmx callers: 403 + HX-Trigger "stepup-required" (js/stepup.js asks for a fresh 6-digit
authenticator code, or a passkey tap where there is one, and re-sends the request). JSON callers and tokens: 403 JSON. Anyone else: a plain "Confirm it's you" page."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app import local_auth as la
from app.fold import is_htmx
from app.templating import templates

_NO_STORE = {"Cache-Control": "no-store"}


def handle_stepup(request: Request, exc) -> object:
    if exc.token:
        return JSONResponse({"error": "this needs a signed-in person's recent confirmation (authenticator code or passkey); a token can't give one"},
                            status_code=403, headers=_NO_STORE)
    if exc.oidc:
        msg = "This needs a recent Microsoft sign-in. Sign out and sign in again, then repeat it."
        return PlainTextResponse(msg, status_code=403, headers=_NO_STORE) if not is_htmx(request) else \
            PlainTextResponse(msg, status_code=403, headers={**_NO_STORE, "HX-Trigger": "{\"mdd:flash\": {\"message\": \"" + msg + "\", \"tone\": \"error\"}}"})
    if is_htmx(request):
        return PlainTextResponse("Confirm it's you", status_code=403, headers={**_NO_STORE, "HX-Trigger": "stepup-required",
                                                                                     "HX-Reswap": "none"})
    if request.url.path.startswith("/api/") or "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse({"error": "confirm it's you", "stepup": True}, status_code=403, headers=_NO_STORE)
    return templates.TemplateResponse(request, "pages/auth_stepup.html", {"error": "", "passkeys_enabled": la.passkeys_available(request)},
                                      status_code=403, headers=_NO_STORE)
