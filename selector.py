"""Select tests by baseline dependencies, then verify affordable candidates."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import lru_cache
import gzip
import json
from pathlib import Path
import sys
from subprocess import SubprocessError
import time

from impact import analyze
from patches import PatchError, apply_file, read_patch
from schedule import schedule, fill_headroom
from scalars import OPERATIONS, decode
from verify import Probe, run_probe


HERE = Path(__file__).resolve().parent
COLLECTION = 1
DOC_SUFFIXES = {".md", ".rst"}
PACKAGE_PREFIXES = ("sqlglot/", "sqlglotc/", "tests/")


def combine(values):
    mask = 0
    for value in values:
        mask |= value
    return mask


def positions(mask):
    while mask:
        lowest = mask & -mask
        yield lowest.bit_length() - 1
        mask ^= lowest


def identity_matches_equality(left, right, equality):
    left_type, left_protocol = left
    right_type, right_protocol = right
    safe = {"object", "builtin", "enum-scalar"}
    if equality:
        safe.add("type-guarded-eq")
    if left_type == "builtins.NoneType":
        return right_protocol in safe and right_type != "builtins.memoryview"
    if right_type == "builtins.NoneType":
        return left_protocol in safe and left_type != "builtins.memoryview"
    if left_protocol == right_protocol == "object":
        return True
    return left_type == right_type and left_protocol == right_protocol == "enum-scalar"


class Catalog:
    def __init__(self, manifest, index):
        tests = manifest["tests"]
        self.all = (1 << len(tests)) - 1
        self.ids = [test["id"] for test in tests]
        self.numbers = [test["number"] for test in tests]
        self.weights = [round(1000 * test["seconds"]) for test in tests]
        self.by_name = {name: index for index, name in enumerate(self.ids)}
        self.files = {}
        for index_, test in enumerate(tests):
            path = test["file"]
            self.files[path] = self.files.get(path, 0) | (1 << index_)
        prior = {name: position for position, name in enumerate(index["names"])}
        ordering = [prior.get(name) for name in self.ids]
        self.observed = index.get("observed", index["verified"])
        self.unobserved = combine(
            1 << pos for pos, base in enumerate(ordering)
            if base is None or not self.observed[base]
        )
        self.verified = combine(
            1 << pos for pos, base in enumerate(ordering)
            if base is not None and index["verified"][base]
        )
        self.unreliable_baseline = combine(
            1 << (base + 1) for base in ordering if base is not None and not index["verified"][base]
        )
        if ordering == list(range(len(tests))):
            def translate(mask):
                return mask >> 1
        else:
            reverse = {base + 1: pos for pos, base in enumerate(ordering) if base is not None}

            @lru_cache(maxsize=32768)
            def translate(mask):
                return combine(1 << reverse[index] for index in positions(mask) if index in reverse)

        self.translate = translate
        self.lines = {path: {int(line): int(bits, 16) for line, bits in entries.items()}
                      for path, entries in index["lines"].items()}
        self.file_coverage = {path: combine(entries.values()) for path, entries in self.lines.items()}
        self.reads = {path: int(bits, 16) for path, bits in index["reads"].items()}
        self.symbols = {name: int(bits, 16) for name, bits in index.get("symbols", {}).items()}
        self.receivers = {name: int(bits, 16) for name, bits in index.get("receivers", {}).items()}
        self.lambdas = {site: int(bits, 16) for site, bits in index.get("lambdas", {}).items()}
        self.parser_tokens = set(index.get("parser_tokens", []))
        self.parser_guards = {site: [(tuple(state), int(bits, 16)) for state, bits in rows]
                              for site, rows in index.get("parser_guards", {}).items()}
        self.ordering = index.get("ordering", {})
        self.values = index.get("values", {})
        self.subscripts = {site: (int(seen, 16), int(seen, 16) & ~int(unsafe, 16))
                           for site, (seen, unsafe) in index.get("subscripts", {}).items()}
        self.parameters = {site: {kind: int(bits, 16) for kind, bits in rows.items()}
                           for site, rows in index.get("parameters", {}).items()}
        self.attributes = {field: int(bits, 16) for field, bits in index.get("attributes", {}).items()}
        self.attributes_eager = set(index.get("attributes_eager", []))
        self.scalar_fields = set(index.get("scalar_fields", []))
        self.importers = {path: int(bits, 16) for path, bits in index.get("importers", {}).items()}
        self.projected_maps = index.get("projected_maps", {})
        self.lookups = {site: {(origin, key): int(bits, 16) for origin, key, bits in entries}
                        for site, entries in index.get("lookups", {}).items()}
        self.identity_safe = {}
        for site, pairs in index.get("types", {}).items():
            seen, unsafe_eq, unsafe_ne = 0, 0, 0
            for (left, right), raw in pairs:
                bits = int(raw, 16)
                seen |= bits
                if not identity_matches_equality(left, right, equality=True):
                    unsafe_eq |= bits
                if not identity_matches_equality(left, right, equality=False):
                    unsafe_ne |= bits
            self.identity_safe[site] = (seen, {"Eq": seen & ~unsafe_eq, "NotEq": seen & ~unsafe_ne})

    def parser_equivalence(self, location, kind, detail):
        rows = self.parser_guards.get(location, ())
        if kind == "entry":
            tokens, forced = detail
            if not set(tokens).issubset(self.parser_tokens):
                return 0, 0

            def unchanged(state):
                token, flag, skip = state
                return flag is forced or (isinstance(flag, bool) and skip is False
                                          and token is not None and token not in tokens)
        elif kind == "token":
            before, after = detail
            if before not in self.parser_tokens or after not in self.parser_tokens:
                return 0, 0

            def unchanged(state):
                token, actual = state
                return actual == before and token is not None and token not in (before, after)
        elif kind == "short":
            if detail not in self.parser_tokens:
                return 0, 0

            def unchanged(state):
                token, left = state
                return left is False and token is not None and token != detail
        else:
            def unchanged(state):
                return state == (True,)

        seen = failed = 0
        for state, bits in rows:
            seen |= bits
            if not unchanged(state):
                failed |= bits
        return seen, seen & ~failed

    def comparison(self, site, before, after, bounds, unchanged):
        if before == "identity":
            seen, equivalent = self.identity_safe.get(site, (0, {}))
            return seen, equivalent.get(after, 0)
        if before == "index":
            return self.subscripts.get(site, (0, 0))
        first, second = OPERATIONS[before], OPERATIONS[after]
        seen, unsafe = 0, 0
        if site in self.values:
            for pair, raw in self.values[site]:
                bits = int(raw, 16)
                seen |= bits
                if any(value is None for value in pair):
                    unsafe |= bits
                    continue
                previous = tuple(map(decode, pair))
                current = tuple(old if bound is None else decode(bound)
                                for bound, old in zip(bounds, previous))
                try:
                    if first(*previous) != second(*current):
                        unsafe |= bits
                except TypeError:
                    unsafe |= bits
            return seen, seen & ~unsafe
        if unchanged:
            for (left, right), raw in self.ordering.get(site, ()):
                bits = int(raw, 16)
                seen |= bits
                if (left is None or right is None or type(left) is not type(right)
                        or first(left, right) != second(left, right)):
                    unsafe |= bits
        return seen, seen & ~unsafe

    def value_reads(self, name, origin, keys, all_values):
        sites = self.projected_maps.get(name)
        if not sites:
            return None
        hits = 0
        for site in sites:
            path, line, _ = site.rsplit(":", 2)
            reads = self.lookups.get(site, {})
            seen = combine(reads.values())
            hits |= self.lines.get(path, {}).get(int(line), 0) & ~seen
            hits |= combine(bits for (source, key), bits in reads.items()
                            if source in {None, origin} and (all_values or key is None or key in keys))
        return hits

    def readers(self, path, listing=False):
        found = self.reads.get(path, 0)
        if found:
            return found
        if not listing:
            return 0
        parent = Path(path).parent
        if str(parent) != ".":
            return self.reads.get(parent.as_posix(), 0)
        return 0

    def imports(self, path):
        return self.files.get(path, 0)


@dataclass
class Selection:
    mask: int = 0
    reserve: int = 0
    guard_exclusions: int = 0
    edits: dict = field(default_factory=dict)
    collection: bool = False
    probe: bool = True
    risky: bool = False
    fixture_only: bool = True


class Selector:
    def __init__(self, root, catalog):
        self.root = root
        self.catalog = catalog
        self.read = lru_cache(maxsize=256)(self.read)

    def read(self, path):
        return (self.root / path).read_bytes()

    def python(self, path, original, modified, selection, single_source=True):
        index = self.catalog
        direct = index.readers(path)
        if original == modified:
            return 0
        try:
            impact = analyze(original, modified, path)
        except SyntaxError:
            selection.collection = selection.risky = True
            try:
                compile(modified, path, "exec")
            except SyntaxError:
                # This file cannot execute. Preserve known module consumers;
                # collection also finds files that now fail to import.
                consumers = index.importers.get(path, 0)
                return (index.translate(consumers | direct) | index.unobserved) if consumers else index.all
            return index.all
        except (ValueError, RecursionError):
            return index.all
        coverage = index.lines.get(path, {})
        hits = combine(coverage.get(line, 0) for line in impact.lines)
        # A second executable edit could change a match implementation or
        # feed different state into a guard. Use ordinary coverage in that case.
        use_parser_proofs = (
            single_source and len(impact.parser_guards) + len(impact.parameter_assignments) == 1
            and not (impact.lines or impact.symbols or impact.collection or impact.broad
                     or impact.comparisons or impact.lambdas or impact.dictionary_edits
                     or impact.attribute_edits)
        )
        for kind, location, detail, lines in impact.parser_guards:
            covered = combine(coverage.get(line, 0) for line in lines)
            seen, equivalent = (index.parser_equivalence(location, kind, detail)
                                if use_parser_proofs else (0, 0))
            possible = covered | seen
            selection.guard_exclusions |= index.translate(possible & equivalent)
            hits |= possible & ~equivalent
        for name, lines, owner in impact.attribute_edits:
            binding = f"{path[:-3].replace('/', '.')}:{owner}:{name}"
            known = index.attributes.get(binding, 0)
            if known and name in index.scalar_fields and binding not in index.attributes_eager:
                hits |= known
            else:
                impact.symbols.add(name)
                impact.owners.add(owner)
                hits |= combine(coverage.get(line, 0) for line in lines)
        for entry, name, value, fallback in impact.parameter_assignments:
            records = index.parameters.get(f"{path}:{entry}:{name}", {})
            seen = combine(records.values())
            different = combine(bits for kind, bits in records.items() if kind != value)
            covered = combine(coverage.get(line, 0) for line in fallback)
            candidates = different | (covered & ~seen)
            tokens = impact.parser_entries.get((entry, name))
            if tokens and use_parser_proofs:
                observed, equivalent = index.parser_equivalence(f"{path}:{entry}:entry", "entry",
                                                                 (tokens, value == "true"))
                possible = candidates | observed
                selection.guard_exclusions |= index.translate(possible & equivalent)
                candidates = possible & ~equivalent
            hits |= candidates
        for name, keys, all_values, lines, owner in impact.dictionary_edits:
            readers = index.value_reads(name, f"{path}:{lines[0]}", keys, all_values)
            if readers is None:
                impact.symbols.add(name)
                impact.owners.add(owner)
                hits |= combine(coverage.get(line, 0) for line in lines)
            else:
                hits |= readers
        for line in impact.lambdas:
            hits |= index.lambdas.get(f"{path}:{line}", 0)
        for site, lines, before, after, bounds, unchanged in impact.comparisons:
            seen, equivalent = index.comparison(site, before, after, bounds, unchanged)
            covered = combine(coverage.get(line, 0) for line in lines)
            hits |= (covered | seen) & ~equivalent
        if impact.symbols:
            selection.reserve |= index.translate(index.importers.get(path, 0))
            symbols = combine(index.symbols.get(name, 0) for name in impact.symbols)
            owned = combine(
                index.receivers.get(f"{path[:-3].replace('/', '.')}:{owner.split('.')[0]}", 0)
                for owner in impact.owners if owner
            )
            if owned and None not in impact.owners:
                # Covered receiver classes include subclasses and dialects used
                # indirectly through parse, tokenize and generate.
                owner_names = combine(index.symbols.get(owner.split(".")[0], 0)
                                      for owner in impact.owners if owner)
                if symbols & COLLECTION or impact.collection:
                    symbols |= owned
                hits = (symbols & (owned | owner_names)) | (hits & ~COLLECTION)
            else:
                hits |= symbols | index.file_coverage.get(path, 0)

        if impact.collection or impact.broad or (hits & COLLECTION and not impact.symbols):
            # Import-time execution may leave cached globals or prevent collection.
            selection.collection = selection.risky = True
            consumers = index.importers.get(path, 0)
            imported_tests = index.translate(consumers)
            selection.reserve |= imported_tests
            # A test with an incomplete baseline run can have no executable
            # line hits even though it consumes the changed module.
            hits |= consumers & index.unreliable_baseline
            if (hits & COLLECTION and not impact.symbols) or (
                    impact.collection and index.translate(hits).bit_count() * 2 >= len(index.ids)):
                hits |= consumers
            if impact.broad:
                if not (impact.import_change and not impact.other_initializers and consumers):
                    return index.all
                hits |= consumers
            elif not (hits >> 1):
                if not consumers:
                    return index.all
                hits |= consumers
        if not (impact.lines or impact.symbols or impact.collection or impact.comparisons
                or impact.lambdas or impact.dictionary_edits or impact.attribute_edits
                or impact.parameter_assignments or impact.parser_guards):
            # The compiled syntax is unchanged. Tests that read the actual
            # source still depend on its contents.
            return index.translate(direct)

        selection.fixture_only = False
        return (index.translate(hits | direct) | index.imports(path) | index.unobserved)

    def select(self, patch):
        index = self.catalog
        result = Selection()
        try:
            files = read_patch(patch)
            single_source = sum((file.new_path or file.old_path).endswith(".py") for file in files) == 1
            for file in files:
                old, new = file.old_path, file.new_path
                path = new or old
                if file.binary:
                    readers = index.readers(path, listing=old != new)
                    result.probe = False
                    result.mask |= (index.all if readers & COLLECTION
                                    else index.translate(readers) or index.all)
                    continue
                if file.mode_changed:
                    result.probe = False
                    result.mask |= index.all
                    continue
                original = self.read(old) if old else b""
                modified = apply_file(original, file)
                if old and old != new and not file.copied:
                    result.edits[old] = None
                    result.mask |= index.imports(old) | index.translate(index.readers(old, listing=True))
                if new:
                    result.edits[new] = modified
                if path.endswith(".py") and old != new:
                    result.collection = result.risky = True
                    result.fixture_only = False
                    result.mask |= index.all
                elif path.endswith(".py"):
                    result.mask |= self.python(path, original, modified, result, single_source)
                else:
                    readers = index.readers(path, listing=old != new)
                    result.mask |= index.translate(readers)
                    if readers & COLLECTION:
                        result.collection = result.risky = True
                        result.mask |= index.all
                    elif not readers and (path.startswith(PACKAGE_PREFIXES)
                                          or Path(path).suffix not in DOC_SUFFIXES):
                        result.probe = False
                        result.mask |= index.all
            if result.collection:
                result.fixture_only = False
            result.reserve &= ~result.mask
            return result
        except (OSError, PatchError, UnicodeError, ValueError, RecursionError):
            return Selection(mask=index.all, probe=False)

    @lru_cache(maxsize=128)
    def heavy_imports(self, path):
        # Fresh pytest processes pay the startup cost for these native libraries,
        # even when an individual test has a very short recorded runtime.
        try:
            source = self.read(path)
        except OSError:
            return True
        return any(part in source for part in (b"import duckdb", b"import pandas", b"from pandas ",
                                               b"from pandas.", b"import numpy"))

    def plan(self, selection, mode="adaptive"):
        index = self.catalog
        if not selection.probe or selection.fixture_only:
            return None
        # Checking imports can only add tests from files that are not already
        # fully selected. Avoid paying to collect files that cannot add anything.
        collection_files = tuple(sorted(path for path, tests in index.files.items()
                                        if tests & ~selection.mask)) if selection.collection else ()
        if mode == "collection":
            if not collection_files:
                return None
            return Probe(collection_files, (), 3.1, 10_000)
        chosen = list(positions(selection.mask))
        eligible = [pos for pos in chosen if index.verified & (1 << pos)]
        weight = index.weights
        cost, _ = schedule(chosen, weight)
        by_file = {}
        for pos in eligible:
            by_file.setdefault(index.ids[pos].partition("::")[0], []).append(pos)
        files = collection_files
        benefit = 0
        tests = ()
        duration = 0
        groups = []
        if len(eligible) <= 120 and sum(weight[pos] for pos in eligible) <= 2_000:
            groups.append(eligible)
        if len(chosen) <= 800 and cost >= 4_000:
            # Removing a long job matters only when it changes the worker schedule.
            # Consider a few such jobs and their file groups.
            for pos in sorted(eligible, key=weight.__getitem__, reverse=True)[:4]:
                if 300 <= weight[pos] <= 1_900:
                    groups.append([pos])
                    groups.append(by_file[index.ids[pos].partition("::")[0]])
        for group in dict.fromkeys(tuple(pos) for pos in groups if pos):
            recorded = sum(weight[pos] for pos in group)
            if len(group) > 120 or recorded > 2_000:
                continue
            group_mask = combine(1 << pos for pos in group)
            candidate_files = tuple(sorted({index.ids[pos].partition("::")[0] for pos in group}))
            remaining, _ = schedule(list(positions(selection.mask & ~group_mask)), weight)
            saving = cost - remaining
            # Import/setup and test CPU are charged even when nothing is removed.
            extra_files = [path for path in candidate_files if path not in collection_files]
            startup = (0 if collection_files else 200) + 85 * len(extra_files)
            if any(self.heavy_imports(path) for path in extra_files):
                startup += 450
            pass_likelihood = 0.50 if len(group) >= 3 else (0.24 if len(group) == 2 else 0.12)
            expected = pass_likelihood * saving - startup - 1.08 * recorded - 60
            if expected > benefit:
                benefit, duration = expected, recorded
                tests = tuple(index.ids[pos] for pos in group)
                files = tuple(sorted(set(collection_files).union(candidate_files)))
        if not files:
            return None
        priority = 10_000 + benefit if selection.collection else benefit
        seconds = (3.1 if selection.collection else 1.25) + 1.35 * duration / 1000
        return Probe(files, tests, seconds, priority)

    def resolve(self, selection, probe, deadline):
        index = self.catalog
        passed, failed, errored, collected = run_probe(self.root, selection.edits, probe, deadline)
        mask = selection.mask
        failures = 0
        for path in errored:
            if path in index.files:
                failures |= index.files[path]
            elif path in {"", "."}:
                failures |= index.all
            else:
                name = path.rstrip("/") + "/"
                failures |= combine(tests for file, tests in index.files.items() if file.startswith(name))
        if selection.risky and "*" not in collected:
            # A subprocess can die while importing one file. Keep the tests of
            # each unchecked file, plus all original candidates and known errors.
            checked = failures | combine(index.files.get(path, 0) for path in collected)
            requested = combine(index.files.get(path, 0) for path in probe.files)
            mask |= requested & ~checked
            passed = set()
        for name in passed - failed:
            pos = index.by_name.get(name)
            if pos is not None:
                mask &= ~(1 << pos)
        for name in failed:
            pos = index.by_name.get(name)
            if pos is not None:
                mask |= 1 << pos
        return mask | failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    parser.add_argument("--index", type=Path, default=HERE / "dependencies.json.gz")
    parser.add_argument("--static", action="store_true", help="Alias for --verification static")
    parser.add_argument("--verification", choices=("static", "collection", "adaptive"),
                        default="adaptive", help="Which runtime verification stages to run")
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    deadline = started + 35
    manifest = json.loads((args.data / "tests.json").read_text(encoding="utf-8"))
    with gzip.open(args.index, "rt", encoding="utf-8") as stream:
        index = Catalog(manifest, json.load(stream))
    selector = Selector(args.source.resolve(), index)
    patches = sorted((args.data / "commits").glob("*.patch"))
    selections = {patch.stem: selector.select(patch.read_bytes()) for patch in patches}
    masks = {name: selection.mask for name, selection in selections.items()}
    mode = "static" if args.static else args.verification
    if mode != "static":
        plans = []
        for name, selection in selections.items():
            plan = selector.plan(selection, mode)
            if plan:
                plans.append((name, plan))
        plans.sort(key=lambda pair: -pair[1].priority)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {executor.submit(selector.resolve, selections[name], plan, deadline): name
                       for name, plan in plans}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    masks[name] = future.result()
                except (OSError, ValueError, SubprocessError):
                    if selections[name].risky:
                        masks[name] = index.all
    for name, mask in masks.items():
        cost, workers = schedule(list(positions(mask)), index.weights)
        fill_headroom(workers, positions(selections[name].reserve & ~mask), index.weights)
        numbers = [[index.numbers[pos] for pos in worker] for worker in workers]
        print(json.dumps({"commit": name, "workers": numbers}, separators=(",", ":")))
        if args.explain:
            print(f"{name} {selections[name].mask.bit_count()} -> {sum(map(len, workers))}"
                  f" tests, {cost / 1000:.3f}s", file=sys.stderr)
            guarded = selections[name].guard_exclusions & ~mask
            if guarded or workers:
                longest = max(positions(guarded), key=index.weights.__getitem__, default=None)
                if longest is not None:
                    print(f"  parser guard excluded {guarded.bit_count()} tests; "
                          f"longest {index.weights[longest] / 1000:.3f}s {index.ids[longest]}",
                          file=sys.stderr)
                if workers:
                    busiest = max(workers, key=lambda worker: sum(index.weights[pos] for pos in worker))
                    job = max(busiest, key=index.weights.__getitem__)
                    print(f"  longest test on busiest worker {index.weights[job] / 1000:.3f}s "
                          f"{index.ids[job]}", file=sys.stderr)


if __name__ == "__main__":
    main()
