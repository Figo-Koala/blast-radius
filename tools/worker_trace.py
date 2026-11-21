"""Carry the originating test ID into real multiprocessing workers."""

from concurrent.futures import ProcessPoolExecutor
from multiprocessing.util import Finalize
import os
from pathlib import Path


_profiler = None


def execute(root, files, test, fn, args, kwargs):
    global _profiler
    if _profiler is None:
        from build_index import Profiler

        _profiler = Profiler(Path(root), {})
        _profiler.test = test
        _profiler.start()
        Finalize(None, flush, (Path(files),), exitpriority=0)
    elif _profiler.test != test:
        _profiler.test = test
        _profiler.monitor.restart_events()
    return fn(*args, **kwargs)


def flush(files):
    _profiler.monitor.free_tool_id(_profiler.tool)
    _profiler.dump(files / f"{os.getpid()}.json.gz")


def install(profiler, root, files):
    submit = ProcessPoolExecutor.submit

    def submit_with_trace(pool, fn, /, *args, **kwargs):
        return submit(pool, execute, str(root), str(files), profiler.test, fn, args, kwargs)

    ProcessPoolExecutor.submit = submit_with_trace
