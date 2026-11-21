"""Record resolved imports, including imports that hit Python's module cache."""

import builtins
import importlib
import importlib.util
import sys
from types import ModuleType


def install(profiler):
    original_import = builtins.__import__
    original_module = importlib.import_module

    def record(origin, scope, module):
        if not isinstance(module, ModuleType):
            return
        target = profiler.path(module.__dict__.get("__file__"))
        if not target or target == origin:
            return
        if not target.startswith(("sqlglot/", "tests/", "sqlglotc/")):
            return
        spec = scope.get("__spec__")
        if profiler.test <= 1 or getattr(spec, "_initializing", False):
            profiler.import_edges.setdefault(origin, set()).add(target)
        if profiler.test > 1:
            profiler.import_hits[target] = profiler.import_hits.get(target, 0) | profiler.test

    def traced_import(name, globals=None, locals=None, fromlist=(), level=0):
        result = original_import(name, globals, locals, fromlist, level)
        origin = profiler.path(globals.get("__file__")) if isinstance(globals, dict) else None
        if origin and origin.startswith(("sqlglot/", "tests/", "sqlglotc/")):
            if level:
                package = globals.get("__package__")
                if not package:
                    package = globals["__name__"]
                    if "__path__" not in globals:
                        package = package.rpartition(".")[0]
                name = importlib.util.resolve_name("." * level + name, package)
            module = sys.modules.get(name, result)
            record(origin, globals, module)
            for member in fromlist or ():
                if type(member) is str:
                    submodule = sys.modules.get(f"{name}.{member}")
                    if submodule is None and isinstance(module, ModuleType):
                        submodule = module.__dict__.get(member)
                    record(origin, globals, submodule)
        return result

    def traced_module(name, package=None):
        result = original_module(name, package)
        scope = sys._getframe(1).f_globals
        origin = profiler.path(scope.get("__file__"))
        if origin and origin.startswith(("sqlglot/", "tests/", "sqlglotc/")):
            record(origin, scope, result)
        return result

    builtins.__import__ = traced_import
    importlib.import_module = traced_module
