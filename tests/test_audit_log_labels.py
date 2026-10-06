"""Audit log speaks human (demo review P2-5): action / actor / status
labels from the one shared map, order numbers instead of UUIDs, raw JSON
only behind "Technical details" for tenant admins."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.audit_event import AuditEvent
from app.models.enums import STATUS_LABELS, OrderStatus, UserRole
from app.models.order import Order
from app.models.user import User
from app.security.passwords import hash_password
from app.templating import _new_environment


def test_shared_labels_cover_every_status_with_the_enum_msgids() -> None:
    module = _new_environment(None).get_template("_audit_labels.html").module
    assert module.status_labels == {s.value: label for s, label in STATUS_LABELS.items()}
    assert set(module.actor_type_labels) == {"user", "contact", "system"}
    # The dashboard feed and the audit log import the same names.
    for name in ("activity_icons", "activity_verbs", "entity_type_labels", "field_labels"):
        assert isinstance(getattr(module, name), dict)


def test_every_recorded_action_has_a_verb() -> None:
    """Grep-free guard: the action codes the app writes all have a label."""
    import re
    from pathlib import Path

    module = _new_environment(None).get_template("_audit_labels.html").module
    root = Path(__file__).resolve().parent.parent / "app"
    actions: set[str] = set()
    for path in root.rglob("*.py"):
        for m in re.finditer(r'action=\(?\s*"([a-z_]+\.[a-z_]+)"', path.read_text()):
            actions.add(m.group(1))
        for m in re.finditer(
            r'"([a-z_]+\.[a-z_]+)" if \w+ else "([a-z_]+\.[a-z_]+)"', path.read_text()
        ):
            actions.update(m.groups())
    missing = sorted(a for a in actions if a not in module.activity_verbs)
    assert actions and not missing, missing


# ------------------------------------------------------------- Postgres


async def _seed(owner_engine, tenant_id) -> dict:
    from tests.test_orders_item_autosave import _seed as base_seed

    seed = await base_seed(owner_engine, tenant_id)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        operator = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email="op@4mex.cz",
            full_name="Operator",
            role=UserRole.TENANT_STAFF,
            password_hash=hash_password("operatorpass"),
        )
        order = Order(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_id=seed["acme"].id,
            number="2026-000021",
            title="Kryt K-07",
            status=OrderStatus.SUBMITTED,
        )
        session.add_all([operator, order])
        await session.flush()
        when = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
        session.add_all(
            [
                AuditEvent(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    occurred_at=when,
                    actor_type="contact",
                    actor_id=seed["jan"].id,
                    actor_label="Jan Novák",
                    action="order.status_changed",
                    entity_type="order",
                    entity_id=order.id,
                    entity_label=order.number,
                    diff={"before": {"status": "draft"}, "after": {"status": "submitted"}},
                ),
                AuditEvent(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    occurred_at=when,
                    actor_type="user",
                    actor_id=seed["staff"].id,
                    actor_label="Owner",
                    action="attachment.upload",
                    entity_type="attachment",
                    entity_id=uuid4(),
                    entity_label="vykres.pdf",
                    diff={"after": {"order_id": str(order.id), "filename": "vykres.pdf"}},
                ),
            ]
        )
    return {"order_id": order.id}


@pytest.mark.postgres
async def test_audit_log_is_human_readable(tenant_client, owner_engine, demo_tenant) -> None:
    from tests.test_orders_item_autosave import _login

    ids = await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    body = (await tenant_client.get("/app/admin/audit", headers={"Accept-Language": "en"})).text

    assert "Changed status of" in body
    assert "Uploaded a file to" in body
    assert "Client contact" in body and "Supplier team" in body
    # Status values by their label, in a readable before → after.
    assert "Draft" in body and "Submitted" in body
    assert "&#34;draft&#34;" in body  # only inside the admin's raw JSON …
    # … and the order id became the order number, linked.
    assert f'<a href="/app/orders/{ids["order_id"]}"' in body
    assert ">2026-000021</a>" in body
    # Day-first local time, no ISO.
    assert "01.10.2026 10:00" in body
    assert "2026-10-01 10:00" not in body
    # Admin: the raw diff is one click away.
    assert "Technical details" in body
    assert "<code>order.status_changed</code>" in body


@pytest.mark.postgres
async def test_audit_log_hides_raw_json_from_non_admin_staff(
    tenant_client, owner_engine, demo_tenant
) -> None:
    from tests.test_orders_item_autosave import _login

    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "op@4mex.cz", "operatorpass")
    resp = await tenant_client.get("/app/admin/audit", headers={"Accept-Language": "en"})
    assert resp.status_code == 200
    body = resp.text
    assert "Changed status of" in body
    assert ">2026-000021</a>" in body
    assert "Technical details" not in body
    assert "&#34;draft&#34;" not in body
    assert "order.status_changed" not in body


@pytest.mark.postgres
async def test_dashboard_feed_uses_the_shared_labels(
    tenant_client, owner_engine, demo_tenant
) -> None:
    from tests.test_orders_item_autosave import _login

    await _seed(owner_engine, demo_tenant.id)
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    body = (await tenant_client.get("/app", headers={"Accept-Language": "en"})).text
    assert "changed status of" in body
    assert "uploaded a file to" in body
    assert "order.status_changed" not in body


async def _seed_comment_and_upload(owner_engine, tenant_id, order_id) -> dict:
    comment_id, attachment_id = uuid4(), uuid4()
    when = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    sm = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        session.add_all(
            [
                AuditEvent(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    occurred_at=when,
                    actor_type="user",
                    actor_id=uuid4(),
                    actor_label="Owner",
                    action="order.comment_added",
                    entity_type="order",
                    entity_id=order_id,
                    entity_label="2026-000021",
                    diff={
                        "before": None,
                        "after": {
                            "comment_id": str(comment_id),
                            "is_internal": True,
                            "body": "Lakovna má RAL 5010 skladem, kooperace 3 pracovní dny.",
                        },
                    },
                ),
                AuditEvent(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    occurred_at=when,
                    actor_type="user",
                    actor_id=uuid4(),
                    actor_label="Owner",
                    action="attachment.upload",
                    entity_type="attachment",
                    entity_id=attachment_id,
                    entity_label="K-07.pdf",
                    diff={
                        "after": {
                            "order_id": str(order_id),
                            "size_bytes": 46490,
                            "content_type": "application/pdf",
                        }
                    },
                ),
            ]
        )
    return {"comment_id": str(comment_id)}


def _human_part(body: str) -> str:
    """The audit table without the admin's "Technical details" blocks."""
    import re

    return re.sub(r"<details.*?</details>", "", body, flags=re.S)


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("lang", "comment", "internal", "size"),
    [
        ("en", "Comment", "internal", "45.4 kB"),
        ("cs", "Comment", "interní", "45,4 kB"),
        ("de", "Comment", None, "45,4 kB"),
    ],
)
async def test_comment_rows_read_as_comments(
    tenant_client, owner_engine, demo_tenant, lang, comment, internal, size
) -> None:
    """P2-5 leftover: no raw ``body:`` / ``comment_id: <uuid>`` /
    ``is_internal: no`` above the toggle — an excerpt and an "internal"
    badge; the ids stay in the admin's technical details."""
    from tests.test_orders_item_autosave import _login

    ids = await _seed(owner_engine, demo_tenant.id)
    extra = await _seed_comment_and_upload(owner_engine, demo_tenant.id, ids["order_id"])
    await _login(tenant_client, "staff@4mex.cz", "staffpass")
    body = (await tenant_client.get("/app/admin/audit", headers={"Accept-Language": lang})).text
    human = _human_part(body)
    assert "Lakovna má RAL 5010 skladem" in human
    assert "data-audit-internal" in human
    if internal:
        assert internal in human
    for raw in ("comment_id", "is_internal", "body:", "content_type", "size_bytes"):
        assert raw not in human, raw
    assert extra["comment_id"] not in human
    assert size in human.replace(chr(0xA0), " ")
    # The order id still turns into the linked order number.
    assert ">2026-000021</a>" in human
    # Admin: the raw ids are one click away.
    assert extra["comment_id"] in body
    if lang == "en":
        assert f"{comment}:" in human
