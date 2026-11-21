"""Observe operand types and small literal-bound comparisons on the baseline."""

import ast
from enum import Enum
from functools import lru_cache
from importlib.abc import MetaPathFinder
from importlib.machinery import PathFinder, SourceFileLoader
import json
import operator
from pathlib import Path
import sys
from types import NoneType


active = None
lookup_sites = set()
tracked_fields = set()
map_origins = {}
ORDERING = {"Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge,
            "Eq": operator.eq, "NotEq": operator.ne}


@lru_cache(maxsize=4096)
def class_info(cls):
    identity = f"{cls.__module__}.{cls.__qualname__}"
    equality = cls.__eq__
    inequality = cls.__ne__
    if equality is object.__eq__ and inequality is object.__ne__:
        protocol = "object"
    elif cls in (bool, int, float, complex, str, bytes, bytearray, tuple, list, dict,
                 set, frozenset, range, memoryview, NoneType):
        protocol = "builtin"
    elif issubclass(cls, Enum) and any(
        equality is base.__eq__ and inequality is base.__ne__ for base in (str, int)
    ):
        protocol = "enum-scalar"
    else:
        protocol = "other"
    return (identity, protocol)


def observed_type(value):
    cls = type(value)
    identity, protocol = class_info(cls)
    if protocol == "enum-scalar":
        try:
            name = object.__getattribute__(value, "_name_")
            if cls.__members__.get(name) is not value:
                protocol = "other"
        except (AttributeError, TypeError):
            protocol = "other"
    return identity, protocol


def identity(site, negated, left, right):
    if active is not None:
        pair = (observed_type(left), observed_type(right))
        observed = active.types.setdefault(site, {})
        observed[pair] = observed.get(pair, 0) | active.test
    return (left is not right) if negated else (left is right)


def native_number(value):
    if type(value) is int and value.bit_length() <= 128:
        return value
    if type(value) is tuple and len(value) <= 8:
        if all(type(item) is int and item.bit_length() <= 128 for item in value):
            return value
    return None


def ordering(site, operation, left, right):
    if active is not None:
        pair = native_number(left), native_number(right)
        records = active.ordering.setdefault(site, {})
        if len(records) >= 128 and pair not in records:
            pair = (None, None)
        records[pair] = records.get(pair, 0) | active.test
    return ORDERING[operation](left, right)


def numeric_literal(node):
    try:
        return native_number(ast.literal_eval(node)) is not None
    except (ValueError, TypeError, SyntaxError):
        return False


def register_mapping(origin, mapping):
    map_origins[id(mapping)] = (origin, mapping)
    return mapping


def bind_mapping_get(site, mapping):
    getter = mapping.get
    if active is None:
        return getter
    origin, _ = map_origins.get(id(mapping), (None, None))
    reads = active.lookups.setdefault(site, {})

    def observe(key, /, *args, **kwargs):
        observed = key if type(key) in {str, int} else None
        pair = origin, observed
        if len(reads) >= 128 and pair not in reads:
            pair = origin, None
        reads[pair] = reads.get(pair, 0) | active.test
        return getter(key, *args, **kwargs)

    return observe


class Instrument(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def mapping_assignment(self, node, targets):
        self.generic_visit(node)
        if isinstance(node.value, ast.Dict) and any(
            isinstance(target, ast.Name) and target.id in tracked_fields for target in targets
        ):
            origin = f"{self.path}:{node.lineno}"
            tracker = ast.Call(func=ast.Name(id="_baseline_register_mapping", ctx=ast.Load()),
                               args=[ast.Constant(origin), node.value], keywords=[])
            node.value = ast.copy_location(tracker, node.value)
        return node

    def visit_Assign(self, node):
        return self.mapping_assignment(node, node.targets)

    def visit_AnnAssign(self, node):
        return self.mapping_assignment(node, [node.target])

    def visit_Call(self, node):
        self.generic_visit(node)
        site = f"{self.path}:{node.lineno}:{node.col_offset}"
        if site in lookup_sites:
            bound = ast.Call(func=ast.Name(id="_baseline_bind_mapping_get", ctx=ast.Load()),
                             args=[ast.Constant(site), node.func.value], keywords=[])
            node.func = ast.copy_location(bound, node.func)
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1:
            return node
        right = node.comparators[0]
        op = node.ops[0]
        if isinstance(op, (ast.Is, ast.IsNot)) and any(
            (isinstance(part, ast.Constant) and part.value is None)
            or (isinstance(part, ast.Attribute) and part.attr.isupper()
                and isinstance(part.value, ast.Name))
            for part in (node.left, right)
        ):
            observer, operation = "_baseline_identity_operands", isinstance(op, ast.IsNot)
        elif type(op).__name__ in ORDERING and any(map(numeric_literal, (node.left, right))):
            observer, operation = "_baseline_ordering_operands", type(op).__name__
        else:
            return node
        site = f"{self.path}:{node.lineno}:{node.col_offset}"
        call = ast.Call(func=ast.Name(id=observer, ctx=ast.Load()),
                        args=[ast.Constant(site), ast.Constant(operation), node.left, right],
                        keywords=[])
        return ast.copy_location(call, node)


class Loader(SourceFileLoader):
    def __init__(self, name, path, root):
        super().__init__(name, path)
        self.relative = Path(path).relative_to(root).as_posix()

    def exec_module(self, module):
        module.__dict__["_baseline_identity_operands"] = identity
        module.__dict__["_baseline_ordering_operands"] = ordering
        module.__dict__["_baseline_register_mapping"] = register_mapping
        module.__dict__["_baseline_bind_mapping_get"] = bind_mapping_get
        return super().exec_module(module)

    def get_code(self, fullname):
        tree = ast.parse(self.get_data(self.path), filename=self.path)
        tree = Instrument(self.relative).visit(tree)
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class Finder(MetaPathFinder):
    def __init__(self, root):
        self.root = Path(root)

    def find_spec(self, fullname, path=None, target=None):
        if not (fullname == "sqlglot" or fullname.startswith("sqlglot.")):
            return None
        spec = PathFinder.find_spec(fullname, path, target)
        if spec and isinstance(spec.loader, SourceFileLoader):
            if Path(spec.origin).is_relative_to(self.root):
                spec.loader = Loader(fullname, spec.origin, self.root)
        return spec


def install(root, lookup_file):
    spec = json.loads(Path(lookup_file).read_text())
    tracked_fields.update(spec)
    lookup_sites.update(site for sites in spec.values() for site in sites)
    sys.meta_path.insert(0, Finder(root))
