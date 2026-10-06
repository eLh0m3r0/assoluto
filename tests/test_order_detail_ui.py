"""Order detail, orders list, material page and activity feed — UI polish
from the public-demo review (2026-10-06).

* P2-1  read-only unit prices are money, not "385.00";
* P2-6  the customer never sees supplier-only stepper hints;
* P2-7  the orders list carries status, total and due date on a phone;
        the add-item form cannot stretch the page;
* P2-13 material movements name the order they belong to;
* P3-6  "Price per unit" header, "m2" renders as "m²";
* P3-7  attachments show "PDF", not "application/pdf";
* P3-8  "uploaded a file to <order number>";
* P3-17 no "v0.1.0" in the footer.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.asset import Asset, AssetMovement
from app.models.attachment import OrderAttachment
from app.models.audit_event import AuditEvent
from app.models.customer import Customer, CustomerContact
from app.models.enums import (
    AssetMovementType,
    AttachmentKind,
    CustomerContactRole,
    OrderStatus,
    UserRole,
)
from app.models.order import Order, OrderItem
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
            number="2026-UI-0001",
            title="Gearbox covers",
            status=status,
            quoted_total=Decimal("12345.50"),
            promised_delivery_at=datetime(2026, 11, 20).date(),
        )
        session.add_all([contact, order])
        await session.flush()
        items = [
            OrderItem(
                id=uuid4(),
                tenant_id=tenant_id,
                order_id=order.id,
                position=1,
                description="Cover K-07",
                quantity=Decimal("40"),
                unit="ks",
                unit_price=Decimal("1240.50"),
                line_total=Decimal("49620.00"),
            ),
            OrderItem(
                id=uuid4(),
                tenant_id=tenant_id,
                order_id=order.id,
                position=2,
                description="Powder coating",
                quantity=Decimal("18"),
                unit="m2",
                unit_price=Decimal("260"),
                line_total=Decimal("4680.00"),
            ),
        ]
        session.add_all(items)
        return {"staff": staff, "customer": customer, "contact": contact, "order": order}


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/auth/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text


def _t_any(msgid: str) -> tuple[str, ...]:
    from app.i18n import gettext

    return tuple({gettext(loc, msgid) for loc in ("en", "cs", "de")})


# ------------------------------------------------------------- items table


async def test_read_only_unit_price_is_money_and_units_are_pretty(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.CONFIRMED)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    body = (await tenant_client.get(f"/app/orders/{seed['order'].id}")).text

    prices = re.findall(r"data-unit-price>([^<]+)<", body)
    assert prices == ["1 240,50 Kč", "260 Kč"]
    assert "1240.50" not in body
    assert "m²" in body
    assert re.search(r"18 m2\b", body) is None
    assert any(t in body for t in _t_any("Price per unit"))


async def test_editable_price_input_keeps_the_raw_number(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.QUOTED)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    body = (await tenant_client.get(f"/app/orders/{seed['order'].id}")).text
    assert 'value="1240.50"' in body
    assert "data-unit-price" not in body  # every row is editable here
    # The add-item grid is one column below sm (P2-7: no page overflow).
    assert 'id="add-item-form"\n                  class="grid grid-cols-1' in body


async def test_unit_macro_maps_only_known_units() -> None:
    from app.templating import build_jinja_env

    env = build_jinja_env()
    tpl = env.from_string('{% from "_units.html" import unit %}{{ unit(u) }}')
    assert tpl.render(u="m2") == "m²"
    assert tpl.render(u="M3") == "m³"
    assert tpl.render(u="ks") == "ks"
    assert tpl.render(u="hod") == "hod"
    assert tpl.render(u=None) == ""


# ------------------------------------------------- staff-only stepper hints


async def test_customer_does_not_get_the_staff_stepper_hint(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.QUOTED)
    url = f"/app/orders/{seed['order'].id}"

    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    staff_body = (await tenant_client.get(url)).text
    assert "data-staff-hint" in staff_body

    tenant_client.cookies.clear()
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    contact_body = (await tenant_client.get(url)).text
    # The customer can confirm (so the pipeline has an action) — but the
    # "click any step" line is about the staff stepper.
    assert "/transitions/confirmed" in contact_body
    assert "data-staff-hint" not in contact_body
    # The "this is where you confirm" card belongs to the public demo only.
    assert "data-demo-quote-hint" not in contact_body
    assert not any(t in contact_body for t in _t_any("Click any step to move the order there."))


async def test_cancelled_order_reads_neutral_for_the_customer(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.CANCELLED)
    url = f"/app/orders/{seed['order'].id}"
    reopen = _t_any("This order is cancelled. Pick a step below to reopen it.")

    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    staff_body = (await tenant_client.get(url)).text
    assert any(t in staff_body for t in reopen)

    tenant_client.cookies.clear()
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    body = (await tenant_client.get(url)).text
    assert "data-cancelled-note" in body
    assert any(t in body for t in _t_any("This order was cancelled."))
    assert not any(t in body for t in reopen)


# --------------------------------------------------------------- attachments


async def test_attachment_row_shows_a_file_type_and_a_large_thumbnail(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.QUOTED)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        for name, ct, thumb in (
            ("K-07_cover.pdf", "application/pdf", "thumbs/k07.jpg"),
            ("photo.png", "image/png", None),
            ("model.stp", "application/octet-stream", None),
        ):
            session.add(
                OrderAttachment(
                    id=uuid4(),
                    tenant_id=demo_tenant.id,
                    order_id=seed["order"].id,
                    kind=AttachmentKind.DRAWING,
                    filename=name,
                    content_type=ct,
                    size_bytes=47_300,
                    storage_key=f"x/{name}",
                    thumbnail_key=thumb,
                )
            )
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    body = (await tenant_client.get(f"/app/orders/{seed['order'].id}")).text

    assert "application/pdf" not in body
    # N7: the size is localised (Czech page: decimal comma, "kB").
    sizes = body.replace(chr(0xA0), " ")
    assert "KB" not in sizes
    assert "PDF · 46,2 kB" in sizes
    assert "PNG · 46,2 kB" in sizes
    assert "STP · 46,2 kB" in sizes  # unknown type: the extension
    # 96 px thumbnail that opens the file.
    thumb = re.search(r'<a href="(/app/attachments/[^"]+/download)"[^>]*>\s*<img', body)
    assert thumb is not None
    assert "h-24 w-24" in body


# --------------------------------------------------------------- orders list


async def test_orders_list_rows_carry_status_total_and_due_for_phones(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id, status=OrderStatus.CONFIRMED)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    body = (await tenant_client.get("/app/orders")).text

    card = body[body.index("data-order-card") :]
    card = card[: card.index("</td>")]
    assert "2026-UI-0001" in card
    assert "bg-violet-100" in card  # the "Confirmed" status badge
    assert re.search(r"data-card-total>12 345,50 Kč<", body)
    assert re.search(r"data-card-due>\s*\S+: 20\.11\.2026", body)
    # The wide columns are desktop-only.
    assert 'class="hidden px-4 py-3 text-left md:table-cell"' in body


# ------------------------------------------------------------------ material


async def test_material_movements_link_their_order(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.CONFIRMED)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        asset = Asset(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            customer_id=seed["customer"].id,
            code="PL-3",
            name="Sheet 3 mm",
            unit="m2",
            current_quantity=Decimal("12"),
        )
        session.add(asset)
        await session.flush()
        session.add_all(
            [
                AssetMovement(
                    id=uuid4(),
                    tenant_id=demo_tenant.id,
                    asset_id=asset.id,
                    type=AssetMovementType.RECEIVE,
                    quantity=Decimal("20"),
                    occurred_at=datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
                ),
                AssetMovement(
                    id=uuid4(),
                    tenant_id=demo_tenant.id,
                    asset_id=asset.id,
                    type=AssetMovementType.CONSUME,
                    quantity=Decimal("-8"),
                    reference_order_id=seed["order"].id,
                    occurred_at=datetime(2026, 9, 3, 12, 30, tzinfo=UTC),
                ),
            ]
        )

    for email, password in (("staff@4mex.cz", "staffpass"), ("jan@acme.cz", "contactpass")):
        tenant_client.cookies.clear()
        await _login(tenant_client, email, password)
        body = (await tenant_client.get(f"/app/assets/{asset.id}")).text
        assert f'href="/app/orders/{seed["order"].id}"' in body, email
        assert ">2026-UI-0001</a>" in body
        assert body.count("data-movement-order") == 2
        # Local time (Europe/Prague, CEST): 12:30 UTC -> 14:30.
        assert "03.09.2026 14:30" in body
        assert "12 m²" in body


# ------------------------------------------------------------ activity feed


async def test_upload_activity_names_the_order(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    seed = await _seed(owner_engine, demo_tenant.id, status=OrderStatus.QUOTED)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add(
            AuditEvent(
                id=uuid4(),
                tenant_id=demo_tenant.id,
                occurred_at=datetime.now(UTC),
                actor_type="contact",
                actor_id=seed["contact"].id,
                actor_label="Jan Novák",
                action="attachment.upload",
                entity_type="attachment",
                entity_id=uuid4(),
                entity_label="K-07_cover.pdf",
                diff={"before": None, "after": {"order_id": str(seed["order"].id)}},
            )
        )
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    body = (await tenant_client.get("/app")).text
    assert re.search(
        rf'href="/app/orders/{seed["order"].id}"[^>]*data-activity-order>2026-UI-0001<', body
    )
    assert "(K-07_cover.pdf)" in body


# -------------------------------------------------------------------- footer


async def test_footer_has_no_version_number(
    tenant_client: AsyncClient, owner_engine, demo_tenant
) -> None:
    from app import __version__

    await _seed(owner_engine, demo_tenant.id, status=OrderStatus.DRAFT)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    body = (await tenant_client.get("/app")).text
    footer = body[body.index("<footer") :]
    assert f"v{__version__}" not in footer
    assert "Assoluto" in footer
    assert "title=" not in footer[: footer.index("Assoluto</span>")]  # build id: staff only

    tenant_client.cookies.clear()
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    footer = (await tenant_client.get("/app")).text.split("<footer", 1)[1]
    assert re.search(r'<span title="[^"]+">Assoluto</span>', footer)
