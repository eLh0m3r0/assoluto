"""Sales / public demo tenant (CEO decision E3).

* :mod:`app.demo.seed` — creates (or re-creates) the fictional CNC job
  shop "CNC Dílna Vzorová s.r.o." with clients, orders, drawings and
  material. ``python -m app.demo.seed`` inside the container;
  ``scripts/seed_demo.py`` is a thin wrapper kept for existing docs.
* :mod:`app.demo.guard` — the public-demo guard, active only for the
  tenant named by ``PUBLIC_DEMO_TENANT``: no e-mail, no account /
  invitation / settings changes, capped uploads, banner, ``noindex``.
* :mod:`app.demo.router` — ``GET /demo`` + ``POST /demo/enter``: enter
  the demo as the supplier or as the customer without signing up.

Core code: nothing here imports :mod:`app.platform` (CLAUDE.md §6).
"""

#: ``tenants.settings`` key stamped by the seed. Only a tenant carrying it
#: may be wiped by a re-seed or served as the public demo.
DEMO_MARKER = "demo_seed"
