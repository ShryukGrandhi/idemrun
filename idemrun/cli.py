"""Command line: idemrun {plan,apply,check} SPEC [-i INVENTORY | --root DIR]."""
from __future__ import annotations

import argparse
import fnmatch
import os
import sys

from . import Executor, Host, PlanError, ResourceError, SpecError, load_inventory, load_spec, plan_fleet
from .report import to_json, to_text


def _hosts(args) -> list[Host]:
    if args.inventory:
        hosts = load_inventory(args.inventory)
    else:
        hosts = [Host("localhost", os.path.abspath(args.root))]
    if args.limit:
        hosts = [h for h in hosts if fnmatch.fnmatchcase(h.name, args.limit)]
        if not hosts:
            raise SpecError(f"--limit {args.limit!r} matched no hosts")
    return hosts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="idemrun", description=__doc__)
    ap.add_argument("command", choices=["plan", "apply", "check"],
                    help="plan: show execution order; apply: converge; check: dry run / drift report")
    ap.add_argument("spec", help="desired-state spec (TOML)")
    ap.add_argument("-i", "--inventory", help="inventory of hosts (TOML)")
    ap.add_argument("--root", default=".", help="host root when no inventory is given")
    ap.add_argument("-l", "--limit", help="only hosts matching this glob")
    ap.add_argument("-j", "--jobs", type=int, default=8, help="worker threads (default 8)")
    ap.add_argument("--diff", action="store_true", help="show content diffs for files")
    ap.add_argument("--json", action="store_true", help="print a JSON report")
    ap.add_argument("-v", "--verbose", action="store_true", help="also list unchanged resources")
    args = ap.parse_args(argv)

    try:
        hosts = _hosts(args)
        plans, roots = plan_fleet(load_spec(args.spec), hosts)
    except (SpecError, PlanError, ResourceError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    if args.command == "plan":
        for p in plans:
            print(f"{p.host}:")
            for i, rid in enumerate(p.order, 1):
                deps = sorted(p.deps(rid))
                print(f"  {i:3d}. {rid}" + (f"  <- {', '.join(deps)}" if deps else ""))
        return 0

    if args.command == "apply":
        for root in roots.values():
            os.makedirs(root, exist_ok=True)
    report = Executor(jobs=args.jobs, check=args.command == "check", diff=args.diff).run(plans, roots)
    print(to_json(report) if args.json else to_text(report, verbose=args.verbose))
    return report.exit_code()
