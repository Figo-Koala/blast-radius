"""Generate a test-dependency index from an unmodified SQLGlot checkout.

Requires CPython 3.13 and the organizer's dependency lock. The test manifest
supplies node IDs and balancing weights. Patches and labels are never read.
"""

import argparse
import ast
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from map_flow import value_sites
from worker_trace import install
import type_trace


if source_root := os.environ.get("BLAST_PROFILE_ROOT"):
    type_trace.install(source_root, os.environ["BLAST_LOOKUP_SITES"])


class Profiler:
    def __init__(self, root, chosen):
        self.prefix = str(root) + os.sep
        self.chosen = chosen
        self.locations = {}
        self.instrumented = set()
        self.receivers = {}
        self.receiver_keys = {}
        self.types = {}
        self.ordering = {}
        self.lambdas = {}
        self.lookups = {}
        self.lines = {}
        self.reads = {}
        self.observed = set()
        self.baseline_failures = set()
        self.test = 0  # Bit zero represents collection and session-level work.
        self.monitor = sys.monitoring
        self.tool = self.monitor.COVERAGE_ID

    def path(self, filename):
        if not isinstance(filename, str):
            return None
        if filename not in self.locations:
            relative = os.path.normpath(os.path.join(self.prefix, filename))
            if relative.startswith(self.prefix):
                self.locations[filename] = relative[len(self.prefix):]
            else:
                self.locations[filename] = None
        return self.locations[filename]

    def enter(self, code, offset):
        path = self.path(code.co_filename)
        if path and path.startswith(("sqlglot/", "tests/", "sqlglotc/")):
            if code not in self.instrumented:
                self.instrumented.add(code)
                self.monitor.set_local_events(self.tool, code, self.monitor.events.LINE)
        if path and path.startswith("sqlglot/") and code.co_name == "<lambda>":
            key = f"{path}:{code.co_firstlineno}"
            self.lambdas[key] = self.lambdas.get(key, 0) | self.test
        if self.test > 1 and (
            (path == "sqlglot/dialects/dialect.py"
             and code.co_qualname in {"Dialect.parser", "Dialect.generator", "Dialect.tokenizer"})
            or (path == "sqlglot/expressions/core.py" and code.co_qualname == "Expr.__init__")
            or (path in {"sqlglot/parser.py", "sqlglot/generator.py", "sqlglot/tokens.py"}
                and code.co_name == "__init__")
        ):
            receiver = type(sys._getframe(1).f_locals["self"])
            keys = self.receiver_keys.get(receiver)
            if keys is None:
                keys = tuple(f"{cls.__module__}:{cls.__qualname__}" for cls in receiver.__mro__
                             if cls.__module__.startswith("sqlglot."))
                self.receiver_keys[receiver] = keys
            for key in keys:
                self.receivers[key] = self.receivers.get(key, 0) | self.test
            return None
        return self.monitor.DISABLE

    def line(self, code, line):
        path = self.path(code.co_filename)
        if path and path.startswith(("sqlglot/", "tests/", "sqlglotc/")):
            locations = self.lines.setdefault(path, {})
            locations[line] = locations.get(line, 0) | self.test
        return self.monitor.DISABLE

    def audit(self, event, args):
        if event not in {"open", "os.listdir", "os.scandir"}:
            return
        path = self.path(args[0])
        if not path or "__pycache__" in path or ".pytest_cache" in path:
            return
        if event == "open" and not os.path.isfile(os.path.join(self.prefix, path)):
            return
        if path.endswith(".py"):
            # Ordinary loader reads are represented by executed lines. Keep
            # direct reads, such as pathlib/inspect reading a source file.
            frame = sys._getframe(1)
            for _ in range(8):
                if frame is None:
                    break
                filename = frame.f_code.co_filename
                if filename.startswith("<frozen importlib") or filename.endswith("/rewrite.py"):
                    return
                if self.path(filename):
                    break
                frame = frame.f_back
        self.reads[path] = self.reads.get(path, 0) | self.test

    def start(self):
        type_trace.active = self
        self.monitor.use_tool_id(self.tool, "baseline dependencies")
        self.monitor.register_callback(self.tool, self.monitor.events.LINE, self.line)
        self.monitor.register_callback(self.tool, self.monitor.events.PY_START, self.enter)
        self.monitor.set_events(self.tool, self.monitor.events.PY_START)
        sys.addaudithook(self.audit)

    def pytest_collection_modifyitems(self, config, items):
        retained = [item for item in items if item.nodeid in self.chosen]
        excluded = [item for item in items if item.nodeid not in self.chosen]
        config.hook.pytest_deselected(items=excluded)
        items[:] = retained

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.test = self.chosen[item.nodeid]
        self.monitor.restart_events()
        try:
            return (yield)
        finally:
            self.test = 1
            self.monitor.restart_events()

    def pytest_runtest_logreport(self, report):
        if report.when == "call" and report.passed:
            self.observed.add(report.nodeid)
        if report.failed:
            self.baseline_failures.add(report.nodeid)

    def dump(self, destination):
        summary = {
            "lines": {path: {line: format(bits, "x") for line, bits in lines.items()}
                      for path, lines in self.lines.items()},
            "reads": {path: format(bits, "x") for path, bits in self.reads.items()},
            "types": {site: [[pair, format(bits, "x")] for pair, bits in pairs.items()]
                      for site, pairs in self.types.items()},
            "ordering": {site: [[pair, format(bits, "x")] for pair, bits in pairs.items()]
                         for site, pairs in self.ordering.items()},
            "lambdas": {site: format(bits, "x") for site, bits in self.lambdas.items()},
            "lookups": {site: [[origin, key, format(bits, "x")] for (origin, key), bits in keys.items()]
                        for site, keys in self.lookups.items()},
            "verified": sorted(self.observed - self.baseline_failures),
            "observed": sorted(self.observed | self.baseline_failures),
            "receivers": {key: format(bits, "x") for key, bits in self.receivers.items()},
            "failures": sorted(self.baseline_failures),
        }
        with gzip.open(destination, "wt", compresslevel=4) as stream:
            json.dump(summary, stream, separators=(",", ":"))


