"""Human and JSON renderings of a run report."""
from __future__ import annotations

import json

from .executor import Report

MARKS = {"ok": " ", "changed": "~", "would_change": "?", "failed": "!",
         "skipped": "-", "not_notified": " "}


def to_text(report: Report, verbose: bool = False) -> str:
    lines = []
    host = None
    for o in report.outcomes:
        if not verbose and o.status in ("ok", "not_notified"):
            continue
        if o.host != host:
            host = o.host
            lines.append(f"{host}:")
        detail = ", ".join(o.changes) if o.changes else o.error
        lines.append(f"  {MARKS[o.status]} {o.status:<12} {o.rid}" + (f"  ({detail})" if detail else ""))
        if o.diff:
            lines.extend("      " + d for d in o.diff.rstrip("\n").splitlines())
    counts = report.counts()
    hosts = len({o.host for o in report.outcomes})
    summary = " ".join(f"{k}={counts[k]}" for k in sorted(counts))
    mode = "check" if report.check else "apply"
    lines.append(f"{mode}: {hosts} host(s), {len(report.outcomes)} resource(s) in "
                 f"{report.elapsed:.3f}s  {summary}")
    return "\n".join(lines)


def to_json(report: Report) -> str:
    return json.dumps({
        "mode": "check" if report.check else "apply",
        "elapsed_s": round(report.elapsed, 6),
        "counts": report.counts(),
        "exit_code": report.exit_code(),
        "resources": [
            {"host": o.host, "id": o.rid, "status": o.status, "changes": o.changes,
             "error": o.error, "duration_s": round(o.duration, 6)}
            for o in report.outcomes
        ],
    }, indent=2)
