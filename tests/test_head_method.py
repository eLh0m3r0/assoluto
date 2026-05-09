"""HEAD-from-GET auto-derivation middleware (F-UX-017).

Asserts that every public GET endpoint also responds to HEAD with the
same status + an empty body, instead of 405 Method Not Allowed.
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "path",
    [
        "/healthz",
        "/readyz",
        "/sitemap.xml",
        "/robots.txt",
        "/",
        "/pricing",
        "/features",
        "/terms",
        "/contact",
        "/platform/login",
        "/platform/signup",
    ],
)
async def test_head_returns_same_status_as_get(client, path: str) -> None:
    """HEAD must return the same 2xx/3xx as GET, with an empty body."""
    get = await client.get(path, follow_redirects=False)
    head = await client.head(path, follow_redirects=False)
    assert head.status_code == get.status_code, (
        f"{path}: HEAD={head.status_code} GET={get.status_code}"
    )
    assert head.content == b"" or len(head.content) == 0
