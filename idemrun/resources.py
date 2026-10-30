"""Resource types.

Every resource describes a piece of desired state on a host (a file with some
content, a directory, a line inside a config file, ...). A resource knows how to
`inspect` the current state and return the list of changes needed to reach the
desired state, and how to `apply` those changes. An empty change list means the
resource is already converged, which is what makes repeated runs idempotent.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field

REQUIRED = object()


class ResourceError(Exception):
    """Raised when a resource cannot be inspected or applied."""


@dataclass
class Context:
    """Per-host execution context. All resource paths are relative to `root`."""

    host: str
    root: str
    check: bool = False

    def resolve(self, rel: str) -> str:
        norm = os.path.normpath(rel)
        if os.path.isabs(norm) or norm == ".." or norm.startswith(".." + os.sep):
            raise ResourceError(f"path escapes host root: {rel!r}")
        return os.path.join(self.root, norm)


@dataclass
class Result:
    changes: list[str] = field(default_factory=list)
    diff: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def parse_mode(value: str | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"0?[0-7]{3,4}", value):
        raise ResourceError(f"mode must be an octal string like '0644', got {value!r}")
    return int(value, 8)


def atomic_write(path: str, data: str, mode: int | None) -> None:
    """Write via a temp file in the same directory + rename, so readers never
    see a half-written file."""
    directory = os.path.dirname(path) or "."
    if mode is None and os.path.exists(path):
        mode = stat.S_IMODE(os.stat(path).st_mode)
    fd, tmp = tempfile.mkstemp(prefix=".idemrun-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(data)
        os.chmod(tmp, mode if mode is not None else 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mode_change(path: str, want: int | None) -> list[str]:
    if want is None:
        return []
    have = stat.S_IMODE(os.stat(path).st_mode)
    return [] if have == want else [f"mode {have:04o} -> {want:04o}"]


class Resource:
    kind = ""
    schema: dict[str, object] = {}

    def __init__(self, name: str, params: dict, *, requires=(), notify=(),
                 handler: bool = False, index: int = 0):
        unknown = set(params) - set(self.schema)
        if unknown:
            raise ResourceError(f"{self.kind}[{name}]: unknown parameter(s) {sorted(unknown)}")
        self.params = {}
        for key, default in self.schema.items():
            if key in params:
                self.params[key] = params[key]
            elif default is REQUIRED:
                raise ResourceError(f"{self.kind}[{name}]: missing required parameter {key!r}")
            else:
                self.params[key] = default
        self.name = name
        self.requires = list(requires)
        self.notify = list(notify)
        self.handler = handler
        self.index = index
        self.validate()

    @property
    def id(self) -> str:
        return f"{self.kind}[{self.name}]"

    def validate(self) -> None:
        pass

    def paths(self) -> list[str]:
        """Paths (relative to the host root) this resource manages exclusively."""
        return []

    def inspect(self, ctx: Context) -> list[str]:
        raise NotImplementedError

    def apply(self, ctx: Context, changes: list[str]) -> None:
        raise NotImplementedError

    def diff(self, ctx: Context) -> str:
        return ""

    def run(self, ctx: Context, want_diff: bool = False) -> Result:
        changes = self.inspect(ctx)
        diff = self.diff(ctx) if (changes and want_diff) else ""
        if changes and not ctx.check:
            self.apply(ctx, changes)
        return Result(changes, diff)

    def __repr__(self) -> str:
        return f"<{self.id}>"


class File(Resource):
    kind = "file"
    schema = {"path": REQUIRED, "content": REQUIRED, "mode": None}

    def validate(self):
        self.mode = parse_mode(self.params["mode"])

    def paths(self):
        return [os.path.normpath(self.params["path"])]

    def inspect(self, ctx):
        path = ctx.resolve(self.params["path"])
        if not os.path.lexists(path):
            return ["create"]
        if os.path.islink(path) or not os.path.isfile(path):
            raise ResourceError(f"{self.params['path']} exists and is not a regular file")
        changes = []
        if _sha(_read_text(path)) != _sha(self.params["content"]):
            changes.append("content")
        return changes + _mode_change(path, self.mode)

    def diff(self, ctx):
        path = ctx.resolve(self.params["path"])
        old = _read_text(path) if os.path.isfile(path) else ""
        return "".join(difflib.unified_diff(
            old.splitlines(keepends=True), self.params["content"].splitlines(keepends=True),
            fromfile=f"a/{self.params['path']}", tofile=f"b/{self.params['path']}"))

    def apply(self, ctx, changes):
        path = ctx.resolve(self.params["path"])
        if changes == [c for c in changes if c.startswith("mode")]:
            os.chmod(path, self.mode)
        else:
            atomic_write(path, self.params["content"], self.mode)


class Directory(Resource):
    kind = "directory"
    schema = {"path": REQUIRED, "mode": None}

    def validate(self):
        self.mode = parse_mode(self.params["mode"])

    def paths(self):
        return [os.path.normpath(self.params["path"])]

    def inspect(self, ctx):
        path = ctx.resolve(self.params["path"])
        if not os.path.lexists(path):
            return ["create"]
        if os.path.islink(path) or not os.path.isdir(path):
            raise ResourceError(f"{self.params['path']} exists and is not a directory")
        return _mode_change(path, self.mode)

    def apply(self, ctx, changes):
        path = ctx.resolve(self.params["path"])
        os.makedirs(path, exist_ok=True)
        if self.mode is not None:
            os.chmod(path, self.mode)


class Line(Resource):
    """Ensure a single line is present in (or absent from) a text file.

    With `match`, the last line matching the regex is replaced instead of
    appending, which is how you edit a `key = value` setting in place.
    """

    kind = "line"
    schema = {"path": REQUIRED, "line": REQUIRED, "match": None,
              "state": "present", "create": False}

    def validate(self):
        if self.params["state"] not in ("present", "absent"):
            raise ResourceError(f"{self.id}: state must be 'present' or 'absent'")
        if "\n" in self.params["line"]:
            raise ResourceError(f"{self.id}: line must not contain a newline")
        try:
            self.regex = re.compile(self.params["match"]) if self.params["match"] else None
        except re.error as exc:
            raise ResourceError(f"{self.id}: bad regex: {exc}") from exc

    def _new_lines(self, lines: list[str]) -> tuple[list[str], list[str]]:
        line = self.params["line"]
        if self.params["state"] == "absent":
            kept = [l for l in lines if l != line and not (self.regex and self.regex.search(l))]
            removed = len(lines) - len(kept)
            return kept, ([f"remove {removed} line(s)"] if removed else [])
        if line in lines:
            return lines, []
        if self.regex:
            hits = [i for i, l in enumerate(lines) if self.regex.search(l)]
            if hits:
                out = list(lines)
                out[hits[-1]] = line
                return out, [f"replace line {hits[-1] + 1}"]
        return lines + [line], ["append"]

    def _load(self, ctx):
        path = ctx.resolve(self.params["path"])
        if not os.path.exists(path):
            if self.params["state"] == "absent":
                return path, None
            if not self.params["create"]:
                raise ResourceError(f"{self.params['path']} does not exist (set create = true)")
            return path, None
        return path, _read_text(path).splitlines()

    def inspect(self, ctx):
        path, lines = self._load(ctx)
        if lines is None:
            return [] if self.params["state"] == "absent" else ["create file", "append"]
        return self._new_lines(lines)[1]

    def apply(self, ctx, changes):
        path, lines = self._load(ctx)
        new, _ = self._new_lines(lines or [])
        atomic_write(path, "".join(l + "\n" for l in new), None)


class Symlink(Resource):
    kind = "symlink"
    schema = {"path": REQUIRED, "target": REQUIRED}

    def paths(self):
        return [os.path.normpath(self.params["path"])]

    def inspect(self, ctx):
        path = ctx.resolve(self.params["path"])
        if os.path.islink(path):
            have = os.readlink(path)
            return [] if have == self.params["target"] else [f"retarget {have} -> {self.params['target']}"]
        if os.path.lexists(path):
            raise ResourceError(f"{self.params['path']} exists and is not a symlink")
        return ["create"]

    def apply(self, ctx, changes):
        path = ctx.resolve(self.params["path"])
        tmp = f"{path}.idemrun-tmp"
        if os.path.lexists(tmp):
            os.unlink(tmp)
        os.symlink(self.params["target"], tmp)
        os.replace(tmp, path)


class Absent(Resource):
    kind = "absent"
    schema = {"path": REQUIRED}

    def paths(self):
        return [os.path.normpath(self.params["path"])]

    def inspect(self, ctx):
        return ["remove"] if os.path.lexists(ctx.resolve(self.params["path"])) else []

    def apply(self, ctx, changes):
        path = ctx.resolve(self.params["path"])
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)


class Command(Resource):
    """Run a shell command in the host root.

    Guards keep it idempotent: `creates` skips the command if that path exists,
    `unless` skips it if the given check command exits 0. A command with no
    guard runs every time (useful as a handler, e.g. a service reload).
    """

    kind = "command"
    schema = {"cmd": REQUIRED, "creates": None, "unless": None, "timeout": 60}

    def _env(self, ctx):
        return {**os.environ, "IDEMRUN_HOST": ctx.host, "IDEMRUN_ROOT": ctx.root}

    def inspect(self, ctx):
        if self.params["creates"] and os.path.lexists(ctx.resolve(self.params["creates"])):
            return []
        if self.params["unless"]:
            probe = subprocess.run(self.params["unless"], shell=True, cwd=ctx.root,
                                   env=self._env(ctx), capture_output=True,
                                   timeout=self.params["timeout"])
            if probe.returncode == 0:
                return []
        return ["run"]

    def apply(self, ctx, changes):
        try:
            proc = subprocess.run(self.params["cmd"], shell=True, cwd=ctx.root,
                                  env=self._env(ctx), capture_output=True, text=True,
                                  timeout=self.params["timeout"])
        except subprocess.TimeoutExpired as exc:
            raise ResourceError(f"timed out after {exc.timeout}s") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
            raise ResourceError(f"exit {proc.returncode}: {' | '.join(tail)}")


TYPES: dict[str, type[Resource]] = {
    cls.kind: cls for cls in (File, Directory, Line, Symlink, Absent, Command)
}
