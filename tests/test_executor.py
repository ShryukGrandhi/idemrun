import os
import shutil
import threading
import time

import pytest

from idemrun import Executor, Host, build_plan, instantiate, parse_spec
from idemrun.resources import Resource

from conftest import converge, statuses


def test_converge_then_idempotent(host):
    data = {"resource": [
        {"type": "directory", "path": "etc"},
        {"type": "file", "path": "etc/a", "content": "1\n"},
        {"type": "line", "path": "etc/b", "line": "k=v", "create": True},
        {"type": "symlink", "path": "cur", "target": "etc"},
    ]}
    first = converge(data, [host])
    assert set(statuses(first).values()) == {"changed"}
    second = converge(data, [host])
    assert set(statuses(second).values()) == {"ok"}
    assert second.exit_code() == 0


def test_failure_skips_dependents_only(host):
    report = converge({"resource": [
        {"type": "command", "name": "bad", "cmd": "exit 1"},
        {"type": "command", "name": "after-bad", "cmd": "touch x", "requires": "bad"},
        {"type": "command", "name": "after-after", "cmd": "touch y", "requires": "after-bad"},
        {"type": "command", "name": "independent", "cmd": "touch z"},
    ]}, [host])
    s = statuses(report)
    assert s == {"command[bad]": "failed", "command[after-bad]": "skipped",
                 "command[after-after]": "skipped", "command[independent]": "changed"}
    assert os.path.exists(os.path.join(host.root, "z"))
    assert not os.path.exists(os.path.join(host.root, "x"))
    assert report.exit_code() == 2


def test_failure_on_one_host_does_not_affect_others(tmp_path):
    hosts = [Host(f"h{i}", str(tmp_path / f"h{i}")) for i in range(3)]
    for h in hosts:
        os.makedirs(h.root)
    report = converge({"resource": [
        {"type": "command", "name": "maybe", "cmd": 'test "$IDEMRUN_HOST" != h1'},
        {"type": "file", "path": "done", "content": "", "requires": "maybe"},
    ]}, hosts)
    assert statuses(report, "h1") == {"command[maybe]": "failed", "file[done]": "skipped"}
    assert statuses(report, "h0")["file[done]"] == "changed"
    assert statuses(report, "h2")["file[done]"] == "changed"


SPEC_WITH_HANDLER = {"resource": [
    {"type": "file", "path": "app.conf", "content": "{{v}}\n", "notify": "reload"},
    {"type": "file", "path": "other", "content": "x", "notify": "reload"},
    {"type": "command", "name": "reload", "handler": True, "cmd": "echo r >> reloads"},
    {"type": "command", "name": "orphan", "handler": True, "cmd": "touch orphan"},
]}


def test_handlers_run_once_only_when_notified(host):
    reloads = os.path.join(host.root, "reloads")
    data = {**SPEC_WITH_HANDLER, "vars": {"v": 1}}
    s = statuses(converge(data, [host]))
    assert s["command[reload]"] == "changed" and s["command[orphan]"] == "not_notified"
    assert open(reloads).read() == "r\n"  # two notifiers, one run
    assert statuses(converge(data, [host]))["command[reload]"] == "not_notified"
    converge({**SPEC_WITH_HANDLER, "vars": {"v": 2}}, [host])
    assert open(reloads).read() == "r\nr\n"
    assert not os.path.exists(os.path.join(host.root, "orphan"))


def test_check_mode_writes_nothing_and_reports_drift(host):
    data = {"resource": [
        {"type": "directory", "path": "d"},
        {"type": "file", "path": "d/f", "content": "x"},
        {"type": "command", "name": "seed", "cmd": "echo a > d/g", "creates": "d/g", "requires": "directory[d]"},
        {"type": "line", "path": "d/g", "line": "y", "requires": "seed"},  # d/g does not exist yet
    ]}
    report = converge(data, [host], check=True)
    assert set(statuses(report).values()) == {"would_change"}
    line = [o for o in report.outcomes if o.rid == "line[d/g]"][0]
    assert line.changes == ["unknown until dependencies are applied"]
    assert os.listdir(host.root) == []
    assert report.exit_code() == 1

    converge(data, [host])
    assert converge(data, [host], check=True).exit_code() == 0
    with open(os.path.join(host.root, "d/f"), "w") as fh:
        fh.write("tampered\n")
    drift = converge(data, [host], check=True)
    assert [(o.rid, o.changes) for o in drift.drifted] == [("file[d/f]", ["content"])]


