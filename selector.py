"""Select tests by baseline dependencies, then verify affordable candidates."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import lru_cache
import gzip
import json
import operator
from pathlib import Path
import sys
from subprocess import SubprocessError
import time

from impact import analyze
from patches import PatchError, apply_file, read_patch
from schedule import schedule
from verify import Probe, run_probe


HERE = Path(__file__).resolve().parent
COLLECTION = 1
DOC_SUFFIXES = {".md", ".rst"}
PACKAGE_PREFIXES = ("sqlglot/", "sqlglotc/", "tests/")
ORDERING = {"Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge,
            "Eq": operator.eq, "NotEq": operator.ne}


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


def identity_matches_equality(left, right):
    left_type, left_protocol = left
    right_type, right_protocol = right
    safe = {"object", "builtin", "enum-scalar"}
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
        self.ordering = index.get("ordering", {})
        self.projected_maps = index.get("projected_maps", {})
        self.lookups = {site: {(origin, key): int(bits, 16) for origin, key, bits in entries}
                        for site, entries in index.get("lookups", {}).items()}
        self.identity_safe = {}
        for site, pairs in index.get("types", {}).items():
            seen, unsafe = 0, 0
            for (left, right), raw in pairs:
                bits = int(raw, 16)
                seen |= bits
                if not identity_matches_equality(left, right):
                    unsafe |= bits
            self.identity_safe[site] = seen, seen & ~unsafe

    def comparison(self, site, before, after):
        if before == "identity":
            return self.identity_safe.get(site, (0, 0))
        first, second = ORDERING[before], ORDERING[after]
        seen, unsafe = 0, 0
        for (left, right), raw in self.ordering.get(site, ()):
            bits = int(raw, 16)
            seen |= bits
            if (left is None or right is None or type(left) is not type(right)
                    or first(left, right) != second(left, right)):
                unsafe |= bits
        return seen, seen & ~unsafe

    def value_reads(self, name, origin, keys):
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
                            if source in {None, origin} and (key is None or key in keys))
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

    def python(self, path, original, modified, selection):
        index = self.catalog
        direct = index.readers(path)
        try:
            impact = analyze(original, modified, path)
        except (SyntaxError, ValueError, RecursionError):
            selection.collection = selection.risky = True
            return index.all
        coverage = index.lines.get(path, {})
        hits = combine(coverage.get(line, 0) for line in impact.lines)
        for name, keys, lines, owner in impact.dictionary_edits:
            readers = index.value_reads(name, f"{path}:{lines[0]}", keys)
            if readers is None:
                impact.symbols.add(name)
                impact.owners.add(owner)
                hits |= combine(coverage.get(line, 0) for line in lines)
            else:
                hits |= readers
        for line in impact.lambdas:
            hits |= index.lambdas.get(f"{path}:{line}", 0)
        for site, lines, before, after in impact.comparisons:
            seen, equivalent = index.comparison(site, before, after)
            covered = combine(coverage.get(line, 0) for line in lines)
            hits |= (covered | seen) & ~equivalent
        if impact.symbols:
            symbols = combine(index.symbols.get(name, 0) for name in impact.symbols)
            owned = combine(
                index.receivers.get(f"{path[:-3].replace('/', '.')}:{owner}", 0)
                for owner in impact.owners if owner
            )
            if owned and None not in impact.owners:
                # Covered receiver classes include subclasses and dialects used
                # indirectly through parse, tokenize and generate.
                owner_names = combine(index.symbols.get(owner, 0)
                                      for owner in impact.owners if owner)
                if symbols & COLLECTION or impact.collection:
                    symbols |= owned
                hits = (symbols & (owned | owner_names)) | (hits & ~COLLECTION)
            else:
                hits |= symbols | index.file_coverage.get(path, 0)

        if impact.collection or impact.broad or (hits & COLLECTION and not impact.symbols):
            # Import-time execution may leave cached globals or prevent collection.
            selection.collection = selection.risky = True
            if impact.broad or not (hits >> 1):
                return index.all
        if not (impact.lines or impact.symbols or impact.collection or impact.comparisons
                or impact.lambdas or impact.dictionary_edits):
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
                    result.mask |= self.python(path, original, modified, result)
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
            return result
        except (OSError, PatchError, UnicodeError, ValueError, RecursionError):
            return Selection(mask=index.all, probe=False)

    def plan(self, selection):
        index = self.catalog
        if not selection.probe or (not selection.mask and not selection.collection):
            return None
        if selection.fixture_only:
            return None
        selected = list(positions(selection.mask & index.verified))
        total_ms = sum(index.weights[pos] for pos in selected)
        if not selection.risky and (len(selected) < 3 or total_ms > 400):
            # For larger selections, verifying candidates costs more CPU than
            # the typical savings in simulated worker time.
            return None
        selected.sort(key=lambda pos: (index.weights[pos], index.ids[pos]))
        affordable = []
        elapsed_ms = 0
        limit_ms = 700 if selection.risky else 850
        for pos in selected:
            duration = index.weights[pos]
            if len(affordable) >= 100 or elapsed_ms + duration > limit_ms:
                break
            affordable.append(pos)
            elapsed_ms += duration
        if not affordable and not selection.collection:
            return None
        files = (tuple(sorted(index.files)) if selection.collection else
                 tuple(sorted({index.ids[pos].split("::", 1)[0] for pos in affordable})))
        cost, _ = schedule(list(positions(selection.mask)), index.weights)
        priority = 10_000 if selection.collection else cost / (500 + elapsed_ms)
        seconds = (3.5 if selection.collection else 1.5) + 1.3 * elapsed_ms / 1000
        return Probe(files, tuple(index.ids[pos] for pos in affordable), seconds, priority)

    def resolve(self, selection, probe, deadline):
        index = self.catalog
        passed, failed, errored, collected = run_probe(self.root, selection.edits, probe, deadline)
        mask = selection.mask
        if selection.risky and "*" not in collected:
            # Import/collection was interrupted. Preserve the conservative result.
            return index.all
        for name in passed - failed:
            pos = index.by_name.get(name)
            if pos is not None:
                mask &= ~(1 << pos)
        for name in failed:
            pos = index.by_name.get(name)
            if pos is not None:
                mask |= 1 << pos
        for path in errored:
            mask |= index.files.get(path, index.all if path in {"", "."} else 0)
        return mask


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    parser.add_argument("--index", type=Path, default=HERE / "dependencies.json.gz")
    parser.add_argument("--static", action="store_true")
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
    if not args.static:
        plans = []
        for name, selection in selections.items():
            plan = selector.plan(selection)
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
        numbers = [[index.numbers[pos] for pos in worker] for worker in workers]
        print(json.dumps({"commit": name, "workers": numbers}, separators=(",", ":")))
        if args.explain:
            print(f"{name} {selections[name].mask.bit_count()} -> {mask.bit_count()}"
                  f" tests, {cost / 1000:.3f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
