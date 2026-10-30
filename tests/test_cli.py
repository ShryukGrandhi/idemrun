import json
import os
import shutil

import pytest

from idemrun.cli import main

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "webfleet")


@pytest.fixture
def fleet(tmp_path):
    dst = tmp_path / "webfleet"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns("fleet"))
    return dst


def run(fleet, *args):
    return main([args[0], str(fleet / "site.toml"), "-i", str(fleet / "inventory.toml"), *args[1:]])


def test_example_end_to_end(fleet, capsys):
    assert run(fleet, "check") == 1  # nothing deployed yet: drift
    assert not (fleet / "fleet").exists()
    assert run(fleet, "apply") == 0
    conf = (fleet / "fleet/web2/etc/shop/app.conf").read_text()
    assert "host = web2" in conf and "port = 8081" in conf
    assert (fleet / "fleet/db1/var/lib/shop/schema").exists()
    assert not (fleet / "fleet/web1/var/lib").exists()
    assert os.readlink(fleet / "fleet/web1/opt/shop/current") == "releases/v1.4.2"
    assert (fleet / "fleet/web1/var/log/shop/reloads.log").read_text() == "reload web1 v1.4.2\n"
    capsys.readouterr()

    assert run(fleet, "apply", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["counts"].get("changed", 0) == 0

    # Someone hand-edits a config on one host: check finds it, apply fixes it
    (fleet / "fleet/web1/etc/shop/app.conf").write_text("port = 1\n")
    (fleet / "fleet/web1/etc/shop/legacy.ini").write_text("")
    assert run(fleet, "check", "--json") == 1
    out = json.loads(capsys.readouterr().out)
    assert sorted((r["host"], r["id"]) for r in out["resources"] if r["status"] == "would_change") == [
        ("web1", "absent[etc/shop/legacy.ini]"), ("web1", "command[reload-app]"), ("web1", "file[app-config]")]
    assert run(fleet, "apply", "--limit", "web*") == 0
    assert run(fleet, "check") == 0
    assert (fleet / "fleet/web1/var/log/shop/reloads.log").read_text().count("\n") == 2


def test_plan_and_diff(fleet, capsys):
    assert run(fleet, "plan", "-l", "db1") == 0
    out = capsys.readouterr().out
    assert "command[init-db]" in out and "command[reload-app]  <-" in out
    assert run(fleet, "check", "--diff", "-l", "web1") == 1
    assert "+port = 8080" in capsys.readouterr().out


def test_errors(fleet, tmp_path, capsys):
    assert run(fleet, "apply", "-l", "nomatch") == 3
    bad = tmp_path / "bad.toml"
    bad.write_text('[[resource]]\ntype = "command"\nname = "a"\ncmd = "true"\nrequires = "a"\n')
    assert main(["apply", str(bad), "--root", str(tmp_path / "r")]) == 3
    assert "cycle" in capsys.readouterr().err
