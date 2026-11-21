"""Common representation for comparisons over exact, side-effect-free builtins."""

import ast
import math
import operator


OPERATIONS = {
    "Lt": operator.lt,
    "LtE": operator.le,
    "Gt": operator.gt,
    "GtE": operator.ge,
    "Eq": operator.eq,
    "NotEq": operator.ne,
}


def snapshot(value):
    kind = type(value)
    if kind is int and value.bit_length() <= 128:
        return ("int", value)
    if kind is bool:
        return ("bool", value)
    if kind is float and math.isfinite(value):
        return ("float", value)
    if kind is str and len(value) <= 100:
        return ("str", value)
    if kind is tuple and len(value) <= 8:
        items = tuple(snapshot(item) for item in value)
        if all(item is not None and item[0] != "tuple" for item in items):
            return ("tuple", items)
    if value is None:
        return ("none", None)
    return None


def literal(node):
    # Skip arbitrary expression trees, especially the nonconstant side of hot comparisons.
    if not isinstance(node, (ast.Constant, ast.Tuple, ast.UnaryOp)):
        return None
    try:
        return snapshot(ast.literal_eval(node))
    except (TypeError, ValueError, SyntaxError, RecursionError):
        return None


def decode(value):
    kind, item = value
    if kind == "tuple":
        return tuple(decode(child) for child in item)
    return item


def restore_key(data):
    if data is None:
        return None
    return (data[0], tuple(restore_key(child) for child in data[1])
            if data[0] == "tuple" else data[1])
