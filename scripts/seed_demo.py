"""Thin CLI wrapper — the demo seed now lives in :mod:`app.demo.seed`.

Kept so existing docs and habits keep working::

    python -m scripts.seed_demo --slug ukazka --password '<pick-one>'

is the same as ``python -m app.demo.seed …`` (which is what the nightly
public-demo reset runs inside the app). See that module for options.
"""

from __future__ import annotations

from app.demo.seed import (  # noqa: F401 — re-exported for old imports
    DEMO_MARKER,
    TENANT_NAME,
    DemoSeedRefused,
    SeedResult,
    main,
    seed_demo,
)

if __name__ == "__main__":
    main()
