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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from import_trace import install as track_imports  # noqa: E402
from scalars import restore_key  # noqa: E402

from map_flow import value_sites  # noqa: E402
from worker_trace import install  # noqa: E402
import type_trace  # noqa: E402
import guard_trace  # noqa: E402


if source_root := os.environ.get("BLAST_PROFILE_ROOT"):
    type_trace.install(source_root, os.environ["BLAST_LOOKUP_SITES"])


REGISTRY_RESULTS = {"Dialect.get_or_raise", "_Dialect.get", "_Dialect.__getitem__", "_Dialect.classes"}


class Profiler:
    def __init__(self, root, chosen):
        self.prefix = str(root) + os.sep
        self.chosen = chosen
        self.guards = {}
        self.locations = {}
        self.instrumented = {}
        self.receivers = {}
        self.receiver_keys = {}
        self.types = {}
        self.values = {}
        self.import_edges = {}
        self.import_hits = {}
        self.attributes = {}
        self.attributes_eager = set()
        self.subscripts = {}
        self.parameters = {}
        self.parameter_sites = type_trace.parameter_sites
        self.attribute_owners = {}
        self.parameter_codes = {}
        self.lambdas = {}
        self.lookups = {}
        self.lines = {}
        self.reads = {}
        self.observed = set()
        self.baseline_failures = set()
        self.test = 1  # Bit zero represents collection and session-level work.
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
            identity = id(code)
            if identity not in self.instrumented:
                self.instrumented[identity] = code
                events = self.monitor.events.LINE
                if path == "sqlglot/dialects/dialect.py" and code.co_qualname in REGISTRY_RESULTS:
                    events |= self.monitor.events.PY_RETURN
                self.monitor.set_local_events(self.tool, code, events)
        if path and path.startswith("sqlglot/"):
            code_id = id(code)
            parameters = self.parameter_codes.get(code_id)
            if parameters is None:
                site = f"{path}:{code.co_firstlineno}"
                parameters = tuple((name, f"{site}:{name}")
                                   for name in self.parameter_sites.get(site, ()))
                self.parameter_codes[code_id] = parameters
            if parameters:
                locals_ = sys._getframe(1).f_locals
                for name, site in parameters:
                    value = locals_.get(name, ...)
                    kind = type_trace.singleton(value)
                    records = self.parameters.setdefault(site, {})
                    records[kind] = records.get(kind, 0) | self.test
                parameter_observed = True
            else:
                parameter_observed = False
        else:
            parameter_observed = False
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
            self.receiver(receiver)
            return None
        return None if parameter_observed else self.monitor.DISABLE

    def receiver(self, cls):
        keys = self.receiver_keys.get(cls)
        if keys is None:
            keys = tuple(f"{base.__module__}:{base.__qualname__}" for base in cls.__mro__
                         if base.__module__.startswith("sqlglot."))
            self.receiver_keys[cls] = keys
        for key in keys:
            self.receivers[key] = self.receivers.get(key, 0) | self.test

    def registry_return(self, code, _offset, value):
        values = value.values() if code.co_qualname == "_Dialect.classes" and type(value) is dict else (value,)
        for obj in values:
            cls = obj if isinstance(obj, type) else type(obj)
            if cls.__module__.startswith("sqlglot."):
                self.receiver(cls)

    def attribute(self, name, receiver, frame):
        cls = receiver if isinstance(receiver, type) else type(receiver)
        key = (id(cls), name)
        owner = self.attribute_owners.get(key)
        if owner is None:
            owner = next((base for base in cls.__mro__ if name in vars(base)), False)
            self.attribute_owners[key] = owner
        if not owner or not owner.__module__.startswith("sqlglot."):
            return
        binding = f"{owner.__module__}:{owner.__qualname__}:{name}"
        self.attributes[binding] = self.attributes.get(binding, 0) | self.test
        if self.test == 1 or isinstance(receiver, type):
            # Class-level reads may materialize registrations or derived defaults.
            # Such bindings keep the more conservative receiver/import policy.
            self.attributes_eager.add(binding)
        elif frame.f_code.co_name in {"<module>", "__init_subclass__"}:
            self.attributes_eager.add(binding)

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
        guard_trace.active = self
        if type_trace.guards_only:
            return
        type_trace.active = self
        track_imports(self)
        self.monitor.use_tool_id(self.tool, "baseline dependencies")
        self.monitor.register_callback(self.tool, self.monitor.events.LINE, self.line)
        self.monitor.register_callback(self.tool, self.monitor.events.PY_START, self.enter)
        self.monitor.register_callback(self.tool, self.monitor.events.PY_RETURN, self.registry_return)
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
            "guards": {site: [[state, format(bits, "x")] for state, bits in rows.items()]
                       for site, rows in self.guards.items()},
            "lines": {path: {line: format(bits, "x") for line, bits in lines.items()}
                      for path, lines in self.lines.items()},
            "reads": {path: format(bits, "x") for path, bits in self.reads.items()},
            "types": {site: [[pair, format(bits, "x")] for pair, bits in pairs.items()]
                      for site, pairs in self.types.items()},
            "values": {site: [[pair, format(bits, "x")] for pair, bits in pairs.items()]
                       for site, pairs in self.values.items()},
            "attributes": {field: format(bits, "x") for field, bits in self.attributes.items()},
            "attributes_eager": sorted(self.attributes_eager),
            "subscripts": {site: [format(seen, "x"), format(unsafe, "x")]
                           for site, (seen, unsafe) in self.subscripts.items()},
            "parameters": {site: {value: format(bits, "x") for value, bits in rows.items()}
                           for site, rows in self.parameters.items()},
            "import_edges": {path: sorted(targets) for path, targets in self.import_edges.items()},
            "import_hits": {path: format(bits, "x") for path, bits in self.import_hits.items()},
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
    if not type_trace.guards_only:
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


