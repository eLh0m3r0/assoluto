"""Czech plurals through ngettext (demo review P2-4) and the "open orders"
definition without drafts (P3-14).

The real .po catalogs are maintained in a separate i18n pass, so the
plural tests install a tiny in-memory Czech catalog with the three CLDR
forms. That checks the mechanism — the templates and the dashboard call
``ngettext`` with the right msgids — independently of translation work.
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from uuid import uuid4

import pytest
from babel.messages.catalog import Catalog
from babel.messages.mofile import write_mo
from babel.support import Translations
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import i18n
from app.models.customer import CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus
from app.models.order import Order
from app.security.passwords import hash_password

pytestmark = pytest.mark.postgres

_CS_PLURALS = {
    ("%(num)d contact", "%(num)d contacts"): (
        "%(num)d kontakt",
        "%(num)d kontakty",
        "%(num)d kontaktů",
    ),
    ("%(num)d open order", "%(num)d open orders"): (
        "%(num)d otevřená zakázka",
        "%(num)d otevřené zakázky",
        "%(num)d otevřených zakázek",
    ),
    ("Older than {days} day — follow up.", "Older than {days} days — follow up."): (
        "Starší než {days} den — ozvěte se.",
        "Starší než {days} dny — ozvěte se.",
        "Starší než {days} dní — ozvěte se.",
    ),
    (
        "+ %(num)d draft not yet submitted",
        "+ %(num)d drafts not yet submitted",
    ): (
        "+ %(num)d rozpracovaná, neodeslaná",
        "+ %(num)d rozpracované, neodeslané",
        "+ %(num)d rozpracovaných, neodeslaných",
    ),
}


@pytest.fixture
def czech_plural_catalog(monkeypatch):
    catalog = Catalog(locale="cs")
    for msgid, msgstr in _CS_PLURALS.items():
        catalog.add(msgid, msgstr)
    buf = BytesIO()
    write_mo(buf, catalog)
    buf.seek(0)
    monkeypatch.setitem(i18n._TRANSLATIONS_CACHE, "cs", Translations(buf))


async def _seed(owner_engine, tenant_id) -> dict:
    from tests.test_orders_item_autosave import _seed as base_seed

    seed = await base_seed(owner_engine, tenant_id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        # ACME: jan + 2 more = 3 contacts. Other: eva + 4 more = 5.
        for customer, extra in ((seed["acme"], 2), (seed["other"], 4)):
            for i in range(extra):
                session.add(
                    CustomerContact(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        customer_id=customer.id,
                        email=f"c{i}-{customer.id.hex[:6]}@example.cz",
                        full_name=f"Contact {i}",
                        role=CustomerContactRole.CUSTOMER_USER,
                        password_hash=hash_password("x" * 12),
                        invited_at=datetime.now(),
                        accepted_at=datetime.now(),
                    )
                )
        # ACME: 3 open + 2 drafts + 1 delivered. Other: 1 open.
        statuses = [
            (seed["acme"], OrderStatus.SUBMITTED),
            (seed["acme"], OrderStatus.CONFIRMED),
            (seed["acme"], OrderStatus.READY),
            (seed["acme"], OrderStatus.DRAFT),
            (seed["acme"], OrderStatus.DRAFT),
            (seed["acme"], OrderStatus.DELIVERED),
            (seed["other"], OrderStatus.IN_PRODUCTION),
        ]
        for n, (customer, status) in enumerate(statuses, start=1):
            session.add(
                Order(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    customer_id=customer.id,
                    number=f"2026-{n:06d}",
                    title=f"Order {n}",
                    status=status,
                )
            )
    return seed


async def _login(client, email: str, password: str) -> None:
    from tests.test_orders_item_autosave import _login as base_login

    await base_login(client, email, password)


async def test_customer_list_uses_czech_plural_forms(
    tenant_client, owner_engine, demo_tenant, czech_plural_catalog
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    body = (await tenant_client.get("/app/customers")).text
    assert "3 kontakty" in body
    assert "5 kontaktů" in body
    assert "3 otevřené zakázky" in body  # drafts and the delivered one left out
    assert "1 otevřená zakázka" in body
    assert "kontaktů" not in body.replace("5 kontaktů", "")


async def test_customer_list_english_plurals(tenant_client, owner_engine, demo_tenant) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    body = (await tenant_client.get("/app/customers", headers={"Accept-Language": "en"})).text
    assert "3 contacts" in body
    assert "5 contacts" in body
    assert "3 open orders" in body
    assert "1 open order\n" in body


async def test_dashboard_open_orders_exclude_drafts(
    tenant_client, owner_engine, demo_tenant, czech_plural_catalog
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    body = (await tenant_client.get("/app")).text
    # 4 open (submitted, confirmed, ready, in production); 2 drafts aside.
    assert 'data-drafts-count="2"' in body
    assert "+ 2 rozpracované, neodeslané" in body
    import re

    card = re.search(r"Otevřené zakázky</p>\s*<p[^>]*>(\d+)</p>", body) or re.search(
        r"Open orders</p>\s*<p[^>]*>(\d+)</p>", body
    )
    assert card is not None
    assert card.group(1) == "4"


async def test_contact_dashboard_counts_own_orders_without_drafts(
    tenant_client, owner_engine, demo_tenant
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "jan@acme.cz", "contactpass")
    body = (await tenant_client.get("/app")).text
    assert 'data-drafts-count="2"' in body
    import re

    card = re.search(r"Otevřené zakázky</p>\s*<p[^>]*>(\d+)</p>", body) or re.search(
        r"Open orders</p>\s*<p[^>]*>(\d+)</p>", body
    )
    assert card is not None
    assert card.group(1) == "3"


async def test_stale_quote_hint_is_plural_aware(
    tenant_client, owner_engine, demo_tenant, czech_plural_catalog
) -> None:
    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    body = (await tenant_client.get("/app")).text
    # Default reminder age is 3 days -> Czech "few" form.
    assert "Starší než 3 dny — ozvěte se." in body