def test_check_mode_real_errors_still_fail(host):
    report = converge({"resource": [{"type": "line", "path": "missing", "line": "x"}]}, [host], check=True)
    assert statuses(report) == {"line[missing]": "failed"}


class Probe(Resource):
    """Test resource that records concurrency and timing."""
    kind = "probe"
    schema = {"sleep": 0.0}
    lock = threading.Lock()
    active = 0
    peak = 0
    log: list = []

    def inspect(self, ctx):
        return ["run"]

    def apply(self, ctx, changes):
        cls = type(self)
        with cls.lock:
            cls.active += 1
            cls.peak = max(cls.peak, cls.active)
            cls.log.append(("start", ctx.host, self.name, time.perf_counter()))
        time.sleep(self.params["sleep"])
        with cls.lock:
            cls.active -= 1
            cls.log.append(("end", ctx.host, self.name, time.perf_counter()))


@pytest.fixture
def probe_plans(tmp_path):
    Probe.active = Probe.peak = 0
    Probe.log = []

    def make(n_hosts, edges, n=6, sleep=0.03):
        plans, roots = [], {}
        for h in range(n_hosts):
            res = [Probe(f"p{i}", {"sleep": sleep}, requires=[f"probe[p{j}]" for j in edges.get(i, [])], index=i)
                   for i in range(n)]
            plans.append(build_plan(f"h{h}", res))
            roots[f"h{h}"] = str(tmp_path)
        return plans, roots
    return make


def test_dependencies_respected_under_concurrency(probe_plans):
    edges = {1: [0], 2: [0], 3: [1, 2], 5: [4]}
    plans, roots = probe_plans(4, edges)
    report = Executor(jobs=8).run(plans, roots)
    assert not report.failed
    t = {(e[1], e[2], e[0]): e[3] for e in Probe.log}
    for h in range(4):
        for child, parents in edges.items():
            for p in parents:
                assert t[(f"h{h}", f"p{p}", "end")] <= t[(f"h{h}", f"p{child}", "start")]
    assert Probe.peak > 1


def test_jobs_bounds_concurrency(probe_plans):
    plans, roots = probe_plans(3, {}, n=8, sleep=0.01)
    Executor(jobs=3).run(plans, roots)
    assert Probe.peak <= 3


def test_parallel_speedup(probe_plans):
    plans, roots = probe_plans(4, {}, n=4, sleep=0.05)
    serial = Executor(jobs=1).run(plans, roots).elapsed
    parallel = Executor(jobs=16).run(plans, roots).elapsed
    assert parallel < serial / 3


def test_resource_bug_reported_not_hung(host):
    class Buggy(Resource):
        kind = "buggy"
        schema = {}

        def inspect(self, ctx):
            raise KeyError("oops")
    plan = build_plan("h1", [Buggy("b", {})])
    report = Executor(jobs=2).run([plan], {"h1": host.root})
    assert report.outcomes[0].status == "failed" and "KeyError" in report.outcomes[0].error


def test_invalid_jobs():
    with pytest.raises(ValueError):
        Executor(jobs=0)


def test_line_edits_to_one_file_are_serialised(host):
    # Regression: concurrent read-modify-write of a shared file lost updates.
    data = {"resource": [{"type": "line", "name": f"l{i}", "path": "shared.conf",
                          "line": f"opt{i} = 1", "create": True} for i in range(40)]}
    first = converge(data, [host], jobs=16)
    assert first.counts() == {"changed": 40}
    lines = open(os.path.join(host.root, "shared.conf")).read().splitlines()
    assert lines == [f"opt{i} = 1" for i in range(40)]
    assert converge(data, [host], jobs=16).counts() == {"ok": 40}


def test_serialisation_edge_does_not_propagate_failure(host):
    os.mkdir(os.path.join(host.root, "f"))  # a directory where a file is expected
    report = converge({"resource": [
        {"type": "line", "name": "a", "path": "f", "line": "x"},
        {"type": "line", "name": "b", "path": "f", "line": "y"},
        {"type": "line", "name": "c", "path": "g", "line": "z", "create": True},
    ]}, [host])
    assert statuses(report) == {"line[a]": "failed", "line[b]": "failed", "line[c]": "changed"}
