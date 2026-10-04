"""Uploads must not block the event loop or buffer the whole body (T9).

Audit BE-03 / SEC-3 / BE-17 / F-10 / F-41:

* every boto3 call made from the upload handler and the thumbnail task
  runs in a worker thread, never on the event-loop thread;
* the thumbnail task holds no pooled DB connection while it talks to S3;
* the handler never calls ``UploadFile.read()`` (the 50 MB-in-RAM bug);
* bodies far above ``MAX_UPLOAD_SIZE_MB`` are refused while streaming,
  before multipart parsing;
* deleting an attachment removes the row first, so a failing S3 delete
  can't leave a row pointing at nothing.
"""

from __future__ import annotations

import threading
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.models.attachment import OrderAttachment
from app.security.body_limit import BodySizeLimitMiddleware
from tests import test_attachments_flow as _af
from tests.test_attachments_flow import _login, _png_bytes, _seed_everyone

# Re-use the moto fixtures from the attachment flow tests.
mock_s3 = _af.mock_s3
_point_settings_at_moto = _af._point_settings_at_moto

# ---------------------------------------------------------------- body limit


def _echo_app(limit: int) -> Starlette:
    async def echo(request: Request) -> PlainTextResponse:
        body = await request.body()
        return PlainTextResponse(str(len(body)))

    app = Starlette(routes=[Route("/up", echo, methods=["POST"])])
    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=limit)
    return app


async def test_body_limit_rejects_declared_oversize_without_reading() -> None:
    reached = False

    async def app(scope, receive, send):  # pragma: no cover - must not run
        nonlocal reached
        reached = True

    mw = BodySizeLimitMiddleware(app, max_body_bytes=10)
    sent: list[dict] = []

    async def receive():
        raise AssertionError("body must not be read")

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/up",
        "headers": [(b"content-length", b"11")],
    }
    await mw(scope, receive, send)
    assert not reached
    assert sent[0]["status"] == 413


async def test_body_limit_aborts_streamed_body_over_limit() -> None:
    transport = ASGITransport(app=_echo_app(limit=1000))
    async with AsyncClient(transport=transport, base_url="http://t") as c:

        async def chunks():
            for _ in range(5):
                yield b"x" * 400

        resp = await c.post("/up", content=chunks())
        assert resp.status_code == 413

        ok = await c.post("/up", content=b"y" * 999)
        assert ok.status_code == 200
        assert ok.text == "999"


async def test_app_refuses_huge_declared_upload(client: AsyncClient, settings) -> None:
    too_big = settings.max_upload_size_bytes + 5 * 1024 * 1024
    resp = await client.post(
        "/app/orders/00000000-0000-0000-0000-000000000000/attachments",
        content=b"",
        headers={"content-length": str(too_big), "content-type": "multipart/form-data; b=x"},
    )
    assert resp.status_code == 413


# ---------------------------------------------------------------- handler


async def _create_order(client: AsyncClient) -> UUID:
    resp = await client.post("/app/orders", data={"title": "Offload"}, follow_redirects=False)
    return UUID(resp.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])


@pytest.mark.postgres
async def test_upload_and_thumbnail_s3_io_runs_off_the_event_loop(
    tenant_client: AsyncClient, owner_engine, demo_tenant, mock_s3, monkeypatch
) -> None:
    from starlette.datastructures import UploadFile

    from app.db.session import get_engine
    from app.storage import s3 as s3_mod

    await _seed_everyone(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    order_id = await _create_order(tenant_client)

    loop_thread = threading.get_ident()
    calls: list[tuple[str, int, int]] = []

    real_upload_fileobj = s3_mod.upload_fileobj
    real_upload_bytes = s3_mod.upload_bytes
    real_download = s3_mod.download_bytes

    def spy_upload_fileobj(*a, **kw):
        calls.append(("upload_fileobj", threading.get_ident(), -1))
        return real_upload_fileobj(*a, **kw)

    def spy_upload_bytes(*a, **kw):
        calls.append(("upload_bytes", threading.get_ident(), -1))
        return real_upload_bytes(*a, **kw)

    def spy_download(*a, **kw):
        # A pooled app connection checked out here would mean the
        # thumbnail task holds a transaction across S3 I/O.
        calls.append(("download", threading.get_ident(), get_engine().pool.checkedout()))
        return real_download(*a, **kw)

    async def forbidden_read(self, *a, **kw):
        raise AssertionError("upload handler must not read the whole body into memory")

    monkeypatch.setattr(s3_mod, "upload_fileobj", spy_upload_fileobj)
    monkeypatch.setattr(s3_mod, "upload_bytes", spy_upload_bytes)
    monkeypatch.setattr(s3_mod, "download_bytes", spy_download)
    monkeypatch.setattr(UploadFile, "read", forbidden_read)

    png = _png_bytes()
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/attachments",
        files={"file": ("d.png", png, "image/png")},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    names = [c[0] for c in calls]
    assert "upload_fileobj" in names  # the original
    assert "download" in names  # thumbnail task fetched it
    assert "upload_bytes" in names  # thumbnail stored
    for name, thread_id, _ in calls:
        assert thread_id != loop_thread, f"{name} ran on the event loop"
    download_checked_out = next(c[2] for c in calls if c[0] == "download")
    assert download_checked_out == 0

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        att = (
            await session.execute(
                select(OrderAttachment).where(OrderAttachment.order_id == order_id)
            )
        ).scalar_one()
    assert att.size_bytes == len(png)
    assert att.thumbnail_key is not None
    stored = s3_mod.download_bytes(att.storage_key)
    assert stored == png


@pytest.mark.postgres
async def test_upload_s3_failure_leaves_no_row(
    tenant_client: AsyncClient, owner_engine, demo_tenant, mock_s3, monkeypatch
) -> None:
    from app.storage import s3 as s3_mod

    await _seed_everyone(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    order_id = await _create_order(tenant_client)

    def boom(*a, **kw):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(s3_mod, "upload_fileobj", boom)
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/attachments",
        files={"file": ("d.png", _png_bytes(), "image/png")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        rows = (
            (
                await session.execute(
                    select(OrderAttachment).where(OrderAttachment.order_id == order_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


@pytest.mark.postgres
async def test_delete_removes_row_even_when_s3_delete_fails(
    tenant_client: AsyncClient, owner_engine, demo_tenant, mock_s3, monkeypatch
) -> None:
    from app.storage import s3 as s3_mod

    await _seed_everyone(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    order_id = await _create_order(tenant_client)
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/attachments",
        files={"file": ("d.png", _png_bytes(), "image/png")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        att = (
            await session.execute(
                select(OrderAttachment).where(OrderAttachment.order_id == order_id)
            )
        ).scalar_one()

    def boom(*a, **kw):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(s3_mod, "delete_objects", boom)
    resp = await tenant_client.post(f"/app/attachments/{att.id}/delete", follow_redirects=False)
    assert resp.status_code == 303

    async with sm() as session:
        gone = (
            await session.execute(select(OrderAttachment).where(OrderAttachment.id == att.id))
        ).scalar_one_or_none()
    assert gone is None