def trace(root, manifest, output, part, parts):
    tests = manifest["tests"]
    shards = [[] for _ in range(parts)]
    loads = [0] * parts
    for index, test in sorted(enumerate(tests), key=lambda pair: -pair[1]["seconds"]):
        shard = min(range(parts), key=loads.__getitem__)
        shards[shard].append((index, test))
        loads[shard] += test["seconds"]
    chosen = {test["id"]: 1 << (index + 1) for index, test in shards[part]}
    files = sorted({test["file"] for _, test in shards[part]})
    sys.path.insert(0, str(root))
    os.chdir(root)
    profiler = Profiler(root, chosen)
    worker_files = output.with_suffix(".workers")
    worker_files.mkdir()
    install(profiler, root, worker_files)
    profiler.start()
    result = pytest.main([
        "-p", "no:cacheprovider", "-p", "no:terminal", "-p", "no:warnings",
        "--assert=plain", "--continue-on-collection-errors", *files,
    ], plugins=[profiler])
    profiler.monitor.free_tool_id(profiler.tool)
    profiler.dump(output)
    print(f"Shard {part + 1}/{parts}: {len(profiler.observed)}/{len(chosen)} passed "
          f"(pytest {result}), {len(profiler.lines)} traced files", file=sys.stderr)
    if profiler.baseline_failures:
        print("Baseline failures:", sorted(profiler.baseline_failures), file=sys.stderr)


