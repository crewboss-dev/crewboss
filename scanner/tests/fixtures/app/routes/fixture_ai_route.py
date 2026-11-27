"""Fixture route module: a GET handler that calls a shared helper 1 hop away from the AI function."""
from app import helper


class _Router:
    def get(self, path):
        def deco(fn):
            return fn
        return deco

    def post(self, path):
        def deco(fn):
            return fn
        return deco


router = _Router()


@router.get("/fixture/ai-thing")
def fixture_ai_page(request):
    """Calls the shared helper, which calls the AI function."""
    return helper.do_the_ai_thing({"x": 1})
