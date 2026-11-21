"""Find configuration dictionaries whose values flow only to explicit lookups."""

import ast


def qualified_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def literal_dict(node):
    try:
        return type(ast.literal_eval(node)) is dict
    except (TypeError, ValueError, SyntaxError):
        return False


def source_index(root):
    records, fields, constructors = [], set(), {}
    for folder in ("sqlglot", "tests", "sqlglotc"):
        for source in sorted((root / folder).rglob("*.py")):
            path = source.relative_to(root).as_posix()
            tree = ast.parse(source.read_bytes(), filename=path)
            parents = {child: parent for parent in ast.walk(tree)
                       for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and path.startswith("sqlglot/"):
                    constructors.setdefault(node.name, []).append((node, path))
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and literal_dict(node.value):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    fields.update(
                        name for target in targets if (name := qualified_name(target))
                        and name.isupper()
                    )
                if (isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load)
                        or isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    records.append((path, node, parents))
    return records, fields, constructors


def condition_only(node, parents):
    parent = parents.get(node)
    if isinstance(parent, ast.BoolOp):
        return condition_only(parent, parents)
    if isinstance(parent, (ast.If, ast.While, ast.IfExp, ast.Assert)):
        return parent.test is node
    if isinstance(parent, ast.UnaryOp):
        return isinstance(parent.op, ast.Not)
    if isinstance(parent, ast.Call):
        return isinstance(parent.func, ast.Name) and parent.func.id in {"bool", "len"}
    return False


def follows_constructor(keyword, parents, path, constructors):
    call = parents.get(keyword)
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        return False
    options = constructors.get(call.func.id, [])
    if len(options) != 1:
        return False
    node, definition = options[0]
    if definition != path:
        # Verify a direct import of that constructor without an alias.
        module = definition[:-3].replace("/", ".")
        top = call
        while top in parents:
            top = parents[top]
        if not isinstance(top, ast.Module) or not any(
            isinstance(stmt, ast.ImportFrom) and stmt.level == 0 and stmt.module == module
            and any(alias.name == node.name and alias.asname is None for alias in stmt.names)
            for stmt in top.body
        ):
            return False
    return any(
        isinstance(statement, ast.FunctionDef) and statement.name == "__init__"
        and any(arg.arg == keyword.arg for arg in
                [*statement.args.posonlyargs, *statement.args.args, *statement.args.kwonlyargs])
        for statement in node.body
    )


def value_sites(root):
    records, fields, constructors = source_index(root)
    by_name = {}
    for path, node, parents in records:
        name = node.value if isinstance(node, ast.Constant) else qualified_name(node)
        by_name.setdefault(name, []).append((path, node, parents))
    result = {}
    for field in sorted(fields):
        aliases = {field, field.lower()}
        sites = []
        for path, node, parents in by_name.get(field, []) + by_name.get(field.lower(), []):
            parent = parents.get(node)
            if isinstance(node, ast.Constant):
                if isinstance(parent, ast.Call):
                    # Reflective access may consume the whole dictionary.
                    break
                continue
            if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is node:
                targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
                if all(qualified_name(target) in aliases for target in targets):
                    continue
            if isinstance(parent, ast.keyword) and parent.arg in aliases:
                if follows_constructor(parent, parents, path, constructors):
                    continue
            if isinstance(parent, ast.Compare) and len(parent.ops) == 1:
                if parent.comparators[0] is node and isinstance(parent.ops[0], (ast.In, ast.NotIn)):
                    continue
            if condition_only(node, parents):
                continue
            if isinstance(parent, ast.Attribute) and parent.value is node and parent.attr == "get":
                call = parents.get(parent)
                if (isinstance(call, ast.Call) and call.func is parent and call.args
                        and not isinstance(call.args[0], ast.Starred) and path.startswith("sqlglot/")):
                    sites.append(f"{path}:{call.lineno}:{call.col_offset}")
                    continue
            # Iteration, a copy, an untracked transfer, or an unknown operation
            # can expose the values through additional paths.
            break
        else:
            if sites:
                result[field] = sorted(set(sites))
    return result
