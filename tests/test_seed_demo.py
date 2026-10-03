"""scripts/seed_demo.py — the sales-demo tenant (market.md §5)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.enums import OrderStatus
from app.models.tenant import Tenant
from scripts.seed_demo import DemoSeedRefused, seed_demo

pytestmark = pytest.mark.postgres


async def _count(conn, table: str, tid) -> int:
    return (
        await conn.execute(text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": tid})
    ).scalar_one()


async def test_seed_demo_is_complete_and_idempotent(owner_engine, wipe_db) -> None:
    first = await seed_demo(slug="demo-test", password="Demo-heslo-1", engine=owner_engine)
    second = await seed_demo(slug="demo-test", password="Demo-heslo-1", engine=owner_engine)
    assert first.tenant_id == second.tenant_id  # same tenant reused, not duplicated

    async with owner_engine.connect() as conn:
        tid = second.tenant_id
        assert (
            await conn.execute(text("SELECT count(*) FROM tenants WHERE slug = 'demo-test'"))
        ).scalar_one() == 1
        assert await _count(conn, "customers", tid) == 6
        assert await _count(conn, "customer_contacts", tid) == second.contacts
        assert await _count(conn, "orders", tid) == second.orders
        assert 20 <= second.orders <= 30
        assert await _count(conn, "order_items", tid) >= second.orders
        assert await _count(conn, "order_comments", tid) > 0
        assert await _count(conn, "products", tid) == second.products
        assert await _count(conn, "assets", tid) == second.assets

        statuses = {
            row[0].lower()
            for row in (
                await conn.execute(
                    text("SELECT status FROM orders WHERE tenant_id = :t"), {"t": tid}
                )
            ).all()
        }
        assert statuses == {s.value for s in OrderStatus}

        # Every order's status history ends in its current status.
        mismatched = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM orders o WHERE o.tenant_id = :t AND o.status <> ("
                    " SELECT h.to_status FROM order_status_history h WHERE h.order_id = o.id"
                    " ORDER BY h.created_at DESC LIMIT 1)"
                ),
                {"t": tid},
            )
        ).scalar_one()
        assert mismatched == 0

        # Stock = sum of movements.
        drift = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM assets a WHERE a.tenant_id = :t AND a.current_quantity <> ("
                    " SELECT coalesce(sum(m.quantity), 0) FROM asset_movements m"
                    " WHERE m.asset_id = a.id)"
                ),
                {"t": tid},
            )
        ).scalar_one()
        assert drift == 0

        # Only reserved documentation domains — nothing that could be a real mailbox.
        emails = [
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT email FROM users WHERE tenant_id = :t "
                        "UNION ALL SELECT email FROM customer_contacts WHERE tenant_id = :t"
                    ),
                    {"t": tid},
                )
            ).all()
        ]
        assert emails and all(e.endswith(".example.com") for e in emails)
        assert (
            await conn.execute(
                text("SELECT count(*) FROM customers WHERE tenant_id = :t AND ico IS NOT NULL"),
                {"t": tid},
            )
        ).scalar_one() == 0


async def test_seed_demo_logins_work(owner_engine, wipe_db, settings) -> None:
    from httpx import ASGITransport

    from app.main import create_app
    from tests.conftest import CsrfAwareClient

    result = await seed_demo(slug="demo-login", password="Demo-heslo-1", engine=owner_engine)
    app = create_app(settings)
    async with CsrfAwareClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Tenant-Slug": "demo-login"},
    ) as client:
        for email in (result.staff_email, result.contact_email):
            client.cookies.clear()
            resp = await client.post(
                "/auth/login",
                data={"email": email, "password": "Demo-heslo-1"},
                follow_redirects=False,
            )
            assert resp.status_code == 303, (email, resp.text[:300])
            assert (await client.get("/app/orders")).status_code == 200


async def test_seed_demo_refuses_foreign_tenant(owner_engine, wipe_db) -> None:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            Tenant(
                id=uuid4(),
                slug="real-shop",
                name="Real",
                billing_email="b@real.example.com",
                storage_prefix="tenants/real-shop/",
            )
        )
    with pytest.raises(DemoSeedRefused):
        await seed_demo(slug="real-shop", engine=owner_engine)
