"""Add-on seams: a registry plus the interfaces an add-on can fill, and the in-repo implementations registered as defaults.

The Dashboard is one codebase. What differs between installs — which identity provider signs people in, which monitor
feeds System Health, how a server is reached, what a first run seeds — is a named implementation of one of NINE
interfaces below (typing.Protocol, structural: an add-on needs no import of this module to satisfy one). The registry
holds the implementations by (kind, name); one per kind is the default.

    IdentityProvider    sign-in for the Dashboard's pages (Microsoft Entra OIDC today)
    MonitoringFeed      a source of estate problems for System Health (Zabbix, the ERP's health feed)
    KeyStoreDoor        a project's platform-key store, reached through its signed door
    ServerAccess        one-hour, one-server SSH passes and access checks
    AgentRuntime        what runs the standing AI sessions (the tmux launcher on the dev box)
    Theme               the look: static assets and templates
    CatalogSource       where a project's workflow catalog is built from
    InventorySource     the estate's server inventory
    BootstrapProfile    what a first run seeds

THE LOADING RULE: an add-on is loaded only when instance.yaml names it under `addons:`. load_addons() looks each NAMED
entry point up in the `dash.addons` group and loads exactly those; a package that is merely installed — even one that
advertises a `dash.addons` entry point — is never imported. A named add-on that isn't installed, or doesn't load, is a
loud error (AddonError), never a silent skip. An add-on's entry point is a callable `register(registry)`.

STEP 4 IS SCAFFOLDING: nothing calls through these interfaces yet — call sites still use the modules directly (step 5
moves them behind the registry), so registering the defaults changes no behaviour. The default classes are thin
delegates that import their module on first use."""
from __future__ import annotations

from importlib import metadata
from typing import Any, Callable, Protocol, runtime_checkable

from app import instance

ENTRY_POINT_GROUP = "dash.addons"


class AddonError(Exception):
    """A named add-on could not be found or loaded, or an implementation doesn't fit its interface."""


# --- the interfaces ---------------------------------------------------------------------------------------------------

@runtime_checkable
class IdentityProvider(Protocol):
    name: str

    def required(self) -> bool: ...
    def missing_config(self) -> list[str]: ...
    def start_flow(self, next_path: str) -> tuple[str, str]: ...
    def resolve_session(self, token: str | None) -> dict | None: ...
    def end_session(self, token: str | None) -> None: ...


@runtime_checkable
class MonitoringFeed(Protocol):
    name: str

    def is_configured(self) -> bool: ...
    def sync(self) -> dict: ...
    def summary(self) -> dict: ...


@runtime_checkable
class KeyStoreDoor(Protocol):
    name: str

    def stores(self) -> list[dict]: ...
    def get_status(self, store: dict) -> dict: ...
    def sync(self) -> dict: ...


@runtime_checkable
class ServerAccess(Protocol):
    name: str

    def list_for(self, account_ids) -> list[dict]: ...
    def issue_pass(self, account: dict, server_id: int, public_key: str, actor: str) -> dict: ...
    def check(self, account: dict, server_id: int) -> dict: ...


@runtime_checkable
class AgentRuntime(Protocol):
    name: str

    def session_plan(self) -> list[dict]: ...


@runtime_checkable
class Theme(Protocol):
    name: str

    def static_dir(self) -> str: ...
    def template_dir(self) -> str: ...


@runtime_checkable
class CatalogSource(Protocol):
    name: str

    def repos(self) -> dict[str, str]: ...
    def export_repo(self, repo_dir: str, ref: str, fetch: bool) -> tuple[Any, str]: ...


@runtime_checkable
class InventorySource(Protocol):
    name: str

    def load(self) -> dict: ...
    def summary(self, account_slug: str) -> dict: ...


@runtime_checkable
class BootstrapProfile(Protocol):
    name: str

    def run(self) -> None: ...


# kind -> its interface. The kind is the registry key an add-on registers under.
INTERFACES: dict[str, type] = {
    "identity": IdentityProvider,
    "monitoring": MonitoringFeed,
    "keystore": KeyStoreDoor,
    "server_access": ServerAccess,
    "agent_runtime": AgentRuntime,
    "theme": Theme,
    "catalog": CatalogSource,
    "inventory": InventorySource,
    "bootstrap": BootstrapProfile,
}


# --- the registry -----------------------------------------------------------------------------------------------------

class Registry:
    def __init__(self) -> None:
        self._impls: dict[str, dict[str, Any]] = {k: {} for k in INTERFACES}
        self._default: dict[str, str] = {}

    def register(self, kind: str, impl, *, name: str | None = None, default: bool = False) -> None:
        """Add `impl` under (kind, name or impl.name). The first one registered for a kind is its default unless a
        later one asks to be (`default=True`). Registering a taken name, an unknown kind, or something that doesn't fit
        the interface raises AddonError: an add-on never silently replaces an implementation."""
        if kind not in INTERFACES:
            raise AddonError(f"unknown add-on kind {kind!r} (known: {', '.join(INTERFACES)})")
        name = name or getattr(impl, "name", None)
        if not name or not isinstance(name, str):
            raise AddonError(f"{kind}: an implementation needs a name")
        if not isinstance(impl, INTERFACES[kind]):
            raise AddonError(f"{kind} {name!r} does not implement {INTERFACES[kind].__name__}")
        if name in self._impls[kind]:
            raise AddonError(f"{kind} {name!r} is already registered")
        self._impls[kind][name] = impl
        if default or kind not in self._default:
            self._default[kind] = name

    def get(self, kind: str, name: str | None = None):
        """The named implementation, or the kind's default; AddonError when there is none."""
        if kind not in INTERFACES:
            raise AddonError(f"unknown add-on kind {kind!r}")
        name = name or self._default.get(kind)
        if name is None or name not in self._impls[kind]:
            raise AddonError(f"no {kind} implementation {name!r}" if name else f"no {kind} implementation registered")
        return self._impls[kind][name]

    def names(self, kind: str) -> list[str]:
        return sorted(self._impls.get(kind, {}))

    def default_name(self, kind: str) -> str | None:
        return self._default.get(kind)


