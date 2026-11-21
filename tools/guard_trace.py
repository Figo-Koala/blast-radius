"""Collect baseline token and argument facts without executing skipped branches."""

import ast
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
import sys
from types import FunctionType, MemberDescriptorType, MethodType

from parser_guards import (ATTRS, PARSER, body_of, direct_match, entry_guard, match_implementation,
                           optional_method, self_attribute, short_circuit, site)


active = None
spec = {}
implementations = set()
optional_names = set()
root = Path("/")


def configure(source, config):
    global root, spec
    root = Path(source)
    spec = config
    implementations.update(tuple(row) for row in config["implementations"])
    optional_names.update(config["optional"])


def discover(source):
    # The hot paths in this index are limited to the base parser and its dialect subclasses.
    root_file = source / PARSER
    files = [root_file, *sorted((source / "sqlglot/parsers").rglob("*.py"))]
    config = defaultdict(list)
    for file in files:
        relative = file.relative_to(source).as_posix()
        tree = ast.parse(file.read_bytes(), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ATTRS):
                if relative == PARSER and match_implementation(node):
                    config["implementations"].append([node.name, node.lineno])
                if optional_method(node):
                    definition = optional_method(node)
                    config["definitions"].append([relative, node.lineno, node.name, *definition])
    token_file = ast.parse((source / "sqlglot/tokenizer_core.py").read_bytes())
    token_class = next(node for node in token_file.body if isinstance(node, ast.ClassDef)
                       and node.name == "TokenType")
    tokens = [stmt.targets[0].id for stmt in token_class.body
              if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
              and isinstance(stmt.targets[0], ast.Name) and isinstance(stmt.value, ast.Call)
              and isinstance(stmt.value.func, ast.Name) and stmt.value.func.id == "auto"
              and not stmt.value.args and not stmt.value.keywords]
    return {"implementations": config["implementations"], "definitions": config["definitions"],
            "tokens": tokens, "optional": sorted({row[2] for row in config["definitions"]})}


def class_member(cls, name):
    return next((vars(base)[name] for base in cls.__mro__ if name in vars(base)), None)


def is_match(function, name):
    return (type(function) is FunctionType and function.__code__.co_filename == str(root / PARSER)
            and (name, function.__code__.co_firstlineno) in implementations)


def token_state(receiver, matcher, bound=None):
    cls = type(receiver)
    if cls.__getattribute__ is not object.__getattribute__:
        return None
    data = getattr(receiver, "__dict__", None)
    if data is not None and (type(data) is not dict or matcher in data):
        return None
    function = class_member(cls, matcher)
    if not is_match(function, matcher) or (bound is not None and function is not bound):
        return None
    defaults = (True, None) if matcher == "_match" else (True,)
    if function.__defaults__ != defaults:
        return None
    module = sys.modules.get("sqlglot.tokenizer_core")
    if not module:
        return None
    if type(class_member(cls, "_curr")) is not MemberDescriptorType:
        return None
    current = getattr(receiver, "_curr", None)
    if type(current) is not module.Token or type(current.token_type) is not module.TokenType:
        return None
    return current.token_type


def record(key, state):
    if active is not None:
        rows = active.guards.setdefault(key, {})
        rows[state] = rows.get(state, 0) | active.test


def observe_entry(key, receiver, flag, skip):
    if active is None:
        return
    token = token_state(receiver, "_match")
    valid = token is not None and sys._getframe(1).f_globals.get("TokenType") is type(token)
    record(key, (token.name if valid else None,
                 flag if type(flag) is bool else None, skip if type(skip) is bool else None))


def observe_left(key, receiver, result):
    if active is None:
        return result
    token = token_state(receiver, "_match")
    valid = token is not None and sys._getframe(1).f_globals.get("TokenType") is type(token)
    record(key, (token.name if valid else None, result if type(result) is bool else None))
    return result


def observe_match(key, function, target):
    if active is not None:
        token = None
        if type(function) is MethodType:
            token = token_state(function.__self__, "_match", function.__func__)
        valid = token is not None and type(target) is type(token)
        record(key, (token.name, target.name) if valid else (None, None))
    return function(target)


def observe_optional(key, function, expected):
    # Record whether the old call entered the side-effecting branch. The
    # actual baseline invocation still runs normally.
    if active is None:
        return function()
    was_read_only = False
    if type(function) is MethodType and function.__func__.__name__ == expected:
        receiver = function.__self__
        code = function.__func__.__code__
        definitions = optional_definitions()
        definition = definitions.get((code.co_filename, code.co_firstlineno, expected))
        if definition:
            matcher, operand = definition
            current = token_state(receiver, matcher)
            if current is not None:
                if matcher == "_match":
                    canonical = type(current)
                    actual = function.__func__.__globals__.get("TokenType")
                    target = canonical.__members__.get(operand)
                    was_read_only = actual is canonical and target is not None and current is not target
                elif type(class_member(type(receiver), operand)) in (dict, set, frozenset, tuple):
                    members = getattr(receiver, operand)
                    if type(members) in (dict, set, frozenset, tuple) and all(
                        type(token) is type(current) for token in members
                    ):
                        was_read_only = current not in members
    try:
        result = function()
    except BaseException:
        record(key, (False,))
        raise
    record(key, (was_read_only and result is None,))
    return result


@lru_cache(maxsize=1)
def optional_definitions():
    return {(str(root / path), line, name): (matcher, operand)
            for path, line, name, matcher, operand in spec["definitions"]}


def wrap(name, old, args):
    node = ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=args, keywords=[])
    return ast.copy_location(node, old)


class Instrument(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        guard = entry_guard(node)
        if guard and not node.decorator_list:
            flag, skip, _, _ = guard
            original = body_of(node)[0]
            key = f"{self.path}:{node.lineno}:entry"
            call = wrap("_br_observe_entry", original, [
                ast.Constant(key), ast.Name(id="self", ctx=ast.Load()),
                ast.Name(id=flag, ctx=ast.Load()), ast.Name(id=skip, ctx=ast.Load()),
            ])
            node.body.insert(len(node.body) - len(body_of(node)), ast.copy_location(ast.Expr(call), original))
        return node

    def visit_If(self, node):
        # Wrap the original left operand. Python still short-circuits the RHS.
        original = node.test
        gate = short_circuit(original)
        simple = direct_match(original)
        self.generic_visit(node)
        if gate:
            left = node.test.values[0]
            node.test.values[0] = wrap("_br_observe_left", left, [
                ast.Constant(site(self.path, original)), ast.Name(id="self", ctx=ast.Load()),
                left,
            ])
        elif simple:
            call, _, negated = simple
            watched = wrap("_br_observe_match", call, [
                ast.Constant(site(self.path, call)), call.func, call.args[0],
            ])
            if negated:
                node.test.operand = watched
            else:
                node.test = watched
        return node

    def visit_Return(self, node):
        self.generic_visit(node)
        call = node.value
        if isinstance(call, ast.Call) and not call.args and not call.keywords:
            target = self_attribute(call.func)
            if target in optional_names:
                node.value = wrap("_br_observe_optional", call, [
                    ast.Constant(site(self.path, call)), call.func, ast.Constant(target),
                ])
        return node


HELPERS = {
    "_br_observe_entry": observe_entry, "_br_observe_left": observe_left,
    "_br_observe_match": observe_match, "_br_observe_optional": observe_optional,
}


def recognizes(path):
    return path == PARSER or path.startswith("sqlglot/parsers/")
