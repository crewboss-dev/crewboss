"""Add-on registry (app/plugins.py): the nine interfaces, the in-repo defaults, and the loading rule — only add-ons
named in instance.yaml are ever loaded; an installed package that nobody named is never imported."""
import importlib
import inspect

import pytest

from app import plugins
from app.plugins import AddonError, Registry


class FakeEntryPoint:
    def __init__(self, name, target=None, boom=None):
        self.name, self._target, self._boom, self.loaded = name, target, boom, False

    def load(self):
        self.loaded = True
        if self._boom:
            raise self._boom
        return self._target


class GoodTheme:
    name = "night"

    def static_dir(self):
        return "/s"

    def template_dir(self):
        return "/t"


def test_every_kind_has_a_default_that_fits_its_interface():
    assert set(plugins.INTERFACES) == {"identity", "monitoring", "keystore", "server_access", "agent_runtime",
                                       "theme", "catalog", "inventory", "bootstrap"}
    for kind, iface in plugins.INTERFACES.items():
        impl = plugins.registry.get(kind)
        assert isinstance(impl, iface), kind
        assert plugins.registry.default_name(kind) == impl.name
    assert plugins.registry.names("monitoring") == ["erp-feed", "zabbix"]
    assert plugins.registry.default_name("monitoring") == "zabbix"


def test_defaults_delegate_to_functions_that_exist():
    """The defaults are lazy delegates: each method must name a real function of the module it wraps, with the arity it
    passes. Importing is enough; nothing is called."""
    wraps = {
        plugins.EntraIdentity: "app.web_auth", plugins.ZabbixMonitoring: "app.health",
        plugins.ErpFeedMonitoring: "app.erp_health", plugins.SignedDoorKeyStore: "app.platform_keys",
        plugins.SshCaServerAccess: "app.server_access", plugins.EstateFileInventory: "app.servers",
        plugins.SeedBootstrap: "app.seed", plugins.RepoScanCatalog: "scanner.scan", plugins.TmuxAgentRuntime: "app.repo",
    }
    for cls, modname in wraps.items():
        mod = importlib.import_module(modname)
        for meth, fn in inspect.getmembers(cls, inspect.isfunction):
            if meth.startswith("_") or meth == "repos":      # repos() reads a constant, not a function
                continue
            target = getattr(mod, meth, None)
            assert callable(target), f"{cls.__name__}.{meth} -> {modname}.{meth}"
            ours = [p for p in inspect.signature(fn).parameters if p != "self"]
            theirs = inspect.signature(target).parameters
            assert len(ours) <= len(theirs), f"{cls.__name__}.{meth} passes more arguments than {modname}.{meth} takes"


def test_theme_default_points_at_the_shipped_assets():
    import os
    theme = plugins.registry.get("theme")
    assert os.path.isdir(theme.static_dir()) and os.path.isdir(theme.template_dir())


def test_catalog_default_reads_the_instance_repos():
    from app import instance
    assert plugins.registry.get("catalog").repos() == dict(instance.get_map("project_repos"))


def test_register_rules():
    reg = plugins.register_defaults(Registry())
    reg.register("theme", GoodTheme())
    assert reg.default_name("theme") == "stock"             # the first registered stays the default…
    reg.register("theme", GoodTheme(), name="night2", default=True)
    assert reg.default_name("theme") == "night2" and reg.get("theme", "night").name == "night"
    with pytest.raises(AddonError):
        reg.register("theme", GoodTheme())                  # a taken name is never replaced
    with pytest.raises(AddonError):
        reg.register("nonsense", GoodTheme())
    with pytest.raises(AddonError):
        reg.register("identity", GoodTheme())               # doesn't fit IdentityProvider
    with pytest.raises(AddonError):
        reg.get("theme", "missing")
    with pytest.raises(AddonError):
        Registry().get("theme")                             # nothing registered


def test_only_named_addons_are_loaded():
    reg = plugins.register_defaults(Registry())
    wanted = FakeEntryPoint("night-theme", lambda r: r.register("theme", GoodTheme()))
    bystander = FakeEntryPoint("bystander", lambda r: pytest.fail("an unnamed add-on was called"))
    eps = lambda: [wanted, bystander]  # noqa: E731
    assert plugins.load_addons(["night-theme"], reg, eps) == ["night-theme"]
    assert wanted.loaded and not bystander.loaded           # the bystander was never even imported
    assert reg.get("theme", "night").static_dir() == "/s"


def test_nothing_named_loads_nothing():
    bystander = FakeEntryPoint("bystander", lambda r: pytest.fail("loaded"))
    assert plugins.load_addons([], Registry(), lambda: [bystander]) == []
    assert not bystander.loaded


def test_a_named_addon_that_is_missing_or_broken_is_a_loud_error():
    with pytest.raises(AddonError, match="no 'dash.addons' entry point"):
        plugins.load_addons(["ghost"], Registry(), lambda: [])
    broken = FakeEntryPoint("broken", boom=ImportError("no module x"))
    with pytest.raises(AddonError, match="failed to load"):
        plugins.load_addons(["broken"], Registry(), lambda: [broken])
    notcallable = FakeEntryPoint("odd", target=42)
    with pytest.raises(AddonError, match="callable"):
        plugins.load_addons(["odd"], Registry(), lambda: [notcallable])


def test_names_come_from_instance_yaml_only(monkeypatch):
    monkeypatch.setitem(plugins.instance.DATA, "addons", ["from-yaml"])
    assert plugins.configured_addons() == ["from-yaml"]
    ep = FakeEntryPoint("from-yaml", lambda r: None)
    assert plugins.load_addons(target=Registry(), entry_points=lambda: [ep]) == ["from-yaml"]
    monkeypatch.delitem(plugins.instance.DATA, "addons")
    assert plugins.configured_addons() == []
