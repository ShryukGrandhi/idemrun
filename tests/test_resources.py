import os
import stat

import pytest

from idemrun.resources import (Absent, Command, Context, Directory, File, Line, ResourceError,
                               Symlink, parse_mode)


def mode_of(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_file_create_then_idempotent(ctx, tmp_path):
    f = File("cfg", {"path": "etc/a.conf", "content": "x=1\n", "mode": "0600"})
    (tmp_path / "etc").mkdir()
    assert f.run(ctx).changes == ["create"]
    assert (tmp_path / "etc/a.conf").read_text() == "x=1\n"
    assert mode_of(tmp_path / "etc/a.conf") == 0o600
    assert f.run(ctx).changes == []


def test_file_detects_content_and_mode_drift(ctx, tmp_path):
    p = tmp_path / "a"
    p.write_text("old\n")
    os.chmod(p, 0o644)
    f = File("a", {"path": "a", "content": "new\n", "mode": "0600"})
    assert f.inspect(ctx) == ["content", "mode 0644 -> 0600"]
    f.run(ctx)
    assert p.read_text() == "new\n" and mode_of(p) == 0o600


def test_file_mode_only_change_keeps_inode(ctx, tmp_path):
    p = tmp_path / "a"
    p.write_text("same\n")
    os.chmod(p, 0o644)
    ino = os.stat(p).st_ino
    File("a", {"path": "a", "content": "same\n", "mode": "0640"}).run(ctx)
    assert os.stat(p).st_ino == ino and mode_of(p) == 0o640


def test_file_diff(ctx, tmp_path):
    (tmp_path / "a").write_text("one\ntwo\n")
    r = File("a", {"path": "a", "content": "one\nthree\n"}).run(Context("h", str(tmp_path), check=True),
                                                                want_diff=True)
    assert "-two" in r.diff and "+three" in r.diff
    assert (tmp_path / "a").read_text() == "one\ntwo\n"  # check mode wrote nothing


def test_file_refuses_directory(ctx, tmp_path):
    (tmp_path / "a").mkdir()
    with pytest.raises(ResourceError, match="not a regular file"):
        File("a", {"path": "a", "content": ""}).inspect(ctx)


def test_path_escape_rejected(ctx):
    for bad in ("../x", "/etc/passwd", "a/../../x"):
        with pytest.raises(ResourceError, match="escapes"):
            File("x", {"path": bad, "content": ""}).inspect(ctx)


def test_unknown_and_missing_params():
    with pytest.raises(ResourceError, match="unknown parameter"):
        File("x", {"path": "a", "content": "", "colour": "red"})
    with pytest.raises(ResourceError, match="missing required parameter 'content'"):
        File("x", {"path": "a"})


def test_parse_mode():
    assert parse_mode("0644") == 0o644 and parse_mode("755") == 0o755
    for bad in ("0899", 644, "rw-r--r--"):
        with pytest.raises(ResourceError):
            parse_mode(bad)


def test_directory(ctx, tmp_path):
    d = Directory("d", {"path": "a/b/c", "mode": "0700"})
    assert d.run(ctx).changes == ["create"]
    assert mode_of(tmp_path / "a/b/c") == 0o700
    assert d.run(ctx).changes == []
    os.chmod(tmp_path / "a/b/c", 0o755)
    assert d.inspect(ctx) == ["mode 0755 -> 0700"]


def test_line_append_replace_absent(ctx, tmp_path):
    p = tmp_path / "sysctl.conf"
    p.write_text("a = 1\nnet.x = 10\nb = 2\n")
    rep = Line("x", {"path": "sysctl.conf", "line": "net.x = 99", "match": r"^net\.x"})
    assert rep.run(ctx).changes == ["replace line 2"]
    assert p.read_text() == "a = 1\nnet.x = 99\nb = 2\n"
    assert rep.run(ctx).changes == []

    app = Line("y", {"path": "sysctl.conf", "line": "c = 3"})
    assert app.run(ctx).changes == ["append"]
    assert p.read_text().endswith("b = 2\nc = 3\n")

    rm = Line("z", {"path": "sysctl.conf", "line": "a = 1", "state": "absent"})
    assert rm.run(ctx).changes == ["remove 1 line(s)"]
    assert "a = 1" not in p.read_text()
    assert rm.run(ctx).changes == []


def test_line_preserves_file_mode(ctx, tmp_path):
    p = tmp_path / "f"
    p.write_text("x\n")
    os.chmod(p, 0o600)
    Line("l", {"path": "f", "line": "y"}).run(ctx)
    assert mode_of(p) == 0o600


def test_line_missing_file(ctx, tmp_path):
    with pytest.raises(ResourceError, match="does not exist"):
        Line("l", {"path": "nope", "line": "x"}).inspect(ctx)
    Line("l", {"path": "nope", "line": "x", "create": True}).run(ctx)
    assert (tmp_path / "nope").read_text() == "x\n"
    assert Line("l", {"path": "gone", "line": "x", "state": "absent"}).inspect(ctx) == []


def test_line_validation():
    with pytest.raises(ResourceError, match="state"):
        Line("l", {"path": "f", "line": "x", "state": "maybe"})
    with pytest.raises(ResourceError, match="newline"):
        Line("l", {"path": "f", "line": "a\nb"})
    with pytest.raises(ResourceError, match="bad regex"):
        Line("l", {"path": "f", "line": "a", "match": "("})


def test_symlink(ctx, tmp_path):
    s = Symlink("cur", {"path": "current", "target": "releases/v1"})
    assert s.run(ctx).changes == ["create"]
    assert os.readlink(tmp_path / "current") == "releases/v1"
    assert s.run(ctx).changes == []
    s2 = Symlink("cur", {"path": "current", "target": "releases/v2"})
    assert s2.run(ctx).changes == ["retarget releases/v1 -> releases/v2"]
    assert os.readlink(tmp_path / "current") == "releases/v2"
    (tmp_path / "plain").write_text("")
    with pytest.raises(ResourceError, match="not a symlink"):
        Symlink("p", {"path": "plain", "target": "x"}).inspect(ctx)


def test_absent(ctx, tmp_path):
    (tmp_path / "d" / "sub").mkdir(parents=True)
    (tmp_path / "f").write_text("")
    for path in ("d", "f"):
        a = Absent(path, {"path": path})
        assert a.run(ctx).changes == ["remove"]
        assert not (tmp_path / path).exists()
        assert a.run(ctx).changes == []


def test_command_guards(ctx, tmp_path):
    c = Command("init", {"cmd": "echo hi > out", "creates": "out"})
    assert c.run(ctx).changes == ["run"]
    assert (tmp_path / "out").read_text() == "hi\n"
    assert c.run(ctx).changes == []

    u = Command("u", {"cmd": "echo x >> log", "unless": "grep -q x log"})
    u.run(ctx)
    u.run(ctx)
    assert (tmp_path / "log").read_text() == "x\n"


def test_command_env_and_failure(ctx, tmp_path):
    Command("e", {"cmd": 'echo "$IDEMRUN_HOST" > who'}).run(ctx)
    assert (tmp_path / "who").read_text() == "h1\n"
    with pytest.raises(ResourceError, match="exit 3: boom"):
        Command("f", {"cmd": "echo boom >&2; exit 3"}).run(ctx)
    with pytest.raises(ResourceError, match="timed out"):
        Command("t", {"cmd": "sleep 5", "timeout": 0.2}).run(ctx)


def test_command_check_mode_does_not_run(tmp_path):
    ctx = Context("h", str(tmp_path), check=True)
    r = Command("c", {"cmd": "touch made"}).run(ctx)
    assert r.changes == ["run"] and not (tmp_path / "made").exists()
