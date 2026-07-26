"""The order-detail status stepper — router view-model + rendered HTML.

The stepper replaced a flat row of transition buttons whose labels came
from the *target status alone*. From CONFIRMED that rendered a blue
button reading "Quote" for what was actually a step **backwards** —
indistinguishable from progress. These tests pin the properties that
made it confusing so a future refactor cannot quietly reintroduce them:

* every pipeline step is always shown, so position is readable at a
  glance rather than inferred from which buttons happen to exist;
* staff can post a multi-step jump and it lands;
* a jump or a step back carries a confirmation, an adjacent step
  forward does not (freedom must not become accidental);
* contacts see the same picture but get no controls they may not use.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus, UserRole
from app.models.order import Order
from app.models.user import User
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres


async def _seed(owner_engine, tenant_id: UUID, *, status: OrderStatus) -> dict:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        staff = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="staff@4mex.cz",
            full_name="Staff User",
            role=UserRole.TENANT_ADMIN,
            password_hash=hash_password("staffpass"),
        )
        customer = Customer(id=uuid4(), tenant_id=tenant_id, name="ACME")
        session.add_all([staff, customer])
        await session.flush()

        contact = CustomerContact(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=customer.id,
            email="jan@acme.cz",
            full_name="Jan Novák",
            role=CustomerContactRole.CUSTOMER_ADMIN,
            password_hash=hash_password("contactpass"),
            invited_at=datetime.now(),
            accepted_at=datetime.now(),
        )
        order = Order(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=customer.id,
            number=f"2026-ST-{uuid4().hex[:4]}",
            title="Stepper test",
            status=status,
        )
        session.add_all([contact, order])
        await session.flush()
        return {"staff": staff, "customer": customer, "contact": contact, "order": order}


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/auth/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text


async def _order_row(owner_engine, order_id) -> Order:
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        return (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()


# ------------------------------------------------------------ rendered page


async def test_stepper_shows_every_pipeline_step(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """All eight pipeline steps render regardless of the current one."""
    seed = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.CONFIRMED)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    resp = await tenant_client.get(f"/app/orders/{seed['order'].id}")
    assert resp.status_code == 200
    body = resp.text

    pipeline = ("draft", "submitted", "quoted", "in_production", "ready", "delivered", "closed")
    for status in pipeline:
        assert f"/transitions/{status}" in body, f"{status} is not reachable from the stepper"

    # The current step renders as a marker, never as a move to itself —
    # ``transition_order`` would reject it as "already in that status".
    assert "/transitions/confirmed" not in body


async def test_backward_and_skipping_moves_are_confirmed(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """``data-confirm`` guards the moves a user would not expect, and
    stays off the obvious next step so the happy path is one click."""
    seed = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.CONFIRMED)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    body = (await tenant_client.get(f"/app/orders/{seed['order'].id}")).text

    def _form_for(status: str) -> str:
        marker = f"/transitions/{status}"
        start = body.index(marker)
        return body[body.rindex("<form", 0, start) : body.index("</form>", start)]

    # Adjacent forward step — no friction.
    assert "data-confirm" not in _form_for("in_production")
    # Backward step — confirm.
    assert "data-confirm" in _form_for("quoted")
    # Multi-step jump — confirm, and it names what gets skipped. The
    # test client renders the tenant default locale (cs), so accept
    # either wording rather than pinning the catalog.
    ready_form = _form_for("ready")
    assert "data-confirm" in ready_form
    assert "Ve výrobě" in ready_form or "In production" in ready_form
    # Cancel — always confirm.
    assert "data-confirm" in _form_for("cancelled")


async def test_contact_sees_pipeline_but_not_staff_controls(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """A customer gets the progress picture without operator powers."""
    seed = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.IN_PRODUCTION)
    await _login(tenant_client, "jan@acme.cz", "contactpass")

    resp = await tenant_client.get(f"/app/orders/{seed['order'].id}")
    assert resp.status_code == 200
    body = resp.text

    # The pipeline itself is visible (status names render as node labels).
    assert "Ve výrobě" in body or "In production" in body
    # …but a contact in IN_PRODUCTION may not move the order anywhere.
    assert "/transitions/" not in body


# ------------------------------------------------------------ posting jumps


async def test_staff_can_post_a_multi_step_jump(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """The headline behaviour, end to end through the HTTP layer."""
    seed = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.DRAFT)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    resp = await tenant_client.post(
        f"/app/orders/{seed['order'].id}/transitions/confirmed", follow_redirects=False
    )
    assert resp.status_code == 303, resp.text

    order = await _order_row(owner_engine, seed["order"].id)
    assert order.status == OrderStatus.CONFIRMED
    assert order.submitted_at is not None, "the skipped SUBMITTED stamp must be backfilled"


async def test_flash_names_the_status_not_the_action(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    """Regression: the flash used to read the transition *verb*, giving
    "Status changed to Start production." instead of "… to In production."."""
    from urllib.parse import unquote

    seed = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.CONFIRMED)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    resp = await tenant_client.post(
        f"/app/orders/{seed['order'].id}/transitions/in_production", follow_redirects=False
    )
    assert resp.status_code == 303
    notice = unquote(resp.headers["location"])
    assert "Ve výrobě" in notice or "In production" in notice
    assert "Zahájit" not in notice and "Start production" not in notice


async def test_cancelled_order_offers_reopen(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.CANCELLED)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")

    body = (await tenant_client.get(f"/app/orders/{seed['order'].id}")).text
    assert "/transitions/draft" in body
    # A cancelled order can go straight back to any pipeline step.
    assert "/transitions/in_production" in body
