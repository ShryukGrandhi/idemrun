"""Build a dependency graph for one host and order it.

Edges come from three places:
  * explicit `requires`;
  * `notify` (a handler runs after everything that can notify it);
  * auto-requires: a resource under a managed directory depends on that
    directory, and a `line` edit depends on the `file` resource for that path.

The planner also rejects specs that can never converge: unknown references,
two resources owning the same path, a `line` edit inside a file whose whole
content is managed by a `file` resource, notify targets that are not handlers, and
dependency cycles (reported with the actual cycle).
"""
from __future__ import annotations

import heapq
import os
from dataclasses import dataclass, field

from .resources import Resource


class PlanError(Exception):
    pass


@dataclass
class Plan:
    host: str
    nodes: dict[str, Resource]
    requires: dict[str, set[str]] = field(default_factory=dict)   # hard deps
    notified_by: dict[str, set[str]] = field(default_factory=dict)  # handler -> notifiers
    order: list[str] = field(default_factory=list)

    def deps(self, rid: str) -> set[str]:
        return self.requires[rid] | self.notified_by[rid]


def _resolve_ref(ref: str, nodes: dict[str, Resource], by_name: dict[str, list[str]], src: str) -> str:
    if ref in nodes:
        return ref
    hits = by_name.get(ref, [])
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise PlanError(f"{src}: reference {ref!r} is ambiguous: {sorted(hits)}")
    raise PlanError(f"{src}: reference {ref!r} does not match any resource")


def _find_cycle(nodes: list[str], deps: dict[str, set[str]]) -> list[str]:
    color = {n: 0 for n in nodes}
    stack: list[str] = []

    def dfs(n):
        color[n] = 1
        stack.append(n)
        for d in sorted(deps[n]):
            if color.get(d) == 1:
                return stack[stack.index(d):] + [d]
            if color.get(d) == 0:
                found = dfs(d)
                if found:
                    return found
        stack.pop()
        color[n] = 2
        return None

    for n in nodes:
        if color[n] == 0:
            found = dfs(n)
            if found:
                return found
    return []


def build_plan(host: str, resources: list[Resource]) -> Plan:
    nodes: dict[str, Resource] = {}
    by_name: dict[str, list[str]] = {}
    for r in resources:
        if r.id in nodes:
            raise PlanError(f"{host}: duplicate resource {r.id}")
        nodes[r.id] = r
        by_name.setdefault(r.name, []).append(r.id)

    plan = Plan(host, nodes)
    for rid in nodes:
        plan.requires[rid] = set()
        plan.notified_by[rid] = set()

    owners: dict[str, str] = {}
    dirs: dict[str, str] = {}
    files: dict[str, str] = {}
    for r in resources:
        for p in r.paths():
            if p in owners:
                raise PlanError(f"{host}: {r.id} and {owners[p]} both manage path {p!r}")
            owners[p] = r.id
            if r.kind == "directory":
                dirs[p] = r.id
            elif r.kind == "file":
                files[p] = r.id

    for r in resources:
        src = f"{host}: {r.id}"
        for ref in r.requires:
            plan.requires[r.id].add(_resolve_ref(ref, nodes, by_name, src))
        for ref in r.notify:
            target = _resolve_ref(ref, nodes, by_name, src)
            if not nodes[target].handler:
                raise PlanError(f"{src}: notifies {target}, which is not a handler")
            plan.notified_by[target].add(r.id)
        # auto-requires
        own = r.paths() or ([os.path.normpath(r.params["path"])] if "path" in r.params else [])
        for p in own:
            parent = os.path.dirname(p)
            while parent:
                if parent in dirs and dirs[parent] != r.id and r.kind != "absent":
                    plan.requires[r.id].add(dirs[parent])
                    break
                parent = os.path.dirname(parent)
            if r.kind == "line" and p in files:
                raise PlanError(f"{src}: edits {p!r}, whose whole content is managed by "
                                f"{files[p]}; the two would undo each other on every run")

    for rid, r in nodes.items():
        if not r.handler and plan.notified_by[rid]:
            raise PlanError(f"{host}: {rid} is notified but is not a handler")

    # Kahn's algorithm; ties broken by declaration order so plans are stable.
    indeg = {rid: len(plan.deps(rid)) for rid in nodes}
    dependents: dict[str, list[str]] = {rid: [] for rid in nodes}
    for rid in nodes:
        for d in plan.deps(rid):
            dependents[d].append(rid)
    heap = [(nodes[rid].index, rid) for rid, n in indeg.items() if n == 0]
    heapq.heapify(heap)
    while heap:
        _, rid = heapq.heappop(heap)
        plan.order.append(rid)
        for nxt in dependents[rid]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                heapq.heappush(heap, (nodes[nxt].index, nxt))
    if len(plan.order) != len(nodes):
        stuck = [rid for rid in nodes if indeg[rid] > 0]
        cycle = _find_cycle(stuck, {rid: plan.deps(rid) for rid in nodes})
        raise PlanError(f"{host}: dependency cycle (a -> b means a requires b): "
                        f"{' -> '.join(cycle)}")
    return plan


def critical_path(plan: Plan, cost: dict[str, float]) -> float:
    """Longest weighted path through the DAG: the best possible wall time with
    unlimited workers."""
    finish: dict[str, float] = {}
    for rid in plan.order:
        start = max((finish[d] for d in plan.deps(rid)), default=0.0)
        finish[rid] = start + cost.get(rid, 0.0)
    return max(finish.values(), default=0.0)
