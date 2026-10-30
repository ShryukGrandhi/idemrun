import pytest

from idemrun import Host, PlanError, SpecError, build_plan, instantiate, load_inventory, load_spec, parse_spec
from idemrun.planner import critical_path
from idemrun.spec import render


def plan_for(resources, host="h"):
    spec = parse_spec({"resource": resources})
    return build_plan(host, instantiate(spec, Host(host, "/tmp/x")))


def test_render():
    assert render("a {{ x }} {{y}}", {"x": 1, "y": "b"}) == "a 1 b"
    assert render(["{{x}}"], {"x": 2}) == ["2"]
    assert render(5, {}) == 5
    with pytest.raises(SpecError, match="undefined variable 'z'"):
        render("{{ z }}", {})


def test_var_precedence(tmp_path):
    inv = tmp_path / "inv.toml"
    inv.write_text('[vars]\nport = 1\nenv = "prod"\n[hosts.a]\nroot = "ra"\n[hosts.b]\nroot = "rb"\nport = 2\n')
    hosts = load_inventory(str(inv))
    assert [h.name for h in hosts] == ["a", "b"]
    assert hosts[0].root == str(tmp_path / "ra")
    spec = parse_spec({"vars": {"port": 0, "app": "x"},
                       "resource": [{"type": "file", "path": "f", "content": "{{app}} {{env}} {{port}} {{host}}"}]})
    assert [instantiate(spec, h)[0].params["content"] for h in hosts] == ["x prod 1 a", "x prod 2 b"]


def test_host_targeting():
    spec = parse_spec({"resource": [
        {"type": "directory", "path": "all"},
        {"type": "directory", "path": "dbonly", "hosts": "db*"},
        {"type": "directory", "path": "webs", "hosts": ["web1", "web2"]},
    ]})
    names = lambda h: [r.name for r in instantiate(spec, Host(h, "/"))]
    assert names("db1") == ["all", "dbonly"]
    assert names("web2") == ["all", "webs"]


def test_spec_errors(tmp_path):
    with pytest.raises(SpecError, match="unknown or missing type"):
        parse_spec({"resource": [{"type": "package", "name": "x"}]})
    with pytest.raises(SpecError, match="needs a 'name'"):
        parse_spec({"resource": [{"type": "command", "cmd": "true"}]})
    with pytest.raises(SpecError, match="unknown top-level"):
        parse_spec({"resources": []})
    bad = tmp_path / "bad.toml"
    bad.write_text("[[resource]\n")
    with pytest.raises(SpecError):
        load_spec(str(bad))


def test_source_file(tmp_path):
    (tmp_path / "t.conf").write_text("port={{port}}\n")
    (tmp_path / "s.toml").write_text('[vars]\nport = 9\n[[resource]]\ntype = "file"\npath = "a"\nsource = "t.conf"\n')
    spec = load_spec(str(tmp_path / "s.toml"))
    assert instantiate(spec, Host("h", "/"))[0].params["content"] == "port=9\n"


def test_autorequire_and_order():
    p = plan_for([
        {"type": "line", "path": "etc/app/sysctl.conf", "line": "x", "create": True},
        {"type": "file", "path": "etc/app/app.conf", "content": ""},
        {"type": "directory", "path": "etc/app"},
        {"type": "directory", "path": "etc"},
    ])
    assert p.requires["line[etc/app/sysctl.conf]"] == {"directory[etc/app]"}
    assert p.requires["file[etc/app/app.conf]"] == {"directory[etc/app]"}
    assert p.requires["directory[etc/app]"] == {"directory[etc]"}
    assert p.order == ["directory[etc]", "directory[etc/app]", "line[etc/app/sysctl.conf]",
                       "file[etc/app/app.conf]"]


def test_requires_by_name_and_id():
    p = plan_for([
        {"type": "command", "name": "a", "cmd": "true", "requires": "b"},
        {"type": "command", "name": "b", "cmd": "true", "requires": "command[c]"},
        {"type": "command", "name": "c", "cmd": "true"},
    ])
    assert p.order == ["command[c]", "command[b]", "command[a]"]


def test_cycle_reported():
    with pytest.raises(PlanError, match=r"cycle.*command\[a\] -> command\[b\] -> command\[a\]"):
        plan_for([
            {"type": "command", "name": "a", "cmd": "true", "requires": "b"},
            {"type": "command", "name": "b", "cmd": "true", "requires": "a"},
            {"type": "command", "name": "c", "cmd": "true"},
        ])


@pytest.mark.parametrize("resources,msg", [
    ([{"type": "command", "name": "a", "cmd": "true", "requires": "nope"}], "does not match"),
    ([{"type": "file", "path": "x", "content": ""}, {"type": "symlink", "path": "x", "target": "y"}],
     "both manage path"),
    ([{"type": "command", "name": "a", "cmd": "true"}, {"type": "command", "name": "a", "cmd": "false"}],
     "duplicate"),
    ([{"type": "file", "path": "f", "content": "a\n"}, {"type": "line", "path": "f", "line": "b"}],
     "undo each other"),
    ([{"type": "command", "name": "a", "cmd": "true", "requires": "a"}], r"cycle.*command\[a\] -> command\[a\]"),
    ([{"type": "command", "name": "a", "cmd": "true", "notify": "b"}, {"type": "command", "name": "b", "cmd": "true"}],
     "not a handler"),
    ([{"type": "command", "name": "x", "cmd": "true"}, {"type": "directory", "name": "x", "path": "d"},
      {"type": "command", "name": "y", "cmd": "true", "requires": "x"}], "ambiguous"),
])
def test_plan_errors(resources, msg):
    with pytest.raises(PlanError, match=msg):
        plan_for(resources)


def test_handler_ordered_after_notifiers():
    p = plan_for([
        {"type": "command", "name": "reload", "cmd": "true", "handler": True},
        {"type": "file", "path": "a", "content": "", "notify": "reload"},
        {"type": "file", "path": "b", "content": "", "notify": "reload"},
    ])
    assert p.order[-1] == "command[reload]"
    assert p.notified_by["command[reload]"] == {"file[a]", "file[b]"}


def test_critical_path():
    p = plan_for([
        {"type": "command", "name": "a", "cmd": "true"},
        {"type": "command", "name": "b", "cmd": "true", "requires": "a"},
        {"type": "command", "name": "c", "cmd": "true"},
    ])
    cost = {"command[a]": 1.0, "command[b]": 2.0, "command[c]": 2.5}
    assert critical_path(p, cost) == 3.0
