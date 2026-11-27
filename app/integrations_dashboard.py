"""Crewboss's OWN integrations (#398's start, with #562): the links from the Dashboard to other systems, in
the same export shape as the ERP's registry (the ERP's integrations registry export(), validated by
app/integrations.validate), so they join a project's Integrations page as another source ("dashboard") through
integrations.merged() without the page changing.

NO DRIFT (#403): a new Dashboard integration is a row here in the same change as its code. The tests fail when a
platform key store (config.PLATFORM_KEY_STORES) uses a credential no row here names. Key NAMES only (for the Dashboard,
the credential file's name under ~/.config/crewboss/); never a value.

The ERP lists the same link from its side (its row dash-platform-keys: "platform keys in, status out")."""
from __future__ import annotations

from app import config, roles

SOURCE = "dashboard"

# The label maps for the codes used below: the ERP's vocabulary, word for word (its export's `vocab`).
VOCAB = {
    "kinds": {"api": "API call"},
    "flows": {"in": "data comes to us", "out": "data goes from us", "both": "data goes both ways"},
    "starts": {"us": "we call them", "them": "they call us", "both": "either side starts"},
    "zones": {"estate": "Our other servers"},
    "resolves": {"hosts_file": "the server's hosts file"},
    "paths": {"mesh": "NetBird mesh"},
    "auths": {"hmac_signature": "Signed request (HMAC)"},
    "transports": {"tls": "TLS"},
}

INTEGRATIONS = [
    {
        "id": "dash-platform-keys",
        "name": "Platform keys to each project's key store",
        "module": "dashboard",
        "owner": "platform",
        "purpose": "The Platform keys page (#562; on the Dashboard project, one section per key store, #577) sets, "
                   "rotates, ends grace on and clears the keys that run the service for every tenant, and their expiry and rotation dates, in each project's own key store, one way. "
                   "It reads back STATUS only (set or not, version, grace, expiry, rotation stage) and never a value; "
                   "the same status read every few minutes raises the rotation reminders on Health and the phone. If "
                   "its credential is wrong, keys can't be changed from the Dashboard; nothing already set stops "
                   "working.",
        "us": "Platform keys page + reminder loop",
        "peer": "Project key store",
        "zone": "estate",
        "flow": "out",
        "starts": "us",
        "kind": "api",
        "label": "platform keys out, status in",
        "endpoint": "the key store's /platform/keys (private network only)",
        "resolves": "hosts_file",
        "path": "mesh",
        "auth": "hmac_signature",
        "transport": "tls",
        "keys": [],     # filled by export() from config.PLATFORM_KEY_STORES
        "notes": "Every call is signed (HMAC-SHA256 over method, path, time, a fresh nonce and the body; "
                 "60 s skew, 30 calls a minute). The credential is ~/.config/crewboss/dash-keys-1 (0600), "
                 "mounted read-only and read at call time; one per key store, never reused. Contract: "
                 "docs/integrations/dashboard-platform-keys.md; code: app/platform_keys.py.",
        "status": "live",
        "accepted": "",
        "grade": "A",
        "why": ["Encrypted.", "Signed in with: Signed request (HMAC)."],
    },
]


def export() -> dict:
    rows = [dict(r) for r in INTEGRATIONS]
    key_ids = sorted({st["key_id"] for st in config.PLATFORM_KEY_STORES.values() if st.get("key_id")})
    for r in rows:
        if r["id"] == "dash-platform-keys":
            r["keys"] = key_ids
    return {"version": 1, "vocab": VOCAB, "modules": {"dashboard": "Crewboss"}, "integrations": rows, "systems": {}}


def shown_on(project: dict) -> bool:
    """The Dashboard's own integrations show on the Dashboard's own project (the dashboard role)."""
    return roles.is_role(project, "dashboard")
