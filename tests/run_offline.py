#!/usr/bin/env python3
"""Run the reviewed ReviewPilot tests with fail-closed external-call guards.

Usage: .venv/bin/python -I -S -B tests/run_offline.py
An existing trusted PyYAML installation can instead be supplied with
--dependency-path /path/to/site-packages. No dependencies are downloaded.

Only an explicit source/test allowlist is copied; config, credentials, history,
repositories, and application entry scripts are never executed. Python audit
hooks are defense in depth for these reviewed tests, not an OS sandbox for
hostile Python or native extensions.
"""

import argparse
import ast
from collections import Counter
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import sysconfig
import tempfile
import unittest


TEST_MODULES = (
    "test_cli", "test_deep_gate", "test_freshness", "test_jev_inline",
    "test_labeler", "test_notifications", "test_parallel", "test_pr_allowlist",
)
SOURCE_FILES = ("engine.py", "labeler.py", "observer.py", "reviewpilot.py")
# This wrapper is inspected as text by a test; it is never executed.
TEXT_FIXTURES = ("observer-run.sh",)


class OfflineViolation(RuntimeError):
    """An audited operation crossed the test boundary."""


def within(path, roots):
    return any(path == root or root in path.parents for root in roots)


def install_guard(run_root, readable_roots):
    counts = Counter()
    writes = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def reject(event):
        counts[event] += 1
        # Do not include paths, command arguments, URLs, or environment values.
        raise OfflineViolation(f"offline guard rejected {event}")

    def check_path(value, event, write=False):
        if isinstance(value, int):
            return  # Existing file descriptors, including captured stdout.
        try:
            path = Path(os.fsdecode(value)).resolve()
        except (TypeError, ValueError):
            reject(event)
        roots = (run_root,) if write else readable_roots
        if not within(path, roots):
            reject(event)

    def audit(event, args):
        if (event.startswith(("socket.", "subprocess.", "ctypes.", "pty."))
                or event in {"os.system", "os.fork", "os.forkpty", "os.exec",
                             "os.posix_spawn", "os.spawn", "os.startfile",
                             "os.startfile/2", "os.kill", "os.killpg"}):
            reject(event)
        if event == "open":
            _, mode, flags = args
            write = bool(isinstance(flags, int) and flags & writes)
            write = write or bool(isinstance(mode, str) and any(c in mode for c in "wax+"))
            check_path(args[0], event, write)
        elif event in {"os.listdir", "os.scandir"}:
            check_path(args[0] if args[0] is not None else os.curdir, event)
        elif event == "os.chdir":
            check_path(args[0], event, write=True)
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.chown",
                       "os.utime", "os.truncate"}:
            check_path(args[0], event, write=True)
        elif event == "os.rename":
            check_path(args[0], event, write=True)
            check_path(args[1], event, write=True)
        elif event in {"os.symlink", "os.link"}:
            reject(event)

    sys.addaudithook(audit)
    return counts


def verify_guards(counts, run_root):
    # Synthetic audit probes invoke the same hook without opening a connection,
    # starting a child process, or touching a credential path.
    probes = (
        ("socket.__new__", (None, 2, 1, 0)),
        ("socket.connect", (None, ("offline.invalid", 443))),
        ("socket.getaddrinfo", ("offline.invalid", 443, 0, 0, 0)),
        ("subprocess.Popen", ("offline-probe", [], None, {})),
        ("os.system", (b"offline-probe",)),
        ("os.posix_spawn", ("offline-probe", [], {})),
        ("ctypes.dlopen", ("offline-probe",)),
        ("open", (str(run_root.parent / "outside-read-probe"), "r", os.O_RDONLY)),
        ("open", (str(run_root.parent / "outside-write-probe"), "w", os.O_WRONLY)),
    )
    for event, args in probes:
        try:
            sys.audit(event, *args)
        except OfflineViolation:
            continue
        raise RuntimeError(f"offline guard self-check failed: {event}")
    result = dict(counts)
    counts.clear()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependency-path", type=Path,
                        help="trusted installed site-packages containing PyYAML")
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        parser.error("launch Python with -I -S -B to skip environment paths, site startup, and bytecode writes")

    source_root = Path(__file__).resolve().parents[1]
    expected = {name + ".py" for name in TEST_MODULES}
    if {p.name for p in source_root.glob("test_*.py")} != expected:
        parser.error("test inventory changed; review it and update the explicit allowlist first")
    version_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    dependency_path = args.dependency_path or (
        Path(sys.executable).absolute().parent.parent / "lib" / version_dir / "site-packages")
    dependency_path = dependency_path.resolve()
    yaml_root = dependency_path / "yaml"
    if not (yaml_root / "__init__.py").is_file():
        parser.error("trusted PyYAML is missing; supply an already-installed --dependency-path")

    payload = {}
    for name in (*SOURCE_FILES, *sorted(expected), *TEXT_FIXTURES):
        source = source_root / name
        if source.is_symlink() or not source.is_file():
            parser.error(f"missing regular source fixture: {name}")
        data = source.read_bytes()
        if name.endswith(".py"):
            ast.parse(data, filename=name)  # Parse only; never execute original files.
        payload[name] = data

    # No inherited tokens, proxy values, CLI configuration paths, or PATH survive.
    os.environ.clear()
    with tempfile.TemporaryDirectory(prefix="reviewpilot-offline-", dir="/tmp") as directory:
        run_root = Path(directory).resolve()
        snapshot = run_root / "snapshot"
        snapshot.mkdir()
        home = run_root / "home"
        home.mkdir()
        scratch = run_root / "tmp"
        scratch.mkdir()
        for name, data in payload.items():
            (snapshot / name).write_bytes(data)
        os.environ.update({"HOME": str(home), "TMPDIR": str(scratch),
                           "TMP": str(scratch), "TEMP": str(scratch), "PATH": "",
                           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
        tempfile.tempdir = str(scratch)
        os.chdir(snapshot)
        # -S skips .pth/sitecustomize. Add only this trusted dependency directory
        # and our copied sources, never the original application directory.
        sys.path[:0] = [str(snapshot), str(dependency_path)]
        stdlib = Path(sysconfig.get_path("stdlib")).resolve()
        roots = (run_root, stdlib, yaml_root.resolve())
        counts = install_guard(run_root, roots)
        guard_probes = verify_guards(counts, run_root)

        suite = unittest.TestSuite()
        loader = unittest.TestLoader()
        for module_name in TEST_MODULES:
            module = importlib.import_module(module_name)
            suite.addTests(loader.loadTestsFromModule(module))
        result = unittest.TextTestRunner(verbosity=2).run(suite)

        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}
        unchanged = all((snapshot / name).read_bytes() == data for name, data in payload.items())
        summary = {
            "project": "ReviewPilot", "tests_run": result.testsRun,
            "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "guard_probe_events": guard_probes,
            "unexpected_blocked_events": dict(counts), "snapshot_unchanged": unchanged,
            "source_sha256": hashes,
        }
        print("REVIEWPILOT_OFFLINE_RESULT=" + json.dumps(summary, sort_keys=True))
        os.chdir(run_root)  # Keep cleanup's relative paths inside a live directory.
        return 0 if result.wasSuccessful() and not counts and unchanged else 1


if __name__ == "__main__":
    raise SystemExit(main())
