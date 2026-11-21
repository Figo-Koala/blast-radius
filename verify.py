"""Run patched candidates in disposable worktrees and report proven outcomes."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time


@dataclass
class Probe:
    files: tuple[str, ...]
    tests: tuple[str, ...]
    limit: float
    priority: float


def run_probe(root, changes, probe, deadline):
    if deadline - time.monotonic() < 0.8:
        return set(), set(), set(), set()
    with tempfile.TemporaryDirectory(prefix="blast-") as temp:
        area = Path(temp)
        working = area / "src"
        shutil.copytree(root, working, ignore=shutil.ignore_patterns(".git", ".pytest_cache"))
        for path, content in changes.items():
            target = working / path
            if content is None:
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            if path.endswith(".py"):
                for cached in (target.parent / "__pycache__").glob(target.stem + ".*.pyc"):
                    cached.unlink()
        plan = area / "plan.json"
        report = area / "report.jsonl"
        plan.write_text(json.dumps([probe.files, probe.tests]), encoding="utf-8")
        env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", SKIP_INTEGRATION="0",
                   PYTHONDONTWRITEBYTECODE="1", OPENBLAS_NUM_THREADS="1",
                   OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", PYTHON_CPU_COUNT="2")
        with subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), str(plan), str(report)],
            cwd=working, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as process:
            timeout = min(probe.limit, max(0.1, deadline - time.monotonic()))
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        outcomes = {"pass": set(), "fail": set(), "file": set(), "collected": set()}
        if report.is_file():
            for line in report.read_text(encoding="utf-8").splitlines():
                try:
                    outcome, name = json.loads(line)
                    outcomes[outcome].add(name)
                except (ValueError, KeyError):
                    # A timed-out child may have been killed during the final write.
                    continue
        collected = outcomes["collected"]
        if "*" in collected:
            collected = {"*"}
        return outcomes["pass"], outcomes["fail"], outcomes["file"], collected


def child(plan, report):
    import pytest

    files, names = json.loads(plan.read_text(encoding="utf-8"))
    cwd = Path.cwd()
    sys.path.insert(0, str(cwd))
    wanted = set(names)

    with report.open("w", buffering=1, encoding="utf-8") as log:
        def emit(kind, name):
            log.write(json.dumps([kind, name], separators=(",", ":")) + "\n")

        requested_files = set(files)

        class Results:
            def __init__(self):
                self.passed = set()
                self.failed = set()
                self.skipped = set()

            def pytest_collectreport(self, report):
                path = report.nodeid.split("::", 1)[0]
                if report.failed:
                    emit("file", path)
                elif report.nodeid == path and path in requested_files:
                    emit("collected", path)

            def pytest_collection_modifyitems(self, config, items):
                retained = [item for item in items if item.nodeid in wanted]
                excluded = [item for item in items if item.nodeid not in wanted]
                config.hook.pytest_deselected(items=excluded)
                items[:] = retained

            def pytest_collection_finish(self, session):
                emit("collected", "*")

            def pytest_runtest_logreport(self, report):
                name = report.nodeid
                if report.failed and name not in self.failed:
                    self.failed.add(name)
                    emit("fail", name)
                elif report.skipped:
                    self.skipped.add(name)
                elif report.when == "call" and report.passed:
                    self.passed.add(name)
                elif (report.when == "teardown" and name in self.passed
                      and name not in self.failed and name not in self.skipped):
                    emit("pass", name)

        present = []
        for path in files:
            if (cwd / path).is_file():
                present.append(path)
            else:
                emit("file", path)
        if present:
            pytest.main([
                "-p", "no:cacheprovider", "-p", "no:terminal", "-p", "no:warnings",
                "--assert=plain", "--continue-on-collection-errors", *present,
            ], plugins=[Results()])


if __name__ == "__main__":
    child(Path(sys.argv[1]), Path(sys.argv[2]))
