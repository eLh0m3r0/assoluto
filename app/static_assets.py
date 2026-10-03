"""Static assets: cache-busting version + long-lived caching (BE-20 / UX-17).

Templates used to append ``?v={{ app_version }}`` — the package version,
which has been ``0.1.0`` forever — and ``/static`` sent no
``Cache-Control`` at all. So browsers revalidated every asset on every
page view, *and* could keep serving stale CSS/JS after a deploy.

Now:

* :func:`asset_version` is ``APP_BUILD_ID`` when the build sets it, else
  a SHA-256 over the static directory's contents (computed once per
  process), so any change to CSS/JS yields a new URL.
* :class:`CachedStaticFiles` marks a response ``immutable`` for a year
  only when the request carries the *current* version (``?v=<version>``);
  unversioned or stale-versioned requests get ``no-cache`` (revalidate),
  so a wrong file can never be pinned for a year.

HTML is unaffected: authenticated pages keep ``no-store`` from
:class:`app.security.headers.SecurityHeadersMiddleware`.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

STATIC_DIR = Path(__file__).resolve().parent / "static"

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


@lru_cache(maxsize=4)
def _content_hash(directory: str) -> str:
    digest = hashlib.sha256()
    root = Path(directory)
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def asset_version(build_id: str | None = None, directory: Path = STATIC_DIR) -> str:
    """Cache-busting token for ``/static`` URLs."""
    cleaned = _SAFE.sub("", (build_id or "").strip())[:40]
    return cleaned or _content_hash(str(directory))


class CachedStaticFiles(StaticFiles):
    """``StaticFiles`` with explicit ``Cache-Control``."""

    def __init__(self, *args, version: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.version = version

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            versioned = query.get("v", [None])[0] == self.version
            response.headers["Cache-Control"] = IMMUTABLE if versioned else REVALIDATE
        return response
