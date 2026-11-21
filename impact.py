"""Map structural source changes to executable lines and import-time effects."""

import ast
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from marshal import dumps
from types import CodeType

from scalars import literal as scalar_literal
from parser_guards import direct_match, entry_guard, self_attribute, short_circuit, site


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
ORDERING = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)


@dataclass
class Impact:
    lines: set[int] = field(default_factory=set)
    symbols: set[str] = field(default_factory=set)
    owners: set[str] = field(default_factory=set)
    comparisons: list = field(default_factory=list)
    lambdas: set[int] = field(default_factory=set)
    dictionary_edits: list = field(default_factory=list)
    attribute_edits: list = field(default_factory=list)
    parameter_assignments: list = field(default_factory=list)
    parser_guards: list = field(default_factory=list)
    parser_entries: dict = field(default_factory=dict)
    collection: bool = False
    broad: bool = False
    import_change: bool = False
    other_initializers: bool = False


def span(node):
    return range(node.lineno, node.end_lineno + 1)


def decorated_span(node):
    start = min((item.lineno for item in getattr(node, "decorator_list", ())), default=node.lineno)
    return range(start, node.end_lineno + 1)


def names(target):
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Attribute):
        return {target.attr}
    if isinstance(target, (ast.List, ast.Tuple)):
        return set().union(*(names(item) for item in target.elts))
    return set()


def assigned(statement):
    if isinstance(statement, ast.Assign):
        return set().union(*(names(target) for target in statement.targets))
    if isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
        return names(statement.target)
    if isinstance(statement, (ast.ClassDef, *FUNCTIONS)):
        return {statement.name}
    return set()


def literal(node):
    if node is None or any(isinstance(part, ast.Call) for part in ast.walk(node)):
        return None
    try:
        return type(ast.literal_eval(node))
    except (ValueError, TypeError, SyntaxError):
        return None


def changed_dictionary_values(before, after):
    if not (isinstance(before, ast.Dict) and isinstance(after, ast.Dict)):
        return None
    if any(key is None for key in before.keys) or len(before.keys) != len(after.keys):
        return None
    if any(ast.dump(left) != ast.dump(right) for left, right in zip(before.keys, after.keys)):
        return None
    changed = [(key, left, right) for key, left, right in zip(before.keys, before.values, after.values)
               if ast.dump(left) != ast.dump(right)]
    if not changed or any(literal(left) is None or literal(right) is None
                          for _, left, right in changed):
        return None
    keys = set()
    for key, _, _ in changed:
        value = scalar_literal(key)
        if value is None or value[0] not in {"int", "str"}:
            # A stable key expression still permits narrowing to value reads.
            # Unknown key values are conservatively included for this origin.
            return set(), True
        keys.add(value[1])
    return keys, False


IDENTITY_INDEX_OPERATORS = {
    ast.Add: 0, ast.Sub: 0, ast.Mult: 1, ast.FloorDiv: 1,
    ast.BitOr: 0, ast.BitXor: 0, ast.BitAnd: -1, ast.LShift: 0, ast.RShift: 0,
}
COMMUTATIVE = (ast.Add, ast.Mult, ast.BitOr, ast.BitXor, ast.BitAnd)


def singleton_name(node):
    value = scalar_literal(node)
    if value is None:
        return None
    if value[0] == "none":
        return "none"
    if value[0] == "bool":
        return "true" if value[1] else "false"
    return None


def truth_expression(node):
    """Drop literal identity operands only where the result is truth-tested."""
    while isinstance(node, ast.BoolOp) and len(node.values) == 2:
        identity = isinstance(node.op, ast.And)
        left, right = node.values
        if isinstance(left, ast.Constant) and left.value is identity:
            node = right
        elif isinstance(right, ast.Constant) and right.value is identity:
            node = left
        else:
            break
    return node


