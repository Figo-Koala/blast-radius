"""Observe operand types and small literal-bound comparisons on the baseline."""

import ast
from builtins import type as builtin_type
from enum import Enum
from functools import lru_cache
from importlib.abc import MetaPathFinder
from importlib.machinery import PathFinder, SourceFileLoader
import json
from pathlib import Path
import sys
from types import NoneType

from scalars import OPERATIONS, literal, snapshot

import guard_trace


active = None
guards_only = False
lookup_sites = set()
tracked_fields = set()
scalar_fields = set()
parameter_sites = {}
guarded_equality = set()
source_root = Path("/")
map_origins = {}



@lru_cache(maxsize=4096)
def class_info(cls):
    identity = f"{cls.__module__}.{cls.__qualname__}"
    equality = cls.__eq__
    inequality = cls.__ne__
    if equality is object.__eq__ and inequality is object.__ne__:
        protocol = "object"
    elif any(cls is kind for kind in (bool, int, float, complex, str, bytes, bytearray, tuple,
                                      list, dict, set, frozenset, range, memoryview, NoneType)):
        protocol = "builtin"
    elif issubclass(cls, Enum) and any(
        equality is base.__eq__ and inequality is base.__ne__ for base in (str, int)
    ):
        protocol = "enum-scalar"
    elif guarded_comparison(equality):
        protocol = "type-guarded-eq"
    else:
        protocol = "other"
    return (identity, protocol)


def guarded_comparison(function):
    code = getattr(function, "__code__", None)
    if code is None or function.__globals__.get("type", builtin_type) is not builtin_type:
        return False
    path = Path(code.co_filename)
    return path.is_relative_to(source_root) and (
        f"{path.relative_to(source_root).as_posix()}:{code.co_firstlineno}" in guarded_equality
    )


def guarded_equality_sites(root):
    sites = []
    for path in (root / "sqlglot").rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        for method in ast.walk(ast.parse(path.read_bytes(), filename=relative)):
            if not isinstance(method, ast.FunctionDef) or method.name != "__eq__":
                continue
            if method.decorator_list or len(method.args.args) != 2 or len(method.body) != 1:
                continue
            statement = method.body[0]
            if not isinstance(statement, ast.Return):
                continue
            first, second = (argument.arg for argument in method.args.args)
            branch = statement.value
            if isinstance(branch, ast.BoolOp) and isinstance(branch.op, ast.Or):
                prefix = ast.parse(f"{first} is {second}", mode="eval").body
                if len(branch.values) != 2 or ast.dump(branch.values[0]) != ast.dump(prefix):
                    continue
                branch = branch.values[1]
            if not isinstance(branch, ast.BoolOp) or not isinstance(branch.op, ast.And):
                continue
            guard = ast.parse(f"type({first}) is type({second})", mode="eval").body
            if ast.dump(branch.values[0]) == ast.dump(guard):
                sites.append(f"{relative}:{method.lineno}")
    return sorted(sites)


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


def ordering(site, operation, left, right):
    if active is not None:
        pair = snapshot(left), snapshot(right)
        records = active.values.setdefault(site, {})
        if len(records) >= 96 and pair not in records:
            pair = (None, None)
        records[pair] = records.get(pair, 0) | active.test
    return OPERATIONS[operation](left, right)