# --- the in-repo defaults ---------------------------------------------------------------------------------------------
# Thin delegates to today's modules, imported on first use (no import cost or cycle at registration).

class EntraIdentity:
    name = "entra"

    def required(self) -> bool:
        from app import web_auth
        return web_auth.required()

    def missing_config(self) -> list[str]:
        from app import web_auth
        return web_auth.missing_config()

    def start_flow(self, next_path: str) -> tuple[str, str]:
        from app import web_auth
        return web_auth.start_flow(next_path)

    def resolve_session(self, token: str | None) -> dict | None:
        from app import web_auth
        return web_auth.resolve_session(token)

    def end_session(self, token: str | None) -> None:
        from app import web_auth
        web_auth.end_session(token)


class ZabbixMonitoring:
    name = "zabbix"

    def is_configured(self) -> bool:
        from app import health
        return health.is_configured()

    def sync(self) -> dict:
        from app import health
        return health.sync()

    def summary(self) -> dict:
        from app import health
        return health.summary()


class ErpFeedMonitoring:
    name = "erp-feed"

    def is_configured(self) -> bool:
        from app import erp_health
        return erp_health.is_configured()

    def sync(self) -> dict:
        from app import erp_health
        return erp_health.sync()

    def summary(self) -> dict:
        from app import erp_health
        return erp_health.summary()


class SignedDoorKeyStore:
    name = "signed-door"

    def stores(self) -> list[dict]:
        from app import platform_keys
        return platform_keys.stores()

    def get_status(self, store: dict) -> dict:
        from app import platform_keys
        return platform_keys.get_status(store)

    def sync(self) -> dict:
        from app import platform_keys
        return platform_keys.sync()


class SshCaServerAccess:
    name = "ssh-ca"

    def list_for(self, account_ids) -> list[dict]:
        from app import server_access
        return server_access.list_for(account_ids)

    def issue_pass(self, account: dict, server_id: int, public_key: str, actor: str) -> dict:
        from app import server_access
        return server_access.issue_pass(account, server_id, public_key, actor)

    def check(self, account: dict, server_id: int) -> dict:
        from app import server_access
        return server_access.check(account, server_id)


class TmuxAgentRuntime:
    name = "tmux"

    def session_plan(self) -> list[dict]:
        from app import repo
        return repo.session_plan()


class StockTheme:
    """The look the checkout ships (the default look): app/static and app/templates."""
    name = "stock"

    def static_dir(self) -> str:
        import os
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

    def template_dir(self) -> str:
        import os
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


class RepoScanCatalog:
    name = "repo-scan"

    def repos(self) -> dict[str, str]:
        from scanner import scan
        return dict(scan.PROJECT_REPOS)

    def export_repo(self, repo_dir: str, ref: str, fetch: bool) -> tuple[Any, str]:
        from scanner import scan
        return scan.export_repo(repo_dir, ref, fetch)


class EstateFileInventory:
    """The estate's servers.yml (config.ESTATE_DIR)."""
    name = "estate-file"

    def load(self) -> dict:
        from app import servers
        return servers.load()

    def summary(self, account_slug: str) -> dict:
        from app import servers
        return servers.summary(account_slug)


class SeedBootstrap:
    name = "seed"

    def run(self) -> None:
        from app import seed
        seed.run()


def register_defaults(registry: Registry) -> Registry:
    for kind, impl in (("identity", EntraIdentity()), ("monitoring", ZabbixMonitoring()),
                       ("monitoring", ErpFeedMonitoring()), ("keystore", SignedDoorKeyStore()),
                       ("server_access", SshCaServerAccess()), ("agent_runtime", TmuxAgentRuntime()),
                       ("theme", StockTheme()), ("catalog", RepoScanCatalog()),
                       ("inventory", EstateFileInventory()), ("bootstrap", SeedBootstrap())):
        registry.register(kind, impl)
    return registry


registry = register_defaults(Registry())


# --- add-on loading ---------------------------------------------------------------------------------------------------

def configured_addons() -> list[str]:
    """The add-on names instance.yaml lists under `addons:` (none by default)."""
    return instance.get_list("addons")


def _entry_points() -> list:
    return list(metadata.entry_points(group=ENTRY_POINT_GROUP))


def load_addons(names: list[str] | None = None, target: Registry | None = None,
                entry_points: Callable[[], list] | None = None) -> list[str]:
    """Load the NAMED add-ons only: for each name, find its `dash.addons` entry point, load it and call it with the
    registry. Entry points nobody named are never loaded (not even imported). Returns the names loaded."""
    names = configured_addons() if names is None else list(names)
    if not names:
        return []
    target = registry if target is None else target
    available = {ep.name: ep for ep in (entry_points or _entry_points)()}
    loaded = []
    for name in names:
        ep = available.get(name)
        if ep is None:
            raise AddonError(f"add-on {name!r} is named in instance.yaml but no {ENTRY_POINT_GROUP!r} entry point "
                             f"of that name is installed")
        try:
            register = ep.load()
        except Exception as e:  # noqa: BLE001 - any import failure is the add-on's, reported by name
            raise AddonError(f"add-on {name!r} failed to load: {e}") from e
        if not callable(register):
            raise AddonError(f"add-on {name!r}: its entry point must be a callable register(registry)")
        register(target)
        loaded.append(name)
    return loaded