def symbol_uses(root, lines):
    symbols = {}
    for path, coverage in lines.items():
        if not path.endswith(".py"):
            continue
        tree = ast.parse((root / path).read_bytes(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                name = node.attr
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id in {"getattr", "hasattr"} and len(node.args) >= 2
                  and isinstance(node.args[1], ast.Constant)
                  and isinstance(node.args[1].value, str)):
                name = node.args[1].value
            else:
                continue
            bits = 0
            for line in range(node.lineno, node.end_lineno + 1):
                bits |= coverage.get(line, 0)
            if bits:
                symbols[name] = symbols.get(name, 0) | bits
    return symbols


def merge(parts, manifest, output, root, projected):
    names = [test["id"] for test in manifest["tests"]]
    lines, reads, receivers, types = {}, {}, {}, {}
    verified, observed = set(), set()
    ordering, lambdas, lookups = {}, {}, {}
    for part in parts:
        with gzip.open(part, "rt") as stream:
            data = json.load(stream)
        for path, entries in data["lines"].items():
            target = lines.setdefault(path, {})
            for line, bits in entries.items():
                line = int(line)
                target[line] = target.get(line, 0) | int(bits, 16)
        for path, bits in data["reads"].items():
            reads[path] = reads.get(path, 0) | int(bits, 16)
        for key, bits in data["receivers"].items():
            receivers[key] = receivers.get(key, 0) | int(bits, 16)
        for site, pairs in data["types"].items():
            target = types.setdefault(site, {})
            for pair, bits in pairs:
                key = tuple(tuple(kind) for kind in pair)
                target[key] = target.get(key, 0) | int(bits, 16)
        for site, entries in data["ordering"].items():
            target = ordering.setdefault(site, {})
            for pair, bits in entries:
                key = tuple(tuple(item) if isinstance(item, list) else item for item in pair)
                target[key] = target.get(key, 0) | int(bits, 16)
        for key, bits in data["lambdas"].items():
            lambdas[key] = lambdas.get(key, 0) | int(bits, 16)
        for site, entries in data["lookups"].items():
            target = lookups.setdefault(site, {})
            for origin, key, bits in entries:
                pair = origin, key
                target[pair] = target.get(pair, 0) | int(bits, 16)
        verified.update(data["verified"])
        observed.update(data["observed"])
    if len(verified) < len(names) * 0.9:
        raise RuntimeError("Too many test node IDs were missing or failed on the baseline")
    index = {
        "names": names,
        "verified": [name in verified for name in names],
        "observed": [name in observed for name in names],
        "receivers": {key: format(bits, "x") for key, bits in sorted(receivers.items())},
        "symbols": {name: format(bits, "x") for name, bits in sorted(symbol_uses(root, lines).items())},
        "types": {site: [[pair, format(bits, "x")] for pair, bits in sorted(pairs.items())]
                  for site, pairs in sorted(types.items())},
        "ordering": {site: [[pair, format(bits, "x")] for pair, bits in pairs.items()]
                     for site, pairs in sorted(ordering.items())},
        "lambdas": {site: format(bits, "x") for site, bits in sorted(lambdas.items())},
        "projected_maps": projected,
        "lookups": {site: [[origin, key, format(bits, "x")] for (origin, key), bits in keys.items()]
                    for site, keys in sorted(lookups.items())},
        "lines": {path: {line: format(bits, "x") for line, bits in sorted(entries.items())}
                  for path, entries in sorted(lines.items())},
        "reads": {path: format(bits, "x") for path, bits in sorted(reads.items())},
    }
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=9, mtime=0) as stream:
            stream.write(json.dumps(index, separators=(",", ":")).encode())
    print(f"Indexed {len(verified)}/{len(names)} tests, {len(lines)} source files and "
          f"{len(reads)} read paths into {output.stat().st_size:,} bytes", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--part", type=int)
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = json.loads(args.manifest.read_text())
    output = args.output.resolve()
    if args.part is not None:
        trace(root, manifest, output, args.part, args.jobs)
        return
    with tempfile.TemporaryDirectory(prefix="blast-profile-") as temp:
        parts = [Path(temp) / f"{index}.json.gz" for index in range(args.jobs)]
        commands = [
            [sys.executable, str(Path(__file__).resolve()), str(root),
             str(args.manifest.resolve()), str(part), "--jobs", str(args.jobs),
             "--part", str(index)]
            for index, part in enumerate(parts)
        ]
        projected = value_sites(root)
        print("Value-only configuration maps:", sorted(projected), file=sys.stderr)
        lookup_file = Path(temp) / "lookups.json"
        lookup_file.write_text(json.dumps(projected), encoding="utf-8")
        env = dict(os.environ, SKIP_INTEGRATION="0", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                   PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0", OPENBLAS_NUM_THREADS="1",
                   PYTHON_CPU_COUNT="2", BLAST_PROFILE_ROOT=str(root),
                   BLAST_LOOKUP_SITES=str(lookup_file))
        processes = [subprocess.Popen(command, env=env) for command in commands]
        if any(status for status in [process.wait() for process in processes]):
            raise SystemExit("Dependency profiling failed")
        worker_parts = [worker for part in parts
                        for worker in sorted(part.with_suffix(".workers").glob("*.json.gz"))]
        print(f"Merging {len(worker_parts)} multiprocessing traces", file=sys.stderr)
        merge([*parts, *worker_parts], manifest, output, root, projected)


if __name__ == "__main__":
    main()