def merge_guards(parts, config):
    sites = {}
    for part in parts:
        with gzip.open(part, "rt") as stream:
            data = json.load(stream)
        for site, rows in data["guards"].items():
            target = sites.setdefault(site, {})
            for state, bits in rows:
                key = tuple(state)
                target[key] = target.get(key, 0) | int(bits, 16)
    return {
        "parser_guards": {site: [[state, format(bits, "x")] for state, bits in rows.items()]
                          for site, rows in sorted(sites.items())},
        "parser_tokens": config["tokens"],
    }


def write_index(output, index):
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=9, mtime=0) as stream:
            stream.write(json.dumps(index, separators=(",", ":")).encode())


def merge(parts, manifest, output, root, projected, shard_groups, guards):
    names = [test["id"] for test in manifest["tests"]]
    lines, reads, receivers, types = {}, {}, {}, {}
    verified, observed = set(), set()
    lambdas, lookups = {}, {}
    values, import_edges, import_hits, attributes = {}, {}, {}, {}
    attributes_eager = set()
    subscripts, parameters = {}, {}
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
        for site, entries in data["values"].items():
            target = values.setdefault(site, {})
            for pair, bits in entries:
                key = tuple(map(restore_key, pair))
                target[key] = target.get(key, 0) | int(bits, 16)
        for field, bits in data["attributes"].items():
            attributes[field] = attributes.get(field, 0) | int(bits, 16)
        attributes_eager.update(data["attributes_eager"])
        for site, (seen, unsafe) in data["subscripts"].items():
            first, second = subscripts.get(site, (0, 0))
            subscripts[site] = first | int(seen, 16), second | int(unsafe, 16)
        for site, rows in data["parameters"].items():
            target = parameters.setdefault(site, {})
            for kind, bits in rows.items():
                target[kind] = target.get(kind, 0) | int(bits, 16)
        for name, targets in data["import_edges"].items():
            import_edges.setdefault(name, set()).update(targets)
        for name, bits in data["import_hits"].items():
            import_hits[name] = import_hits.get(name, 0) | int(bits, 16)
        for key, bits in data["lambdas"].items():
            lambdas[key] = lambdas.get(key, 0) | int(bits, 16)
        for site, entries in data["lookups"].items():
            target = lookups.setdefault(site, {})
            for origin, key, bits in entries:
                pair = origin, key
                target[pair] = target.get(pair, 0) | int(bits, 16)
        verified.update(data["verified"])
        observed.update(data["observed"])
    for group in shard_groups:
        passed_this_seed = set()
        for shard in group:
            with gzip.open(shard, "rt") as stream:
                passed_this_seed.update(json.load(stream)["verified"])
        verified.intersection_update(passed_this_seed)
    if len(verified) < len(names) * 0.9:
        raise RuntimeError("Too many test node IDs were missing or failed on the baseline")
    importers = dict(import_hits)
    for pos, test in enumerate(manifest["tests"], 1):
        path = test["file"]
        importers[path] = importers.get(path, 0) | (1 << pos)
    for path, rows in lines.items():
        for bits in rows.values():
            importers[path] = importers.get(path, 0) | (bits & ~1)
    for name, bits in receivers.items():
        module, _, _ = name.partition(":")
        path = module.replace(".", "/") + ".py"
        importers[path] = importers.get(path, 0) | (bits & ~1)
    changed = True
    while changed:
        changed = False
        for importer, targets in import_edges.items():
            bits = importers.get(importer, 0)
            for path in targets:
                old = importers.get(path, 0)
                if bits & ~old:
                    importers[path] = old | bits
                    changed = True
    index = {
        "names": names,
        "verified": [name in verified for name in names],
        "observed": [name in observed for name in names],
        "receivers": {key: format(bits, "x") for key, bits in sorted(receivers.items())},
        "symbols": {name: format(bits, "x") for name, bits in sorted(symbol_uses(root, lines).items())},
        "types": {site: [[pair, format(bits, "x")] for pair, bits in sorted(pairs.items())]
                  for site, pairs in sorted(types.items())},
        "values": {site: [[pair, format(bits, "x")] for pair, bits in pairs.items()]
                   for site, pairs in sorted(values.items())},
        "importers": {path: format(bits, "x") for path, bits in sorted(importers.items()) if bits},
        "attributes": {field: format(bits, "x") for field, bits in sorted(attributes.items())},
        "attributes_eager": sorted(attributes_eager),
        "scalar_fields": sorted(type_trace.fields_with_explicit_reads(root)),
        "subscripts": {site: [format(seen, "x"), format(unsafe, "x")]
                       for site, (seen, unsafe) in sorted(subscripts.items())},
        "parameters": {site: {value: format(bits, "x") for value, bits in sorted(rows.items())}
                       for site, rows in sorted(parameters.items())},
        "lambdas": {site: format(bits, "x") for site, bits in sorted(lambdas.items())},
        "projected_maps": projected,
        "lookups": {site: [[origin, key, format(bits, "x")] for (origin, key), bits in keys.items()]
                    for site, keys in sorted(lookups.items())},
        "lines": {path: {line: format(bits, "x") for line, bits in sorted(entries.items())}
                  for path, entries in sorted(lines.items())},
        "reads": {path: format(bits, "x") for path, bits in sorted(reads.items())},
    }
    index.update(guards)
    write_index(output, index)
    print(f"Indexed {len(verified)}/{len(names)} tests, {len(lines)} source files and "
          f"{len(reads)} read paths into {output.stat().st_size:,} bytes", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--part", type=int)
    parser.add_argument("--extend-guards", type=Path,
                        help="Add focused parser observations to an existing baseline index")
    parser.add_argument("--seeds", type=int, nargs="+", default=[0, 37])
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = json.loads(args.manifest.read_text())
    output = args.output.resolve()
    if args.part is not None:
        trace(root, manifest, output, args.part, args.jobs)
        return
    with tempfile.TemporaryDirectory(prefix="blast-profile-") as temp:
        extending = args.extend_guards is not None
        projected = {} if extending else value_sites(root)
        guard_config = guard_trace.discover(root)
        lookup_file = Path(temp) / "lookups.json"
        lookup_file.write_text(json.dumps({
            "maps": projected,
            "fields": [] if extending else sorted(type_trace.fields_with_explicit_reads(root)),
            "parameters": {} if extending else type_trace.parameters_with_singleton_defaults(root),
            "guarded_equality": [] if extending else type_trace.guarded_equality_sites(root),
            "guards": guard_config, "guards_only": extending,
        }), encoding="utf-8")
        env = dict(os.environ, SKIP_INTEGRATION="0", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                   PYTHONDONTWRITEBYTECODE="1", OPENBLAS_NUM_THREADS="1",
                   PYTHON_CPU_COUNT="2", BLAST_PROFILE_ROOT=str(root),
                   BLAST_LOOKUP_SITES=str(lookup_file))
        groups = []
        for seed in dict.fromkeys(args.seeds):
            parts = [Path(temp) / f"{seed}-{index}.json.gz" for index in range(args.jobs)]
            commands = [
                [sys.executable, str(Path(__file__).resolve()), str(root),
                 str(args.manifest.resolve()), str(part), "--jobs", str(args.jobs),
                 "--part", str(index)]
                for index, part in enumerate(parts)
            ]
            print(f"Tracing baseline with hash seed {seed}", file=sys.stderr)
            child_env = dict(env, PYTHONHASHSEED=str(seed))
            processes = [subprocess.Popen(command, env=child_env) for command in commands]
            if any(status for status in [process.wait() for process in processes]):
                raise SystemExit("Dependency profiling failed")
            groups.append(parts)
        parts = [part for group in groups for part in group]
        worker_parts = [worker for part in parts
                        for worker in sorted(part.with_suffix(".workers").glob("*.json.gz"))]
        print(f"Merging {len(worker_parts)} multiprocessing traces", file=sys.stderr)
        all_parts = [*parts, *worker_parts]
        guards = merge_guards(all_parts, guard_config)
        print(f"Recorded {len(guards['parser_guards'])} parser sites", file=sys.stderr)
        if extending:
            with gzip.open(args.extend_guards, "rt") as stream:
                index = json.load(stream)
            if index["names"] != [test["id"] for test in manifest["tests"]]:
                raise RuntimeError("The extension manifest does not match the existing index")
            index.update(guards)
            write_index(output, index)
        else:
            merge(all_parts, manifest, output, root, projected, groups, guards)


if __name__ == "__main__":
    main()
