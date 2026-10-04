"""Every scheduled job takes a Postgres advisory lock so only one worker
runs it. Two jobs sharing an id silently skip each other: during the
2026-10 integration the weekly summary and the quote reminder were both
given 42_101 by teams working in parallel."""

import importlib
import pkgutil

import app.tasks


def test_scheduler_lock_ids_are_unique():
    seen: dict[int, str] = {}
    for mod_info in pkgutil.iter_modules(app.tasks.__path__):
        module = importlib.import_module(f"app.tasks.{mod_info.name}")
        for name, value in vars(module).items():
            if name.endswith("_LOCK_ID") and isinstance(value, int):
                where = f"{mod_info.name}.{name}"
                assert value not in seen, f"{where} reuses lock id {value} of {seen[value]}"
                seen[value] = where
    assert len(seen) >= 8
