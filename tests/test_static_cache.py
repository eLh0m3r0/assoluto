"""Static asset caching + cache-busting (BE-20 / UX-17)."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.static_assets import IMMUTABLE, REVALIDATE, asset_version


async def test_versioned_assets_are_immutable_and_unversioned_revalidate(settings) -> None:
    app = create_app(settings)
    version = app.state.asset_version
    assert version and version != "0.1.0"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        fresh = await c.get(f"/static/js/app.js?v={version}")
        assert fresh.status_code == 200
        assert fresh.headers["cache-control"] == IMMUTABLE

        bare = await c.get("/static/js/app.js")
        assert bare.headers["cache-control"] == REVALIDATE

        stale = await c.get("/static/js/app.js?v=0.1.0")
        assert stale.headers["cache-control"] == REVALIDATE


async def test_asset_version_prefers_build_id_and_hashes_content(tmp_path) -> None:
    assert asset_version("abc1234 ") == "abc1234"
    assert asset_version("bad/../id") == "bad..id"

    (tmp_path / "a.css").write_text("body{}")
    first = asset_version("", tmp_path)
    (tmp_path / "a.css").write_text("body{color:red}")
    from app.static_assets import _content_hash

    _content_hash.cache_clear()
    second = asset_version("", tmp_path)
    assert first != second


async def test_base_template_uses_asset_version(settings) -> None:
    settings.app_build_id = "build42"
    app = create_app(settings)
    assert app.state.asset_version == "build42"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # No tenant -> the HTML 404 page, which extends base.html.
        resp = await c.get("/app/orders", headers={"accept": "text/html"})
    assert resp.status_code == 404
    assert "app.css?v=build42" in resp.text
