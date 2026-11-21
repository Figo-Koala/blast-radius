"""Recognize a small set of SQLGlot parser decisions with provable no-op paths."""

import ast


PARSER = "sqlglot/parser.py"
ATTRS = (ast.FunctionDef, ast.AsyncFunctionDef)


def is_name(node, name):
    return isinstance(node, ast.Name) and node.id == name


def self_attribute(node):
    return node.attr if isinstance(node, ast.Attribute) and is_name(node.value, "self") else None


def token_name(node):
    if isinstance(node, ast.Attribute) and is_name(node.value, "TokenType"):
        return node.attr
    return None


def match_call(node):
    if isinstance(node, ast.Call) and not node.keywords and len(node.args) == 1:
        name = self_attribute(node.func)
        if name in {"_match", "_match_set"}:
            return name, node.args[0]
    return None, None


def short_circuit(node):
    if isinstance(node, ast.BoolOp) and len(node.values) == 2:
        method, arg = match_call(node.values[1])
        if method == "_match" and (token := token_name(arg)) is not None:
            return node.values[0], token
    return None


def direct_match(test):
    negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
    call = test.operand if negated else test
    method, operand = match_call(call)
    if method == "_match" and (token := token_name(operand)):
        return call, token, negated
    return None


def body_of(function):
    return function.body[1:] if ast.get_docstring(function, clean=False) is not None else function.body


def entry_guard(function):
    """Find a flag used only in the first match gate and its conditional body."""
    body = body_of(function)
    if not body or not isinstance(body[0], ast.If) or body[0].orelse:
        return None
    guard = body[0].test
    if not isinstance(guard, ast.BoolOp) or not isinstance(guard.op, ast.Or) or len(guard.values) != 2:
        return None
    skip, call = guard.values
    method, choose = match_call(call)
    if method != "_match" or not isinstance(choose, ast.IfExp) or not isinstance(choose.test, ast.Name):
        return None
    flag = choose.test.id
    first, second = token_name(choose.body), token_name(choose.orelse)
    if not isinstance(skip, ast.Name) or skip.id == flag or not first or not second:
        return None
    params = {arg.arg for arg in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)}
    if not {flag, skip.id}.issubset(params):
        return None
    for stmt in body[1:]:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and node.id in {flag, "locals", "eval", "exec", "vars", "inspect", "sys"}:
                return None
    return flag, skip.id, first, second


def optional_method(function):
    """Recognize methods whose only code on a failed initial match returns None."""
    body = body_of(function)
    if (function.decorator_list or function.args.posonlyargs or function.args.kwonlyargs
            or function.args.vararg or function.args.kwarg or function.args.defaults
            or [arg.arg for arg in function.args.args] != ["self"] or len(body) != 2):
        return None
    branch, last = body
    if (not isinstance(branch, ast.If) or branch.orelse or not isinstance(last, ast.Return)
            or not (last.value is None or isinstance(last.value, ast.Constant) and last.value.value is None)):
        return None
    method, arg = match_call(branch.test)
    token = token_name(arg) if method == "_match" else self_attribute(arg)
    return (method, token) if method and token else None


def match_implementation(function):
    """Accept the pinned cursor matcher only when its negative path is read-only."""
    body = body_of(function)
    if function.decorator_list or len(body) != 2:
        return False
    branch, last = body
    if (not isinstance(branch, ast.If) or branch.orelse or not isinstance(last, ast.Return)
            or not isinstance(last.value, ast.Constant) or last.value.value is not False):
        return False
    expected = {
        "_match": "self._curr.token_type == token_type",
        "_match_set": "self._curr.token_type in types",
    }.get(function.name)
    return expected is not None and ast.dump(branch.test) == ast.dump(ast.parse(expected, mode="eval").body)


def site(path, node):
    return f"{path}:{node.lineno}:{node.col_offset}"
