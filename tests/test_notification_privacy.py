"""Notification privacy: pending invitees and cross-customer isolation.

* SEC-8 — a contact who has not accepted their invitation (never proved
  they own the address) still gets told something happened (CLAUDE.md
  §19: reachability yields rather than silence the customer), but the
  mail carries no comment text and no file name.
* SEC-9 — one customer's contacts never receive mail about another
  customer's order, for any event and through the digest path, even
  with ``scope=all`` and every event switched on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.email.sender import render_email
from app.models.customer import Customer, CustomerContact
from app.models.enums import CustomerContactRole, OrderStatus
from app.models.order import Order
from app.services.notification_prefs import (
    NotificationPrefs,
    NotificationScope,
    NotificationSide,
)
from tests.test_notification_routing import BASE_URL, _seed

pytestmark = pytest.mark.postgres

SECRET_COMMENT = "Use the 4.2 mm drill, price stays 1 250 CZK"
SECRET_FILE = "acme-secret-bracket-rev7.pdf"


async def _load_order(owner_engine, order_id):
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    session = sm()
    order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
    return session, order


async def test_pending_invitee_is_told_but_gets_no_content(
    owner_engine, demo_tenant, settings
) -> None:
    from app.services.notification_service import build_order_attachment, build_order_comment

    seeded = await _seed(
        owner_engine,
        demo_tenant.id,
        contacts=[{"email": "pending@acme.cz", "accepted_at": None}],
    )
    session, order = await _load_order(owner_engine, seeded["order"].id)
    try:
        comment = await build_order_comment(
            session,
            tenant=demo_tenant,
            order=order,
            author_email="staff0@4mex.cz",
            author_name="Staff 0",
            author_is_staff=True,
            body=SECRET_COMMENT,
            base_url=BASE_URL,
            settings=settings,
        )
        attachment = await build_order_attachment(
            session,
            tenant=demo_tenant,
            order=order,
            uploader_email="staff0@4mex.cz",
            uploader_name="Staff 0",
            uploader_is_staff=True,
            filename=SECRET_FILE,
            base_url=BASE_URL,
            settings=settings,
        )
    finally:
        await session.close()

    # §19: the only contact is pending, so reachability yields — they ARE told.
    assert [p.recipient.email for p in comment] == ["pending@acme.cz"]
    assert [p.recipient.email for p in attachment] == ["pending@acme.cz"]

    for payload in (*comment, *attachment):
        ctx = payload.context()
        assert "body_excerpt" not in ctx and "filename" not in ctx
        assert ctx["pending_invite"] is True
        for locale in (None, "cs", "en"):
            mail = render_email(payload.template, ctx, locale=locale)
            for part in (mail.subject, mail.html, mail.text):
                assert SECRET_COMMENT not in part
                assert SECRET_FILE not in part


async def test_accepted_contact_still_gets_the_content(owner_engine, demo_tenant, settings) -> None:
    from app.services.notification_service import build_order_comment

    seeded = await _seed(owner_engine, demo_tenant.id, contacts=[{"email": "jan@acme.cz"}])
    session, order = await _load_order(owner_engine, seeded["order"].id)
    try:
        payloads = await build_order_comment(
            session,
            tenant=demo_tenant,
            order=order,
            author_email="staff0@4mex.cz",
            author_name="Staff 0",
            author_is_staff=True,
            body=SECRET_COMMENT,
            base_url=BASE_URL,
            settings=settings,
        )
    finally:
        await session.close()
    [payload] = payloads
    assert payload.context()["body_excerpt"] == SECRET_COMMENT
    assert SECRET_COMMENT in render_email(payload.template, payload.context()).html


async def test_other_customers_contacts_never_hear_about_this_order(
    owner_engine, demo_tenant, settings
) -> None:
    from app.services.notification_service import (
        build_order_attachment,
        build_order_comment,
        build_order_created,
        build_order_status_changed,
        build_order_submitted,
        merge_for_digest,
    )

    seeded = await _seed(owner_engine, demo_tenant.id, contacts=[{"email": "jan@acme.cz"}])

    # Customer B: maximally eager contacts — scope=all, every event on,
    # one accepted and one pending (pending ones are the yield fallback).
    eager = NotificationPrefs.defaults(NotificationSide.CONTACT, CustomerContactRole.CUSTOMER_ADMIN)
    eager_prefs = NotificationPrefs(
        side=NotificationSide.CONTACT,
        events=eager.events,
        scope=NotificationScope.ALL,
    ).to_dict()
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with sm() as s, s.begin():
        other = Customer(id=uuid4(), tenant_id=demo_tenant.id, name="Rival")
        s.add(other)
        await s.flush()
        s.add_all(
            [
                CustomerContact(
                    id=uuid4(),
                    tenant_id=demo_tenant.id,
                    customer_id=other.id,
                    email=email,
                    full_name="Rival",
                    role=CustomerContactRole.CUSTOMER_ADMIN,
                    invited_at=now,
                    accepted_at=accepted,
                    notification_prefs=eager_prefs,
                )
                for email, accepted in (("boss@rival.cz", now), ("new@rival.cz", None))
            ]
        )
        second = Order(
            id=uuid4(),
            tenant_id=demo_tenant.id,
            customer_id=seeded["customer"].id,
            number="2026-000002",
            title="Second",
            status=OrderStatus.QUOTED,
        )
        s.add(second)
    rival = {"boss@rival.cz", "new@rival.cz"}

    session, order = await _load_order(owner_engine, seeded["order"].id)
    second_order = (await session.execute(select(Order).where(Order.id == second.id))).scalar_one()
    common = {"tenant": demo_tenant, "base_url": BASE_URL, "settings": settings}
    try:
        payloads = [
            *await build_order_submitted(session, order=order, **common),
            *await build_order_created(session, order=order, author_name="Staff", **common),
            *await build_order_comment(
                session,
                order=order,
                author_email="staff0@4mex.cz",
                author_name="Staff 0",
                author_is_staff=True,
                body="hi",
                **common,
            ),
            *await build_order_comment(
                session,
                order=order,
                author_email="jan@acme.cz",
                author_name="Jan",
                author_is_staff=False,
                body="hi",
                **common,
            ),
            *await build_order_attachment(
                session,
                order=order,
                uploader_email="staff0@4mex.cz",
                uploader_name="Staff 0",
                uploader_is_staff=True,
                filename="a.pdf",
                **common,
            ),
        ]
        status_payloads = []
        for o in (order, second_order):
            status_payloads += await build_order_status_changed(
                session, order=o, to_status=OrderStatus.CONFIRMED, **common
            )
            status_payloads += await build_order_status_changed(
                session,
                order=o,
                to_status=OrderStatus.CONFIRMED,
                actor_is_contact=True,
                actor_email="jan@acme.cz",
                **common,
            )
    finally:
        await session.close()

    digest = merge_for_digest(status_payloads)
    every = [*payloads, *status_payloads, *digest]
    addressed = {p.recipient.email for p in every}
    assert addressed, "the builders must address someone"
    assert "jan@acme.cz" in addressed  # the right customer is told
    assert not addressed & rival, addressed & rival
