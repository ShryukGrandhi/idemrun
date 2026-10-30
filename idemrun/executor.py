"""Run plans for many hosts on one shared worker pool.

All (host, resource) pairs form one big DAG (hosts are independent of each
other). A resource is submitted as soon as everything it depends on has
finished, so work on a slow host never holds back a fast one, and independent
resources on the same host run concurrently.

Status of each resource:
  ok            already in the desired state, nothing done
  changed       changes applied
  would_change  check mode: changes needed but not applied
  failed        inspect/apply raised
  skipped       a required resource failed or was skipped
  not_notified  handler that no notifier triggered
"""
from __future__ import annotations

import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from .planner import Plan
from .resources import Context, ResourceError

CHANGED = {"changed", "would_change"}
BLOCKING = {"failed", "skipped"}


@dataclass
class Outcome:
    host: str
    rid: str
    status: str
    changes: list[str] = field(default_factory=list)
    error: str = ""
    diff: str = ""
    duration: float = 0.0
    started: float = 0.0


@dataclass
class Report:
    outcomes: list[Outcome]
    check: bool
    elapsed: float

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.status] = out.get(o.status, 0) + 1
        return out

    @property
    def failed(self) -> bool:
        return any(o.status == "failed" for o in self.outcomes)

    @property
    def drifted(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status in CHANGED]

    def exit_code(self) -> int:
        if self.failed:
            return 2
        if self.check and self.drifted:
            return 1
        return 0


class Executor:
    def __init__(self, jobs: int = 4, check: bool = False, diff: bool = False):
        if jobs < 1:
            raise ValueError("jobs must be >= 1")
        self.jobs = jobs
        self.check = check
        self.want_diff = diff

    def _run_one(self, plan: Plan, ctx: Context, rid: str, upstream_pending: bool) -> Outcome:
        res = plan.nodes[rid]
        t0 = time.perf_counter()
        try:
            result = res.run(ctx, want_diff=self.want_diff)
            status = ("would_change" if ctx.check else "changed") if result.changed else "ok"
            out = Outcome(plan.host, rid, status, result.changes, diff=result.diff)
        except (ResourceError, OSError, UnicodeDecodeError) as exc:
            if ctx.check and upstream_pending:
                # In check mode a dependency has not really been applied yet, so
                # this resource cannot be inspected accurately. Report it as
                # pending rather than as a failure.
                out = Outcome(plan.host, rid, "would_change",
                              ["unknown until dependencies are applied"])
            else:
                out = Outcome(plan.host, rid, "failed", error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # a resource bug must not hang the scheduler
            out = Outcome(plan.host, rid, "failed", error=f"{type(exc).__name__}: {exc}")
        out.started = t0
        out.duration = time.perf_counter() - t0
        return out

    def run(self, plans: list[Plan], roots: dict[str, str]) -> Report:
        t_start = time.perf_counter()
        ctxs = {p.host: Context(p.host, roots[p.host], self.check) for p in plans}
        by_host = {p.host: p for p in plans}

        remaining: dict[tuple, int] = {}
        dependents: dict[tuple, list[tuple]] = {}
        results: dict[tuple, Outcome] = {}
        ready: deque[tuple] = deque()
        for p in plans:
            for rid in p.order:
                key = (p.host, rid)
                remaining[key] = len(p.deps(rid))
                dependents.setdefault(key, [])
                for d in p.deps(rid):
                    dependents.setdefault((p.host, d), []).append(key)
        # Seed in round-robin host order so all hosts make progress early.
        seeds = [[(p.host, rid) for rid in p.order
                  if remaining[(p.host, rid)] == 0 and not p.nodes[rid].handler] for p in plans]
        for i in range(max((len(s) for s in seeds), default=0)):
            ready.extend(s[i] for s in seeds if i < len(s))

        def finish(key: tuple, outcome: Outcome) -> None:
            """Record a result and release dependents. Dependents that must not
            run are resolved immediately (iteratively, no recursion)."""
            pending = [(key, outcome)]
            while pending:
                k, o = pending.pop()
                results[k] = o
                host, _ = k
                plan = by_host[host]
                for nxt in dependents[k]:
                    remaining[nxt] -= 1
                    if remaining[nxt]:
                        continue
                    nrid = nxt[1]
                    blocked = [d for d in plan.requires[nrid] if results[(host, d)].status in BLOCKING]
                    if blocked:
                        pending.append((nxt, Outcome(host, nrid, "skipped",
                                                     error=f"dependency not met: {', '.join(sorted(blocked))}")))
                    elif plan.nodes[nrid].handler and not any(
                            results[(host, n)].status in CHANGED for n in plan.notified_by[nrid]):
                        pending.append((nxt, Outcome(host, nrid, "not_notified")))
                    else:
                        ready.append(nxt)

        # Handlers nothing can notify are resolved up front.
        for p in plans:
            for rid in p.order:
                if p.nodes[rid].handler and remaining[(p.host, rid)] == 0:
                    finish((p.host, rid), Outcome(p.host, rid, "not_notified"))

        inflight = {}
        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            while ready or inflight:
                while ready and len(inflight) < self.jobs:
                    key = ready.popleft()
                    host, rid = key
                    plan = by_host[host]
                    upstream = any(results[(host, d)].status == "would_change" for d in plan.deps(rid))
                    fut = pool.submit(self._run_one, plan, ctxs[host], rid, upstream)
                    inflight[fut] = key
                done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:
                    finish(inflight.pop(fut), fut.result())

        order = {(p.host, rid): i for p in plans for i, rid in enumerate(p.order)}
        outcomes = sorted(results.values(), key=lambda o: (o.host, order[(o.host, o.rid)]))
        return Report(outcomes, self.check, time.perf_counter() - t_start)
