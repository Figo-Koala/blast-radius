"""Map structural source changes to executable lines and import-time effects."""

import ast
from dataclasses import dataclass, field
from difflib import SequenceMatcher


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
    collection: bool = False
    broad: bool = False


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
    try:
        return type(ast.literal_eval(node))
    except (ValueError, TypeError, SyntaxError):
        return None


def changed_dictionary_values(before, after):
    if not (isinstance(before, ast.Dict) and isinstance(after, ast.Dict)):
        return None
    try:
        old, new = ast.literal_eval(before), ast.literal_eval(after)
    except (ValueError, TypeError, SyntaxError):
        return None
    if (list(old) != list(new) or len(old) != len(before.keys)
            or len(new) != len(after.keys) or any(type(key) not in (str, int) for key in old)):
        return None
    return {key for key, left, right in zip(old, before.values, after.values)
            if ast.dump(left) != ast.dump(right)}


def analyze(original, modified, path):
    result = Impact()
    old = ast.parse(original, filename=path, type_comments=True)
    new = ast.parse(modified, filename=path, type_comments=True)
    # AST parsing alone accepts some invalid constructs such as top-level return.
    compile(new, path, "exec")
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
            if (isinstance(left, ast.Compare) and len(left.ops) == len(right.ops) == 1
                    and equivalent(left.left, right.left)
                    and equivalent(left.comparators, right.comparators)):
                before, after = type(left.ops[0]), type(right.ops[0])
                if (before, after) in {(ast.Is, ast.Eq), (ast.IsNot, ast.NotEq)}:
                    return [(left, "identity", "")]
                if before in ORDERING and after in ORDERING:
                    return [(left, before.__name__, after.__name__)]
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

    def compare_stmt(left, right, in_function, owner):
        if equivalent(left, right):
            return
        if in_function:
            comparisons = comparison_changes(left, right)
            if comparisons:
                for node, before, after in comparisons:
                    site = f"{path}:{node.lineno}:{node.col_offset}"
                    result.comparisons.append((site, tuple(span(node)), before, after))
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
                          owner or (left.name if isinstance(left, ast.ClassDef) else None))
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
                keys = changed_dictionary_values(left.value, right.value)
                if keys and len(targets) == 1:
                    result.dictionary_edits.append((targets.pop(), keys, tuple(span(left)), owner))
                    return
            mark(left, right, False, owner)
            return
        if isinstance(left, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
            for name, value in ast.iter_fields(left):
                other = getattr(right, name)
                if equivalent(value, other):
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
    if [item.tag for item in old.type_ignores] != [item.tag for item in new.type_ignores]:
        result.collection = result.broad = True
    return result
