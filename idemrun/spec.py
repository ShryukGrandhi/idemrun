"""Loading specs (desired state) and inventories (hosts) from TOML.

A spec is a list of resource definitions with `{{ var }}` placeholders. It is
rendered once per host with that host's variables, producing concrete Resource
objects. Variable precedence, lowest to highest:
spec [vars] < inventory [vars] < per-host vars < built-in `host`.
"""
from __future__ import annotations

import fnmatch
import os
import re
import tomllib
from dataclasses import dataclass, field

from .resources import TYPES, Resource, ResourceError

META_KEYS = {"type", "name", "requires", "notify", "handler", "hosts"}
_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class SpecError(Exception):
    pass


def render(value, variables: dict, where: str = ""):
    """Substitute {{ name }} in strings (recursively in lists); undefined names are errors."""
    if isinstance(value, str):
        def sub(m):
            key = m.group(1)
            if key not in variables:
                raise SpecError(f"{where}: undefined variable {key!r}")
            return str(variables[key])
        return _VAR.sub(sub, value)
    if isinstance(value, list):
        return [render(v, variables, where) for v in value]
    return value


@dataclass
class ResourceDef:
    kind: str
    name: str | None
    params: dict
    requires: list[str]
    notify: list[str]
    handler: bool
    hosts: list[str]
    index: int

    def targets(self, host: str) -> bool:
        return any(fnmatch.fnmatchcase(host, pat) for pat in self.hosts)


@dataclass
class Spec:
    vars: dict
    defs: list[ResourceDef]
    base_dir: str = "."


@dataclass
class Host:
    name: str
    root: str
    vars: dict = field(default_factory=dict)


def _as_list(value, where):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise SpecError(f"{where}: expected a string or list of strings")


def parse_spec(data: dict, base_dir: str = ".") -> Spec:
    unknown = set(data) - {"vars", "resource"}
    if unknown:
        raise SpecError(f"unknown top-level key(s): {sorted(unknown)}")
    defs = []
    for i, raw in enumerate(data.get("resource", [])):
        where = f"resource #{i + 1}"
        kind = raw.get("type")
        if kind not in TYPES:
            raise SpecError(f"{where}: unknown or missing type {kind!r} (have: {sorted(TYPES)})")
        params = {k: v for k, v in raw.items() if k not in META_KEYS}
        if "source" in params:
            if kind != "file" or "content" in params:
                raise SpecError(f"{where}: 'source' is only valid on a file without 'content'")
            src = os.path.join(base_dir, params.pop("source"))
            with open(src, encoding="utf-8") as fh:
                params["content"] = fh.read()
        name = raw.get("name")
        if name is None and "path" not in params:
            raise SpecError(f"{where}: {kind} needs a 'name'")
        defs.append(ResourceDef(
            kind=kind, name=name, params=params,
            requires=_as_list(raw.get("requires"), where),
            notify=_as_list(raw.get("notify"), where),
            handler=bool(raw.get("handler", False)),
            hosts=_as_list(raw.get("hosts", "*"), where),
            index=i,
        ))
    return Spec(vars=dict(data.get("vars", {})), defs=defs, base_dir=base_dir)


def load_spec(path: str) -> Spec:
    with open(path, "rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise SpecError(f"{path}: {exc}") from exc
    return parse_spec(data, os.path.dirname(os.path.abspath(path)))


def load_inventory(path: str) -> list[Host]:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    base = os.path.dirname(os.path.abspath(path))
    shared = dict(data.get("vars", {}))
    hosts = []
    for name, cfg in sorted(data.get("hosts", {}).items()):
        if "root" not in cfg:
            raise SpecError(f"inventory host {name!r} has no root")
        host_vars = {k: v for k, v in cfg.items() if k != "root"}
        hosts.append(Host(name, os.path.abspath(os.path.join(base, cfg["root"])),
                          {**shared, **host_vars}))
    if not hosts:
        raise SpecError(f"{path}: no hosts defined")
    return hosts


def instantiate(spec: Spec, host: Host) -> list[Resource]:
    """Render every definition that targets `host` into a concrete Resource."""
    variables = {**spec.vars, **host.vars, "host": host.name}
    out = []
    for d in spec.defs:
        if not d.targets(host.name):
            continue
        where = f"{host.name}: resource #{d.index + 1}"
        params = {k: render(v, variables, where) for k, v in d.params.items()}
        name = render(d.name, variables, where) if d.name else os.path.normpath(params["path"])
        try:
            out.append(TYPES[d.kind](
                name, params,
                requires=render(d.requires, variables, where),
                notify=render(d.notify, variables, where),
                handler=d.handler, index=d.index))
        except ResourceError as exc:
            raise SpecError(f"{where}: {exc}") from exc
    return out
