"""Run untrusted Python inside the macOS sandbox (Seatbelt), with no network and no access to
the user's files. On any other platform it refuses: running unsandboxed is never the fallback."""

import os
import resource
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

MAX_OUTPUT = 10_000  # characters of stdout and stderr returned

# Seatbelt applies the last rule that matches. Everything the interpreter needs to start is
# allowed by default; then the dangerous things are taken away.
PROFILE = """(version 1)
(allow default)
(deny network*)
(deny process-fork)
(deny file-write*)
(allow file-write* (subpath "{box}") (literal "/dev/null"))
(deny file-read-data (subpath "/Users") (subpath "/Volumes"))
(allow file-read-data (subpath "{python}"))
"""


class SandboxUnavailable(Exception):
    pass


@dataclass
class Run:
    stdout: str
    stderr: str
    exit_code: int | None  # None when the run was killed for exceeding its time limit


def _limits(seconds: int):
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (seconds + 1, seconds + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1_000_000, 1_000_000))  # 1 MB per file written
        # ponytail: no memory cap. macOS does not enforce RLIMIT_AS; a container or VM is the
        # way to bound memory, and the right upgrade if this ever runs code from strangers.

    return apply


def run_python(code: str, seconds: int = 5) -> Run:
    sandbox = shutil.which("sandbox-exec")
    if sys.platform != "darwin" or not sandbox:
        raise SandboxUnavailable("code execution needs the macOS sandbox, which is not available here")
    with tempfile.TemporaryDirectory() as folder:
        box = os.path.realpath(folder)
        profile = PROFILE.format(box=box, python=os.path.realpath(sys.base_prefix))
        # The base interpreter, not the virtualenv's: the script gets the standard library only,
        # and the sandbox does not have to open the project folder to read the venv's config.
        command = [sandbox, "-p", profile, os.path.realpath(sys.executable), "-I", "-c", code]
        try:
            done = subprocess.run(
                command,
                cwd=box,
                env={"PATH": "/usr/bin:/bin", "HOME": box, "TMPDIR": box},  # nothing inherited
                capture_output=True,
                text=True,
                timeout=seconds,
                preexec_fn=_limits(seconds),
            )
        except subprocess.TimeoutExpired as late:
            text = lambda raw: (raw.decode(errors="replace") if isinstance(raw, bytes) else raw or "")[:MAX_OUTPUT]
            return Run(text(late.stdout), text(late.stderr), None)
        return Run(done.stdout[:MAX_OUTPUT], done.stderr[:MAX_OUTPUT], done.returncode)