def fields_with_explicit_reads(root):
    fields, opaque = set(), set()
    for directory in ("sqlglot", "tests"):
        for path in (root / directory).rglob("*.py"):
            tree = ast.parse(path.read_bytes(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for binding in node.body:
                        if isinstance(binding, ast.Assign):
                            targets = binding.targets
                        elif isinstance(binding, ast.AnnAssign):
                            targets = [binding.target]
                        else:
                            continue
                        if literal(binding.value) is not None:
                            fields.update(target.id for target in targets
                                          if isinstance(target, ast.Name) and target.id.isupper())
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    opaque.add(node.id)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    opaque.add(node.value)
    # Bare references and string-based/reflective consumers use the wider policy.
    return fields - opaque


def singleton(value):
    if value is None:
        return "none"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "other"


def parameters_with_singleton_defaults(root):
    sites = {}
    for path in (root / "sqlglot").rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        for node in ast.walk(ast.parse(path.read_bytes(), filename=relative)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            positional = args.posonlyargs + args.args
            defaults = (list(zip(reversed(positional), reversed(args.defaults)))
                        + list(zip(args.kwonlyargs, args.kw_defaults)))
            names = [arg.arg for arg, default in defaults
                     if isinstance(default, ast.Constant) and singleton(default.value) != "other"]
            if names:
                start = min((d.lineno for d in node.decorator_list), default=node.lineno)
                sites[f"{relative}:{start}"] = names
    return sites


def read_index(site, sequence, index):
    if active is not None:
        cls = type(sequence)
        safe = type(index) is int and any(cls is kind for kind in (list, tuple, str, bytes, bytearray, range))
        seen, unsafe = active.subscripts.get(site, (0, 0))
        active.subscripts[site] = seen | active.test, unsafe | (0 if safe else active.test)
    return sequence[index]


def read_attribute(receiver, name):
    value = getattr(receiver, name)
    if active is not None:
        active.attribute(name, receiver, sys._getframe(1))
    return value


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
    def __init__(self, path, protected):
        self.path = path
        self.protected = protected

    def visit(self, node):
        if node in self.protected:
            return node
        return super().visit(node)

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

    def visit_Attribute(self, node):
        self.generic_visit(node)
        # The replacement evaluates the original receiver and performs one lookup.
        if not isinstance(node.ctx, ast.Load) or node.attr not in scalar_fields:
            return node
        call = ast.Call(func=ast.Name(id="_baseline_read_attribute", ctx=ast.Load()),
                        args=[node.value, ast.Constant(node.attr)], keywords=[])
        return ast.copy_location(call, node)

    def visit_Subscript(self, node):
        self.generic_visit(node)
        if not isinstance(node.ctx, ast.Load) or not isinstance(node.slice, (ast.Name, ast.Constant)):
            return node
        site = f"{self.path}:{node.lineno}:{node.col_offset}"
        call = ast.Call(func=ast.Name(id="_baseline_read_index", ctx=ast.Load()),
                        args=[ast.Constant(site), node.value, node.slice], keywords=[])
        return ast.copy_location(call, node)

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
        elif type(op).__name__ in OPERATIONS and any(literal(expr) is not None for expr in (node.left, right)):
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
        if guard_trace.recognizes(self.relative):
            module.__dict__.update(guard_trace.HELPERS)
        if guards_only:
            return super().exec_module(module)
        module.__dict__["_baseline_identity_operands"] = identity
        module.__dict__["_baseline_ordering_operands"] = ordering
        module.__dict__["_baseline_register_mapping"] = register_mapping
        module.__dict__["_baseline_bind_mapping_get"] = bind_mapping_get
        module.__dict__["_baseline_read_attribute"] = read_attribute
        module.__dict__["_baseline_read_index"] = read_index
        return super().exec_module(module)

    def get_code(self, fullname):
        tree = ast.parse(self.get_data(self.path), filename=self.path)
        if guards_only:
            tree = guard_trace.Instrument(self.relative).visit(tree)
            return compile(ast.fix_missing_locations(tree), self.path, "exec")
        protected = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.arg, ast.AnnAssign)):
                protected.add(node.annotation)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                protected.add(node.returns)
            elif isinstance(node, ast.TypeAlias):
                protected.add(node.value)
            if isinstance(node, (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)):
                protected.add(getattr(node, "bound", None))
                protected.add(getattr(node, "default_value", None))
        if guard_trace.recognizes(self.relative):
            tree = guard_trace.Instrument(self.relative).visit(tree)
        tree = Instrument(self.relative, protected).visit(tree)
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class Finder(MetaPathFinder):
    def __init__(self, root):
        self.root = Path(root)

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] not in {"sqlglot", "tests"}:
            return None
        spec = PathFinder.find_spec(fullname, path, target)
        if spec and isinstance(spec.loader, SourceFileLoader):
            origin = Path(spec.origin)
            if origin.is_relative_to(self.root):
                relative = origin.relative_to(self.root).as_posix()
                if not guards_only or guard_trace.recognizes(relative):
                    spec.loader = Loader(fullname, spec.origin, self.root)
        return spec


def install(root, lookup_file):
    global source_root, guards_only
    source_root = Path(root)
    spec = json.loads(Path(lookup_file).read_text())
    guard_trace.configure(root, spec["guards"])
    guards_only = spec.get("guards_only", False)
    tracked_fields.update(spec["maps"])
    lookup_sites.update(site for sites in spec["maps"].values() for site in sites)
    scalar_fields.update(spec["fields"])
    parameter_sites.update(spec["parameters"])
    guarded_equality.update(spec["guarded_equality"])
    sys.meta_path.insert(0, Finder(root))
