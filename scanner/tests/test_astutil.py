import ast

from scanner.lib import astutil as A


def test_slugify():
    assert A.slugify("Xero Token Refresh") == "xero-token-refresh"
    assert A.slugify("email.qualify") == "email-qualify"


def test_module_level_assigns_joins_implicit_concat():
    mod_src = 'X = (\n    "a"\n    "b"\n)\n'
    tree = ast.parse(mod_src)

    class Fake:
        pass

    m = Fake()
    m.tree = tree
    consts = A.module_level_assigns(m)
    assert A.const_str(consts["X"]) == "ab"


def test_describe_context_dict_get_and_bare_names():
    src = 'user = f"{alert.get(\'title\')} {weather}"'
    tree = ast.parse(src, mode="exec")
    assign = tree.body[0]
    out = A.describe_context(assign.value, ["extra"])
    assert "alert title" in out
    assert "weather" in out
    assert "extra" in out


def test_decorator_calls_extracts_string_arg():
    src = '@executor("email.triage.case")\ndef f(cur, decision_id, action):\n    pass\n'
    tree = ast.parse(src)
    fn = tree.body[0]
    calls = A.decorator_calls(fn)
    assert calls == [("executor", ["email.triage.case"], {})]