def executable_signature(code):
    # Marshal v2 preserves signed zeros but has no reference/interning flags.
    constants = tuple(executable_signature(value) if isinstance(value, CodeType) else dumps(value, 2)
                      for value in code.co_consts)
    return (
        code.co_code, constants, code.co_names, code.co_varnames, code.co_cellvars,
        code.co_freevars, code.co_exceptiontable, code.co_flags,
        code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount,
        code.co_name, code.co_qualname,
    )


@lru_cache(maxsize=16)
def original_module(source, path):
    tree = ast.parse(source, filename=path, type_comments=True)
    return tree, executable_signature(compile(tree, path, "exec"))


def analyze(original, modified, path):
    result = Impact()
    old, original_signature = original_module(original, path)
    new = ast.parse(modified, filename=path, type_comments=True)
    # AST parsing alone accepts some invalid constructs such as top-level return.
    patched_code = compile(new, path, "exec")
    tags_changed = [item.tag for item in old.type_ignores] != [item.tag for item in new.type_ignores]
    if tags_changed:
        result.collection = result.broad = result.other_initializers = True
    elif executable_signature(patched_code) == original_signature:
        # Constant folding and formatting can leave executable code identical.
        # The selector still preserves tests that read the source.
        return result
    shapes = {}

    def shape(node):
        if node not in shapes:
            shapes[node] = ast.dump(node, include_attributes=False)
        return shapes[node]

    def equivalent(left, right):
        if isinstance(left, ast.AST) and isinstance(right, ast.AST):
            return shape(left) == shape(right)
        if isinstance(left, list) and isinstance(right, list):
            return len(left) == len(right) and all(equivalent(a, b) for a, b in zip(left, right))
        return left == right

    def comparison_changes(left, right):
        if equivalent(left, right):
            return []
        if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
            pairs = zip(left, right)
        elif isinstance(left, ast.AST) and type(left) is type(right):
            if (isinstance(left, ast.Subscript) and isinstance(left.ctx, ast.Load)
                    and equivalent(left.value, right.value) and isinstance(right.slice, ast.BinOp)):
                op = type(right.slice.op)
                if op in IDENTITY_INDEX_OPERATORS:
                    expected = ("int", IDENTITY_INDEX_OPERATORS[op])
                    same_index = equivalent(left.slice, right.slice.left) and scalar_literal(right.slice.right) == expected
                    reversed_index = (op in COMMUTATIVE and equivalent(left.slice, right.slice.right)
                                      and scalar_literal(right.slice.left) == expected)
                    if same_index or reversed_index:
                        return [(left, "index", "", None, None)]
            if isinstance(left, ast.Compare) and len(left.ops) == len(right.ops) == 1:
                before, after = type(left.ops[0]), type(right.ops[0])
                old_exprs = (left.left, left.comparators[0])
                new_exprs = (right.left, right.comparators[0])
                same = [equivalent(old, new) for old, new in zip(old_exprs, new_exprs)]
                if all(same) and (before, after) in {(ast.Is, ast.Eq), (ast.IsNot, ast.NotEq)}:
                    return [(left, "identity", after.__name__, None, None)]
                if before in ORDERING and after in ORDERING:
                    old_bound = tuple(None if equal else scalar_literal(old)
                                      for equal, old in zip(same, old_exprs))
                    new_bound = tuple(None if equal else scalar_literal(new)
                                      for equal, new in zip(same, new_exprs))
                    if all(equal or (old is not None and new is not None)
                           for equal, old, new in zip(same, old_bound, new_bound)):
                        return [(left, before.__name__, after.__name__, new_bound, all(same))]
            pairs = ((value, getattr(right, key)) for key, value in ast.iter_fields(left))
        else:
            return None
        changes = []
        for old_part, new_part in pairs:
            found = comparison_changes(old_part, new_part)
            if found is None:
                return None
            changes.extend(found)
        return changes

    def deferred_changes(left, right):
        if equivalent(left, right):
            return []
        if isinstance(left, ast.Lambda) and isinstance(right, ast.Lambda):
            return [left] if equivalent(left.args, right.args) else None
        if isinstance(left, ast.Dict) and isinstance(right, ast.Dict):
            if not equivalent(left.keys, right.keys):
                return None
            pairs = zip(left.values, right.values)
        elif isinstance(left, (ast.List, ast.Tuple)) and type(left) is type(right):
            if len(left.elts) != len(right.elts):
                return None
            pairs = zip(left.elts, right.elts)
        else:
            return None
        changes = []
        for old_part, new_part in pairs:
            found = deferred_changes(old_part, new_part)
            if found is None:
                return None
            changes.extend(found)
        return changes

    def mark(old_node, new_node, in_function, owner):
        if old_node is not None:
            result.lines.update(decorated_span(old_node))
        if in_function:
            return
        changed_names = set()
        for node in (old_node, new_node):
            if node is not None:
                changed_names.update(assigned(node))
        result.symbols.update(changed_names)
        result.owners.add(owner)
        if all(node is None or isinstance(node, (ast.Import, ast.ImportFrom))
               for node in (old_node, new_node)):
            result.import_change = True
        else:
            result.other_initializers = True
        # Literal edits to an existing binding can be traced to references. A
        # constructor, import, registration or replaced binding may fail on import.
        assignments = (ast.Assign, ast.AnnAssign)
        if (isinstance(old_node, assignments) and isinstance(new_node, assignments)
                and assigned(old_node) == assigned(new_node)):
            kind = literal(old_node.value)
            if kind is not None and kind is literal(new_node.value) and changed_names:
                return
        result.collection = True
        result.broad |= not changed_names

    def parameter_assignment(node, function):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            return None
        target = node.targets[0]
        if not isinstance(target, ast.Name) or (value := singleton_name(node.value)) is None:
            return None
        params = (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
        if any(arg.arg == target.id for arg in params):
            return target.id, value
        return None

    def compare_suite(left, right, in_function, anchor, owner=None):
        if equivalent(left, right):
            return
        matcher = SequenceMatcher(None, [shape(node) for node in left],
                                  [shape(node) for node in right], autojunk=False)
        for tag, start, end, begin_new, end_new in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace" and end - start == end_new - begin_new:
                for old_node, new_node in zip(left[start:end], right[begin_new:end_new]):
                    compare_stmt(old_node, new_node, in_function, owner)
                continue
            if tag in {"delete", "replace"}:
                for node in left[start:end]:
                    mark(node, None, in_function, owner)
            if (tag == "insert" and isinstance(anchor, FUNCTIONS) and left
                    and ((start == 0 and ast.get_docstring(anchor, clean=False) is None)
                         or (start == 1 and ast.get_docstring(anchor, clean=False) is not None))):
                assignments = [parameter_assignment(node, anchor) for node in right[begin_new:end_new]]
                if all(assignments):
                    fallback = tuple(decorated_span(left[min(start, len(left) - 1)]))
                    entry = decorated_span(anchor)[0]
                    result.parameter_assignments.extend((entry, name, value, fallback)
                                                        for name, value in assignments)
                    if (len(assignments) == 1 and len(right) == len(left) + 1
                            and not anchor.decorator_list
                            and equivalent(left[:start], right[:begin_new])
                            and equivalent(left[start:], right[end_new:])):
                        name, value = assignments[0]
                        guard = entry_guard(anchor)
                        if guard and guard[0] == name and value in {"true", "false"}:
                            result.parser_entries[(entry, name)] = (guard[2], guard[3])
                    continue
            if tag in {"insert", "replace"}:
                for node in right[begin_new:end_new]:
                    mark(None, node, in_function, owner)
                if in_function:
                    if start < len(left):
                        # The old following statement is reached on the same path.
                        # Use its header even if it is itself a compound statement.
                        result.lines.add(left[start].lineno)
                    if start and (start == len(left) or tag == "replace"):
                        result.lines.update(decorated_span(left[start - 1]))
                    if anchor is not None and not left:
                        # An added else/handler can be reached even when the old
                        # branch body was never visited.
                        result.lines.update(span(anchor))

    def parser_edit(left, right):
        if not (path == "sqlglot/parser.py" or path.startswith("sqlglot/parsers/")):
            return False
        if isinstance(left, ast.If) and isinstance(right, ast.If):
            before, after = left.test, right.test
            old_match, new_match = direct_match(before), direct_match(after)
            same_body = equivalent(left.body, right.body) and equivalent(left.orelse, right.orelse)
            if (same_body and old_match and new_match and old_match[2] == new_match[2]
                    and old_match[1] != new_match[1]):
                header = range(left.lineno, max(left.lineno, left.body[0].lineno - 1) + 1)
                result.parser_guards.append(("token", site(path, old_match[0]),
                                             (old_match[1], new_match[1]), tuple(header)))
                return True
            gate = short_circuit(before)
            if (gate and isinstance(after, ast.BoolOp) and type(before.op) is not type(after.op)
                    and equivalent(before.values, after.values)
                    and same_body):
                header = range(left.lineno, max(left.lineno, left.body[0].lineno - 1) + 1)
                result.parser_guards.append(("short", site(path, before), gate[1], tuple(header)))
                return True
        if isinstance(left, ast.Return) and isinstance(right, ast.Return):
            call = left.value
            if (isinstance(call, ast.Call) and not call.args and not call.keywords
                    and self_attribute(call.func) and (right.value is None
                    or isinstance(right.value, ast.Constant) and right.value.value is None)):
                result.parser_guards.append(("optional", site(path, call), None, tuple(span(left))))
                return True
        return False

    def compare_stmt(left, right, in_function, owner):
        if equivalent(left, right):
            return
        if in_function:
            if parser_edit(left, right):
                return
            comparisons = comparison_changes(left, right)
            if comparisons:
                for node, before, after, bounds, unchanged in comparisons:
                    site = f"{path}:{node.lineno}:{node.col_offset}"
                    result.comparisons.append((site, tuple(span(node)), before, after, bounds, unchanged))
                return
        if type(left) is not type(right):
            mark(left, right, in_function, owner)
            return
        if isinstance(left, (ast.ClassDef, *FUNCTIONS)):
            header_matches = all(equivalent(value, getattr(right, key))
                                 for key, value in ast.iter_fields(left) if key != "body")
            if not header_matches:
                mark(left, right, in_function, owner)
            compare_suite(left.body, right.body,
                          in_function or isinstance(left, FUNCTIONS), left,
                          ".".join(filter(None, (owner, left.name)))
                          if isinstance(left, ast.ClassDef) else owner)
            return
        if not in_function:
            if isinstance(left, (ast.Assign, ast.AnnAssign)) and all(
                equivalent(value, getattr(right, key))
                for key, value in ast.iter_fields(left) if key != "value"
            ):
                deferred = deferred_changes(left.value, right.value)
                if deferred:
                    result.lambdas.update(node.lineno for node in deferred)
                    return
                targets = assigned(left)
                projection = changed_dictionary_values(left.value, right.value)
                if projection is not None and len(targets) == 1:
                    keys, all_values = projection
                    result.dictionary_edits.append(
                        (targets.pop(), keys, all_values, tuple(span(left)), owner)
                    )
                    return
                before, after = scalar_literal(left.value), scalar_literal(right.value)
                if owner and len(targets) == 1 and before is not None and after is not None:
                    if before[0] == after[0] and before[0] != "tuple":
                        result.attribute_edits.append((targets.pop(), tuple(span(left)), owner))
                        return
            mark(left, right, False, owner)
            return
        if isinstance(left, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
            for name, value in ast.iter_fields(left):
                other = getattr(right, name)
                if equivalent(value, other):
                    continue
                if name == "test" and equivalent(truth_expression(value), truth_expression(other)):
                    continue
                if name in {"body", "orelse"}:
                    compare_suite(value, other, True, left, owner)
                else:
                    header_end = max(left.lineno, left.body[0].lineno - 1)
                    result.lines.update(range(left.lineno, header_end + 1))
            return
        # For except and match patterns, using the whole compound statement
        # also catches a change in which arm is entered.
        mark(left, right, True, owner)

    compare_suite(old.body, new.body, False, None)
    return result
