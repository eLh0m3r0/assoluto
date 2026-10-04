"""Audit 2026-10-03 LOGIC-1 / D4: money and quantity inputs.

A customer contact could take the supplier's whole order list down with
one ``unit_price=NaN`` POST: ``Decimal("NaN")`` was stored, the cached
total became NaN and ``money_major`` crashed on ``int(NaN)``.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.order import Order, OrderItem
from app.services.money import (
    MONEY_MAX,
    AmountError,
    check_money,
    line_total,
    parse_money,
    parse_quantity,
)
from app.templating import _money_filter, _money_major_filter
from tests.test_orders_item_autosave import _create_draft_with_item, _login, _logout, _seed

# --------------------------------------------------------------- pure unit


@pytest.mark.parametrize("raw", ["nan", "NaN", "Infinity", "-Infinity", "inf", "abc", "1e"])
def test_parse_money_rejects_non_finite_and_garbage(raw: str) -> None:
    with pytest.raises(AmountError) as exc:
        parse_money(raw)
    assert exc.value.reason == "invalid"


def test_parse_money_rejects_negative_and_out_of_range() -> None:
    with pytest.raises(AmountError) as neg:
        parse_money("-500")
    assert neg.value.reason == "negative"
    with pytest.raises(AmountError) as big:
        parse_money("10000000000")
    assert big.value.reason == "too_large"


def test_parse_money_accepts_decimal_comma_and_blank() -> None:
    assert parse_money("12,5") == Decimal("12.50")
    assert parse_money("  ") is None
    assert parse_money("0") == Decimal("0.00")


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "Infinity", "", "1000000000"])
def test_parse_quantity_rejects(raw: str) -> None:
    with pytest.raises(AmountError):
        parse_quantity(raw)


def test_line_total_rounds_half_up_and_checks_range() -> None:
    # 0.5 x 0.25 = 0.125 -> 0.13 (half-up), not 0.12 (banker's).
    assert line_total(Decimal("0.5"), Decimal("0.25")) == Decimal("0.13")
    with pytest.raises(AmountError):
        line_total(Decimal("999999999"), MONEY_MAX)
    assert check_money(Decimal("1.005")) == Decimal("1.01")


def test_money_filters_never_crash_on_non_finite() -> None:
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), "nan"):
        assert _money_major_filter(bad, "CZK") == "—"
        assert _money_filter(bad, "CZK") == "—"
    assert _money_major_filter(Decimal("2050.50"), "CZK") == "2 050,50 Kč"


# ------------------------------------------------------------- integration

pg = pytest.mark.postgres


@pg
async def test_contact_nan_price_is_refused_and_order_list_stays_up(
    tenant_client, owner_engine, demo_tenant
) -> None:
    """The exact reproduction from the audit."""
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    order_id, _item = await _create_draft_with_item(
        tenant_client, owner_engine, customer_id=seed["acme"].id
    )

    resp = await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={"description": "Poison", "quantity": "1", "unit_price": "nan"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        descriptions = (
            (
                await session.execute(
                    select(OrderItem.description).where(OrderItem.order_id == order_id)
                )
            )
            .scalars()
            .all()
        )
    assert "Poison" not in descriptions

    await _logout(tenant_client)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    assert (await tenant_client.get("/app/orders")).status_code == 200
    assert (await tenant_client.get(f"/app/orders/{order_id}")).status_code == 200


@pg
async def test_pages_survive_a_nan_row_already_in_the_database(
    tenant_client, owner_engine, demo_tenant
) -> None:
    """Defence in depth: rows poisoned before the fix must not 500."""
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id, item_id = await _create_draft_with_item(
        tenant_client, owner_engine, customer_id=seed["acme"].id
    )
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE order_items SET unit_price='NaN', line_total='NaN' WHERE id=:id"),
            {"id": item_id},
        )
        await conn.execute(
            text("UPDATE orders SET quoted_total='NaN' WHERE id=:id"), {"id": order_id}
        )

    assert (await tenant_client.get("/app/orders")).status_code == 200
    assert (await tenant_client.get(f"/app/orders/{order_id}")).status_code == 200


@pg
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unit_price", "-500"),
        ("unit_price", "Infinity"),
        ("quantity", "Infinity"),
        ("quantity", "-2"),
        ("unit_price", "99999999999"),
    ],
)
async def test_staff_add_item_rejects_bad_amounts_with_a_flash(
    tenant_client, owner_engine, demo_tenant, field: str, value: str
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id, _ = await _create_draft_with_item(
        tenant_client, owner_engine, customer_id=seed["acme"].id
    )
    data = {"description": "Bad", "quantity": "1", "unit_price": "10", field: value}
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/items", data=data, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
    # Only the seeded 3 x 100 line counts.
    assert order.quoted_total == Decimal("300.00")


@pg
async def test_line_whose_total_overflows_the_column_is_refused(
    tenant_client, owner_engine, demo_tenant
) -> None:
    """Each factor fits its column, the product does not."""
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id, _ = await _create_draft_with_item(
        tenant_client, owner_engine, customer_id=seed["acme"].id
    )
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/items",
        data={"description": "Huge", "quantity": "999999999", "unit_price": "9999999"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert (await tenant_client.get(f"/app/orders/{order_id}")).status_code == 200


@pg
@pytest.mark.parametrize("value", ["nan", "-5", "Infinity"])
async def test_autosave_rejects_bad_price_in_the_row(
    tenant_client, owner_engine, demo_tenant, value: str
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    order_id, item_id = await _create_draft_with_item(
        tenant_client, owner_engine, customer_id=seed["acme"].id
    )
    resp = await tenant_client.post(
        f"/app/orders/{order_id}/items/{item_id}/patch",
        data={"unit_price": value},
    )
    assert resp.status_code == 200
    assert 'data-row-error="1"' in resp.text

    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session:
        item = (
            await session.execute(select(OrderItem).where(OrderItem.id == item_id))
        ).scalar_one()
    assert item.unit_price == Decimal("100.00")


@pg
async def test_add_item_service_refuses_nan_even_without_the_router(
    owner_engine, demo_tenant
) -> None:
    from app.services.order_service import ActorRef, InvalidAmount, add_item
    from tests.test_audit_2026_07_26_orders import _seed as seed_order

    user, _cust, _contact, order = await seed_order(owner_engine, demo_tenant.id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        fresh = (await session.execute(select(Order).where(Order.id == order.id))).scalar_one()
        with pytest.raises(InvalidAmount):
            await add_item(
                session,
                tenant_id=demo_tenant.id,
                order=fresh,
                actor=ActorRef(type="user", id=user.id),
                description="x",
                quantity=Decimal("1"),
                unit_price=Decimal("NaN"),
            )


@pg
async def test_product_form_rejects_nan_default_price(
    tenant_client, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    resp = await tenant_client.post(
        "/app/products",
        data={"sku": f"N-{uuid4().hex[:4]}", "name": "Nan", "unit": "ks", "default_price": "NaN"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    async with owner_engine.connect() as conn:
        count = (
            await conn.execute(text("SELECT count(*) FROM products WHERE name='Nan'"))
        ).scalar()
    assert count == 0
