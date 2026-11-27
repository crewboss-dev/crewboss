"""Fixture mimicking app/routes/webhook.py's @router.post("/webhook/...") shape."""
from app.clients import stripe as stripeclient


class _Router:
    def post(self, path):
        def deco(fn):
            fn._route_path = path
            return fn
        return deco


router = _Router()


@router.post("/webhook/fixture")
async def fixture_webhook(request):
    """A fixture webhook that calls a client and writes a table."""
    stripeclient.verify_webhook(b"", "")
    return {"sql": "UPDATE fixture_invoices SET status='paid' WHERE id=%s"}
