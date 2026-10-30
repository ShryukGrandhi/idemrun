"""Benchmark idemrun on a generated fleet.

Three measurements:
  1. converge   cold apply of a generated fleet, then a second apply (must make
                zero changes) and a check run; throughput in resources/second.
  2. drift      randomly tamper with K managed objects across the fleet (edit
                content, chmod, delete, retarget symlinks, remove lines), run
                `check` and compare what it flags with what was tampered with.
  3. scheduling slow command resources (each sleeps) in a layered DAG; wall
                time for different --jobs values versus jobs=1.

Usage: python bench/bench.py [--quick] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from idemrun import Executor, Host, parse_spec, plan_fleet  # noqa: E402
from idemrun.planner import critical_path  # noqa: E402

LINE_FILES = 4


def fleet_spec(dirs: int, files: int, lines: int, links: int) -> dict:
    res = [{"type": "directory", "path": f"srv/d{d}", "mode": "0755"} for d in range(dirs)]
    for f in range(files):
        res.append({"type": "file", "path": f"srv/d{f % dirs}/f{f}.conf", "mode": "0644",
                    "content": f"# file {f}\nhost = {{{{host}}}}\nshard = {{{{shard}}}}\nkey{f} = {f * 7}\n"})
    for l in range(lines):
        res.append({"type": "line", "name": f"l{l}", "path": f"etc/settings{l % LINE_FILES}.conf",
                    "line": f"opt{l} = {{{{shard}}}}", "match": f"^opt{l} =", "create": True})
    res.append({"type": "directory", "path": "etc"})
    for s in range(links):
        res.append({"type": "symlink", "path": f"srv/link{s}", "target": f"d{s % dirs}"})
    return {"resource": res}


def make_hosts(base: str, n: int) -> list[Host]:
    hosts = []
    for i in range(n):
        root = os.path.join(base, f"host{i:03d}")
        os.makedirs(root)
        hosts.append(Host(f"host{i:03d}", root, {"shard": i % 7}))
    return hosts


def run(spec, hosts, jobs, check=False):
    plans, roots = plan_fleet(spec, hosts)
    return Executor(jobs=jobs, check=check).run(plans, roots)


def bench_converge(base, n_hosts, shape, jobs):
    spec = parse_spec(fleet_spec(**shape))
    hosts = make_hosts(base, n_hosts)
    t = time.perf_counter()
    plans, _ = plan_fleet(spec, hosts)
    plan_s = time.perf_counter() - t
    total = sum(len(p.order) for p in plans)

    cold = run(spec, hosts, jobs)
    warm = run(spec, hosts, jobs)
    chk = run(spec, hosts, jobs, check=True)
    return spec, hosts, {
        "hosts": n_hosts, "resources": total, "jobs": jobs,
        "plan_s": plan_s,
        "cold_apply_s": cold.elapsed, "cold_changed": cold.counts().get("changed", 0),
        "cold_failed": cold.counts().get("failed", 0),
        "second_apply_s": warm.elapsed, "second_changed": warm.counts().get("changed", 0),
        "check_s": chk.elapsed, "check_drift": len(chk.drifted),
        "cold_res_per_s": total / cold.elapsed, "second_res_per_s": total / warm.elapsed,
    }


def tamper(rng, hosts, spec, k):
    """Apply k random mutations, each to a distinct managed object. Returns the
    set of (host, resource id) that should now be reported as drifted."""
    plans, _ = plan_fleet(spec, hosts)
    candidates = [(h, p.nodes[rid]) for h, p in zip(hosts, plans) for rid in p.order
                  if p.nodes[rid].kind in ("file", "symlink", "line")]
    expected = set()
    touched_paths = set()
    for host, res in rng.sample(candidates, len(candidates)):
        if len(expected) == k:
            break
        path = os.path.join(host.root, res.params["path"])
        if (host.name, path) in touched_paths:
            continue  # keep one mutation per file so ground truth stays exact
        if res.kind == "file":
            how = rng.choice(["content", "mode", "delete"])
            if how == "content":
                with open(path, "a") as fh:
                    fh.write("# hand edit\n")
            elif how == "mode":
                os.chmod(path, 0o600)
            else:
                os.unlink(path)
        elif res.kind == "symlink":
            os.unlink(path)
            os.symlink("elsewhere", path)
        else:  # remove our line from the shared settings file
            with open(path) as fh:
                kept = [l for l in fh if l.rstrip("\n") != res.params["line"]]
            with open(path, "w") as fh:
                fh.writelines(kept)
        touched_paths.add((host.name, path))
        expected.add((host.name, res.id))
    return expected


def bench_drift(spec, hosts, k, jobs, seed):
    rng = random.Random(seed)
    expected = tamper(rng, hosts, spec, k)
    chk = run(spec, hosts, jobs, check=True)
    found = {(o.host, o.rid) for o in chk.drifted}
    tp = len(found & expected)
    fix = run(spec, hosts, jobs)
    after = run(spec, hosts, jobs, check=True)
    return {
        "tampered": len(expected), "flagged": len(found), "true_positives": tp,
        "precision": tp / len(found) if found else 1.0,
        "recall": tp / len(expected) if expected else 1.0,
        "check_s": chk.elapsed,
        "repaired": fix.counts().get("changed", 0), "drift_after_repair": len(after.drifted),
    }


def bench_scheduling(base, n_hosts, layers, width, sleep_s, jobs_list):
    res = []
    for layer in range(layers):
        for w in range(width):
            r = {"type": "command", "name": f"c{layer}_{w}", "cmd": f"sleep {sleep_s}"}
            if layer:
                r["requires"] = [f"c{layer - 1}_{x}" for x in range(width)]
            res.append(r)
    spec = parse_spec({"resource": res})
    hosts = make_hosts(base, n_hosts)
    plans, _ = plan_fleet(spec, hosts)
    lower_bound = critical_path(plans[0], {rid: sleep_s for rid in plans[0].order})
    rows = []
    base_t = None
    for jobs in jobs_list:
        rep = run(spec, hosts, jobs)
        assert not rep.failed, rep.outcomes
        base_t = base_t or rep.elapsed
        rows.append({"jobs": jobs, "wall_s": rep.elapsed, "speedup": base_t / rep.elapsed})
    return {"hosts": n_hosts, "commands": len(res) * n_hosts, "layers": layers, "width": width,
            "sleep_s": sleep_s, "critical_path_s": lower_bound, "runs": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="small sizes for CI")
    ap.add_argument("--json", help="write results to this file")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    if args.quick:
        n_hosts, shape, k, sched = 10, dict(dirs=5, files=20, lines=8, links=4), 20, (4, 3, 4)
    else:
        n_hosts, shape, k, sched = 50, dict(dirs=10, files=40, lines=20, links=10), 100, (8, 3, 4)
    jobs = 8
    out = {"python": platform.python_version(), "platform": platform.platform(),
           "cpus": os.cpu_count(), "quick": args.quick}
    base = tempfile.mkdtemp(prefix="idemrun-bench-")
    try:
        spec, hosts, out["converge"] = bench_converge(os.path.join(base, "fleet"), n_hosts, shape, jobs)
        out["drift"] = bench_drift(spec, hosts, k, jobs, args.seed)
        out["scheduling"] = bench_scheduling(os.path.join(base, "sched"), sched[0], sched[1], sched[2],
                                             0.05, [1, 4, 16, 64])
    finally:
        shutil.rmtree(base)

    c, d, s = out["converge"], out["drift"], out["scheduling"]
    print(f"python {out['python']} on {out['platform']} ({out['cpus']} cpus)\n")
    print(f"## converge: {c['hosts']} hosts, {c['resources']} resources, jobs={c['jobs']}")
    print("| run | wall (s) | resources/s | changed |")
    print("|---|---|---|---|")
    print(f"| plan (render + DAG) | {c['plan_s']:.3f} | - | - |")
    print(f"| first apply | {c['cold_apply_s']:.3f} | {c['cold_res_per_s']:.0f} | {c['cold_changed']} |")
    print(f"| second apply | {c['second_apply_s']:.3f} | {c['second_res_per_s']:.0f} | {c['second_changed']} |")
    print(f"| check | {c['check_s']:.3f} | {c['resources'] / c['check_s']:.0f} | {c['check_drift']} drifted |")
    print(f"\n## drift detection: {d['tampered']} random tamperings")
    print(f"flagged {d['flagged']}, true positives {d['true_positives']}, "
          f"precision {d['precision']:.3f}, recall {d['recall']:.3f}, check {d['check_s']:.3f}s; "
          f"apply repaired {d['repaired']}, drift after repair {d['drift_after_repair']}")
    print(f"\n## scheduling: {s['hosts']} hosts x {s['layers']} layers x {s['width']} commands "
          f"(sleep {s['sleep_s']}s each, {s['commands']} total); per-host critical path {s['critical_path_s']:.2f}s")
    print("| jobs | wall (s) | speedup vs jobs=1 |")
    print("|---|---|---|")
    for r in s["runs"]:
        print(f"| {r['jobs']} | {r['wall_s']:.3f} | {r['speedup']:.1f}x |")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=2)

    ok = (c["cold_failed"] == 0 and c["second_changed"] == 0 and c["check_drift"] == 0
          and d["recall"] == 1.0 and d["precision"] == 1.0 and d["drift_after_repair"] == 0)
    if not ok:
        print("\nFAIL: idempotence or drift-detection check did not hold", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
