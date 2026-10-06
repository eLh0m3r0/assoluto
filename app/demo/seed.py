"""Seed a realistic *sales demo* tenant: a Czech sheet-metal / CNC job shop.

Built for showing the product to prospects (MSV Brno and similar) and for
the public demo (``PUBLIC_DEMO_TENANT``, re-seeded nightly by
:mod:`app.tasks.demo_reset`): six clients, their contacts, a priced
catalogue, ~27 orders spread over every status with line items, comments,
requested and promised dates (one in production past its promised date,
quotes waiting for the client's confirmation), client-owned material with
receive / consume / return movements, and three fictional drawings (PDF,
"UKÁZKA / SAMPLE" watermark, rendered here with thumbnails) attached to
orders.

Every company and person below is **fictional** — the names carry
"Ukázková / Vzorová / Příkladná / Modelová / Fiktivní / Demo" on purpose,
and every email address is under ``example.com`` (RFC 2606, reserved for
documentation). No IČO / DIČ is set, because a random 8-digit IČO can
belong to a real company. Prices are illustrative, in CZK excl. VAT.

Idempotent: running it again wipes *only this demo tenant's* data — DB rows
and every S3 object under its storage prefix — and recreates it, so dates
stay relative to "today". It refuses to touch a tenant with the same slug
that it did not create itself (``tenants.settings["demo_seed"]``) unless
``--force`` is given.

Usage (inside the container: ``docker exec -it assoluto-web-1 …``)::

    python -m app.demo.seed                       # slug "demo", random password
    python -m app.demo.seed --slug msv --password 'Veletrh-2026!'
    python -m app.demo.seed --no-files            # no S3 at all (no drawings)

``python -m scripts.seed_demo`` still works (thin wrapper). Uses
``DATABASE_OWNER_URL`` (the table owner — bypasses RLS), like the other
CLI scripts.
"""

# Czech typography in the demo copy uses the en dash and the
# multiplication sign on purpose (product names like "600x400, RAL 7035"
# written the Czech way).
# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import asyncio
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import anyio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.demo import DEMO_MARKER
from app.demo.drawings import SampleDrawing, render_drawing_pdf
from app.logging import get_logger
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
from app.models.order import Order, OrderComment, OrderItem, OrderStatusHistory
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import User
from app.security.passwords import hash_password

log = get_logger("app.demo.seed")

DOMAIN = "example.com"

TENANT_NAME = "CNC Dílna Vzorová s.r.o."

#: The two logins the public demo hands out (``POST /demo/enter``):
#: the shop's admin and the admin contact of its first client.
STAFF_LOCAL, STAFF_DOMAIN = "vedouci", "dilna-vzorova"
CONTACT_LOCAL, CONTACT_DOMAIN = "nakup", "ukazkova"

# (email local part, full name, role)
STAFF: list[tuple[str, str, UserRole]] = [
    ("vedouci", "Pavel Vedoucí", UserRole.TENANT_ADMIN),
    ("planovani", "Lenka Plánovačka", UserRole.TENANT_STAFF),
    ("dilna", "Tomáš Mistr", UserRole.TENANT_STAFF),
]

# key -> (name, mail domain, [(local, full name, admin?, last login days ago | None)])
CUSTOMERS: dict[str, tuple[str, str, list[tuple[str, str, bool, int | None]]]] = {
    "ukazkova": (
        "Strojírna Ukázková s.r.o.",
        "ukazkova",
        [
            ("nakup", "Jana Nákupčí", True, 1),
            ("konstrukce", "Martin Konstruktér", False, 4),
        ],
    ),
    "vzorova": (
        "Kovovýroba Vzorová a.s.",
        "vzorova",
        [
            ("zasobovani", "Petr Zásobovač", True, 2),
            ("kvalita", "Eva Kvalitářka", False, None),
        ],
    ),
    "prikladna": (
        "Zemědělská technika Příkladná s.r.o.",
        "prikladna",
        [("objednavky", "Josef Objednávkář", True, 6)],
    ),
    "fiktivni": (
        "Elektro Fiktivní spol. s r.o.",
        "fiktivni",
        [
            ("vyroba", "Ivana Výrobní", True, 12),
            ("technolog", "Karel Technolog", False, None),
        ],
    ),
    "modelova": (
        "Nábytek Modelový s.r.o.",
        "modelovy",
        [("majitel", "Radek Majitel", True, None)],
    ),
    "demo_auto": (
        "Automotive Demo Komponenty s.r.o.",
        "demo-komponenty",
        [
            ("buyer", "Lucie Nákupní", True, 0),
            ("sqe", "Ondřej Dodavatelský", False, 9),
            ("logistika", "Hana Logistická", False, 20),
        ],
    ),
}

# sku -> (name, unit, price CZK, client key or None for the shared catalogue)
PRODUCTS: dict[str, tuple[str, str, str, str | None]] = {
    "LAS-S235-3": ("Laserové řezání – ocel S235 3 mm", "m", "18.00", None),
    "LAS-S235-6": ("Laserové řezání – ocel S235 6 mm", "m", "29.00", None),
    "LAS-NEREZ-2": ("Laserové řezání – nerez 1.4301 2 mm", "m", "32.00", None),
    "OHYB": ("Ohraňování – jeden ohyb", "ks", "12.00", None),
    "CNC-FREZ": ("CNC frézování – strojní hodina", "hod", "1150.00", None),
    "CNC-SOUST": ("CNC soustružení – strojní hodina", "hod", "980.00", None),
    "SVAR-MAG": ("Svařování MIG/MAG", "hod", "720.00", None),
    "SVAR-TIG": ("Svařování TIG – nerez", "hod", "890.00", None),
    "ODJEHL": ("Odjehlení a sražení hran", "ks", "8.00", None),
    "LAK-PRASK": ("Práškové lakování RAL dle zadání", "m2", "260.00", None),
    "ZINEK": ("Žárové zinkování (kooperace)", "kg", "38.00", None),
    "MAT-S235-3": ("Plech S235JR 3 mm – materiál", "kg", "32.00", None),
    "MAT-ALMG3-2": ("Plech AlMg3 2 mm – materiál", "kg", "115.00", None),
    "MAT-1.4301-2": ("Plech nerez 1.4301 2 mm – materiál", "kg", "125.00", None),
    "KONTROLA": ("Výstupní kontrola + měřicí protokol", "ks", "450.00", None),
    "UK-KM120": ("Konzole motoru KM-120 dle výkresu", "ks", "385.00", "ukazkova"),
    "UK-KRYT-07": ("Kryt převodovky K-07, lakovaný", "ks", "1240.00", "ukazkova"),
    "VZ-PRIR-30": ("Příruba P30, soustružená", "ks", "214.00", "vzorova"),
    "PR-DRZAK-H": ("Držák hydrauliky H-4, zinkovaný", "ks", "168.00", "prikladna"),
    "FI-ROZV-600": ("Skříň rozvaděče 600×400, RAL 7035", "ks", "2860.00", "fiktivni"),
    "AU-PLECH-B2": ("Výztuha B2 – sériový díl", "ks", "47.50", "demo_auto"),
}


@dataclass(frozen=True)
class AssetSpec:
    """Client-owned material held in stock; stock = received - consumed - returned."""

    client: str
    code: str
    name: str
    unit: str
    received: str
    consumed: str
    location: str
    returned: str = "0"  # leftovers handed back to the client


ASSETS: list[AssetSpec] = [
    AssetSpec(
        "ukazkova",
        "UK-S235-3",
        "Plech S235JR 3 mm (materiál zákazníka)",
        "kg",
        "1200",
        "430",
        "Regál A1",
    ),
    AssetSpec(
        "ukazkova",
        "UK-TR-40",
        "Trubka 40×40×3 (materiál zákazníka)",
        "m",
        "180",
        "64",
        "Stojan B2",
        returned="16",
    ),
    AssetSpec(
        "vzorova",
        "VZ-KRUH-60",
        "Kulatina C45 ø60 (materiál zákazníka)",
        "m",
        "36",
        "11.5",
        "Stojan C1",
    ),
    AssetSpec(
        "fiktivni",
        "FI-DC01-15",
        "Plech DC01 1,5 mm (materiál zákazníka)",
        "kg",
        "800",
        "520",
        "Regál A3",
    ),
    AssetSpec(
        "demo_auto",
        "AU-DX51-2",
        "Svitek DX51D+Z 2 mm (materiál zákazníka)",
        "kg",
        "2500",
        "1850",
        "Hala 2",
    ),
    AssetSpec(
        "prikladna",
        "PR-PAS-80",
        "Pásovina 80×8 (materiál zákazníka)",
        "m",
        "60",
        "0",
        "Stojan B4",
    ),
]

PIPELINE = [
    OrderStatus.DRAFT,
    OrderStatus.SUBMITTED,
    OrderStatus.QUOTED,
    OrderStatus.CONFIRMED,
    OrderStatus.IN_PRODUCTION,
    OrderStatus.READY,
    OrderStatus.DELIVERED,
    OrderStatus.CLOSED,
]


@dataclass(frozen=True)
class OrderSpec:
    client: str
    title: str
    status: OrderStatus
    age_days: int  # created this many days ago
    items: tuple[tuple[str, str], ...]  # (sku, quantity)
    by_contact: bool = True
    requested_in: int | None = 14  # requested delivery: created + N days
    promised_in: int | None = None  # promised delivery: created + N days
    comments: tuple[tuple[str, str], ...] = ()  # ("staff"|"contact"|"internal", body)


ORDERS: list[OrderSpec] = [
    OrderSpec(
        "ukazkova",
        "Konzole KM-120 – série 200 ks",
        OrderStatus.IN_PRODUCTION,
        12,
        (("UK-KM120", "200"), ("ODJEHL", "200"), ("KONTROLA", "1")),
        promised_in=16,
        comments=(
            ("contact", "Prosíme o dodání do konce měsíce, montáž začíná 1. v měsíci."),
            ("staff", "Potvrzujeme, laser hotový, jde na ohraňování."),
        ),
    ),
    OrderSpec(
        "ukazkova",
        "Kryty převodovky K-07 – rev. C",
        OrderStatus.QUOTED,
        3,
        (("UK-KRYT-07", "40"), ("LAK-PRASK", "18")),
        comments=(("staff", "Nacenili jsme dle revize C výkresu. Lakování RAL 5010."),),
    ),
    OrderSpec(
        "ukazkova",
        "Rám stojanu – prototyp",
        OrderStatus.SUBMITTED,
        1,
        (("LAS-S235-6", "42"), ("OHYB", "16"), ("SVAR-MAG", "6")),
        requested_in=10,
    ),
    OrderSpec(
        "ukazkova",
        "Konzole KM-120 – série 150 ks",
        OrderStatus.CLOSED,
        58,
        (("UK-KM120", "150"), ("ODJEHL", "150")),
        promised_in=18,
    ),
    OrderSpec(
        "ukazkova",
        "Distanční podložky 3 mm",
        OrderStatus.DELIVERED,
        20,
        (("LAS-S235-3", "64"), ("MAT-S235-3", "48")),
        promised_in=10,
    ),
    OrderSpec(
        "vzorova",
        "Příruby P30 – 500 ks",
        OrderStatus.CONFIRMED,
        6,
        (("VZ-PRIR-30", "500"), ("KONTROLA", "1")),
        promised_in=21,
        comments=(
            ("contact", "Materiál C45 dovezeme ve středu."),
            ("internal", "Pozor: zákazník chce měřicí protokol ke každé dávce."),
        ),
    ),
    OrderSpec(
        "vzorova",
        "Hřídele ø40 – opakovaná",
        OrderStatus.READY,
        15,
        (("CNC-SOUST", "14"), ("KONTROLA", "1")),
        promised_in=14,
        comments=(("staff", "Hotovo, připraveno k odběru na rampě 2."),),
    ),
    OrderSpec(
        "vzorova",
        "Upínací deska – frézování",
        OrderStatus.IN_PRODUCTION,
        25,
        (("CNC-FREZ", "9"), ("ODJEHL", "4")),
        promised_in=20,
        comments=(("staff", "Zpoždění kvůli opravě frézky, nový termín sdělíme zítra."),),
    ),
    OrderSpec(
        "vzorova",
        "Pouzdra – zrušeno zákazníkem",
        OrderStatus.CANCELLED,
        30,
        (("CNC-SOUST", "6"),),
        comments=(("contact", "Projekt odložen, zakázku prosím stornujte."),),
    ),
    OrderSpec(
        "prikladna",
        "Držáky hydrauliky H-4 – 120 ks",
        OrderStatus.IN_PRODUCTION,
        9,
        (("PR-DRZAK-H", "120"), ("ZINEK", "310")),
        promised_in=24,
    ),
    OrderSpec(
        "prikladna",
        "Svařenec rámu secího stroje",
        OrderStatus.QUOTED,
        4,
        (("LAS-S235-6", "65"), ("SVAR-MAG", "11"), ("LAK-PRASK", "6")),
        requested_in=21,
    ),
    OrderSpec(
        "prikladna",
        "Náhradní díly – sezóna",
        OrderStatus.DRAFT,
        0,
        (("PR-DRZAK-H", "30"),),
        requested_in=None,
    ),
    OrderSpec(
        "prikladna",
        "Plechové kryty – oprava",
        OrderStatus.DELIVERED,
        34,
        (("LAS-S235-3", "28"), ("OHYB", "40")),
        promised_in=12,
    ),
    OrderSpec(
        "fiktivni",
        "Skříně rozvaděčů 600×400 – 12 ks",
        OrderStatus.CONFIRMED,
        5,
        (("FI-ROZV-600", "12"),),
        promised_in=28,
        comments=(
            ("contact", "Prosím o potvrzení odstínu RAL 7035 dle vzorníku."),
            ("staff", "Potvrzeno, RAL 7035 jemná struktura."),
        ),
    ),
    OrderSpec(
        "fiktivni",
        "Montážní panely DC01",
        OrderStatus.SUBMITTED,
        2,
        (("LAS-S235-3", "48"), ("OHYB", "96")),
        requested_in=12,
    ),
    OrderSpec(
        "fiktivni",
        "Skříně rozvaděčů – minulá dávka",
        OrderStatus.CLOSED,
        45,
        (("FI-ROZV-600", "8"),),
        promised_in=25,
    ),
    OrderSpec(
        "fiktivni",
        "Úchyty DIN lišty",
        OrderStatus.READY,
        11,
        (("LAS-S235-3", "36"), ("OHYB", "120"), ("ODJEHL", "120")),
        promised_in=12,
    ),
    OrderSpec(
        "modelova",
        "Nerezové nohy stolů – vzorek",
        OrderStatus.QUOTED,
        7,
        (("LAS-NEREZ-2", "24"), ("SVAR-TIG", "5"), ("MAT-1.4301-2", "18")),
        comments=(
            ("contact", "Šlo by to i v kartáčovaném provedení?"),
            ("staff", "Ano, kartáčování přidáme za 120 Kč/ks, upravíme nabídku."),
        ),
    ),
    OrderSpec(
        "modelova",
        "Konzole polic – hliník",
        OrderStatus.DRAFT,
        1,
        (("MAT-ALMG3-2", "12"), ("ODJEHL", "40")),
        by_contact=False,
        requested_in=None,
    ),
    OrderSpec(
        "demo_auto",
        "Výztuha B2 – odvolávka 10/2026",
        OrderStatus.IN_PRODUCTION,
        8,
        (("AU-PLECH-B2", "2400"), ("KONTROLA", "1")),
        promised_in=12,
        comments=(("contact", "Dodací list prosím s číslem odvolávky."),),
    ),
    OrderSpec(
        "demo_auto",
        "Výztuha B2 – odvolávka 09/2026",
        OrderStatus.DELIVERED,
        38,
        (("AU-PLECH-B2", "2400"),),
        promised_in=14,
    ),
    OrderSpec(
        "demo_auto",
        "Výztuha B2 – odvolávka 08/2026",
        OrderStatus.CLOSED,
        60,
        (("AU-PLECH-B2", "2000"),),
        promised_in=14,
    ),
    OrderSpec(
        "demo_auto",
        "Přípravek pro kontrolu B2",
        OrderStatus.CONFIRMED,
        10,
        (("CNC-FREZ", "16"), ("KONTROLA", "1")),
        promised_in=30,
        by_contact=False,
    ),
    OrderSpec(
        "demo_auto",
        "Vzorky nového dílu B3 (PPAP)",
        OrderStatus.SUBMITTED,
        0,
        (("LAS-S235-3", "12"), ("OHYB", "50"), ("KONTROLA", "1")),
        requested_in=20,
        comments=(("contact", "Posíláme 3D model a výkres rev. A, potřebujeme 50 ks vzorků."),),
    ),
    OrderSpec(
        "ukazkova",
        "Stojan na palety – zrušeno",
        OrderStatus.CANCELLED,
        40,
        (("SVAR-MAG", "8"),),
        by_contact=False,
    ),
    # The client the public demo's customer login belongs to gets one order
    # in every interesting state, so the customer view is never thin.
    OrderSpec(
        "ukazkova",
        "Držáky čidel DC-60 – 60 ks",
        OrderStatus.READY,
        9,
        (("LAS-S235-3", "22"), ("OHYB", "120"), ("ODJEHL", "60")),
        promised_in=10,
        comments=(("staff", "Hotovo, k vyzvednutí na rampě 1 zítra od 7:00."),),
    ),
    OrderSpec(
        "ukazkova",
        "Výztuhy rámu – rozpracováno",
        OrderStatus.DRAFT,
        0,
        (("LAS-S235-6", "18"), ("OHYB", "24")),
        requested_in=None,
    ),
]

# Fictional drawings rendered at seed time and attached to these orders
# (by title), uploaded by the client contact who created the order.
DRAWINGS: dict[str, SampleDrawing] = {
    "Konzole KM-120 – série 200 ks": SampleDrawing(
        filename="KM-120_konzole_rev-B.pdf",
        number="KM-120",
        title="Konzole motoru KM-120",
        revision="B",
        material="S235JR, plech t = 3 mm",
        client="Strojírna Ukázková s.r.o.",
        scale="1:2",
        shape="bracket",
        notes=("Povrch: bez úpravy, naolejovat",),
    ),
    "Kryty převodovky K-07 – rev. C": SampleDrawing(
        filename="K-07_kryt_prevodovky_rev-C.pdf",
        number="K-07",
        title="Kryt převodovky K-07",
        revision="C",
        material="DC01, plech t = 2 mm",
        client="Strojírna Ukázková s.r.o.",
        scale="1:4",
        shape="cover",
        notes=("Povrch: práškový lak RAL 5010, jemná struktura",),
    ),
    "Skříně rozvaděčů 600×400 – 12 ks": SampleDrawing(
        filename="FI-ROZV-600x400_rev-A.pdf",
        number="FI-ROZV-600",
        title="Skříň rozvaděče 600×400",
        revision="A",
        material="DC01, plech t = 1,5 mm",
        client="Elektro Fiktivní spol. s r.o.",
        scale="1:5",
        shape="cabinet",
        notes=("Povrch: práškový lak RAL 7035",),
    ),
}


@dataclass
class SeedResult:
    tenant_id: UUID
    slug: str
    staff_email: str
    contact_email: str
    password: str
    customers: int
    contacts: int
    products: int
    orders: int
    assets: int
    attachments: int = 0
    s3_objects_deleted: int = 0


class DemoSeedRefused(RuntimeError):
    """The slug belongs to a tenant this script did not create."""


_WIPE_SQL = [
    "DELETE FROM audit_events WHERE tenant_id = :tid",
    "DELETE FROM asset_movements WHERE tenant_id = :tid",
    "DELETE FROM assets WHERE tenant_id = :tid",
    "DELETE FROM order_attachments WHERE tenant_id = :tid",
    "DELETE FROM order_comments WHERE tenant_id = :tid",
    "DELETE FROM order_status_history WHERE tenant_id = :tid",
    "DELETE FROM order_items WHERE tenant_id = :tid",
    "DELETE FROM orders WHERE tenant_id = :tid",
    "DELETE FROM products WHERE tenant_id = :tid",
    "DELETE FROM platform_tenant_memberships WHERE tenant_id = :tid",
    "DELETE FROM customer_contacts WHERE tenant_id = :tid",
    "DELETE FROM customers WHERE tenant_id = :tid",
    "DELETE FROM users WHERE tenant_id = :tid",
    # Anything still queued for the demo's fictional addresses.
    "DELETE FROM email_outbox WHERE tenant_id = :tid",
]


def _email(local: str, domain: str) -> str:
    return f"{local}@{domain}.{DOMAIN}"


#: Login e-mails of the two public-demo personas (see ``app.demo.router``).
DEMO_STAFF_EMAIL = _email(STAFF_LOCAL, STAFF_DOMAIN)
DEMO_CONTACT_EMAIL = _email(CONTACT_LOCAL, CONTACT_DOMAIN)


def _status_path(status: OrderStatus) -> list[OrderStatus]:
    """DRAFT -> … -> status; CANCELLED branches off after SUBMITTED."""
    if status == OrderStatus.CANCELLED:
        return [OrderStatus.DRAFT, OrderStatus.SUBMITTED, OrderStatus.CANCELLED]
    return PIPELINE[: PIPELINE.index(status) + 1]


def _audit(
    tenant_id: UUID,
    at: datetime,
    actor: User | CustomerContact,
    action: str,
    order: Order,
    *,
    before: dict | None = None,
    after: dict | None = None,
) -> AuditEvent:
    """An audit row shaped like the one the app records for ``action``."""
    return AuditEvent(
        id=uuid4(),
        tenant_id=tenant_id,
        occurred_at=at,
        created_at=at,
        actor_type="contact" if isinstance(actor, CustomerContact) else "user",
        actor_id=actor.id,
        actor_label=actor.full_name,
        action=action,
        entity_type="order",
        entity_id=order.id,
        entity_label=order.number,
        diff={"before": before, "after": after},
    )


def tenant_storage_prefix(storage_prefix: str | None) -> str:
    """Normalised ``tenants/<slug>/`` prefix, or ValueError when unsafe.

    The reset deletes *every* object under it, so an empty or root-level
    prefix (which would match the whole bucket) is refused outright.
    """
    prefix = (storage_prefix or "").strip().strip("/")
    if not prefix or prefix in (".", ".."):
        raise ValueError(f"refusing to clean an unsafe S3 prefix {storage_prefix!r}")
    return prefix + "/"


def _render_files(drawing: SampleDrawing) -> tuple[bytes, bytes | None]:
    """PDF bytes + JPEG thumbnail (None when poppler is unavailable). Blocking."""
    from app.tasks.thumbnail_tasks import _render_thumbnail

    pdf = render_drawing_pdf(drawing)
    return pdf, _render_thumbnail(pdf, "application/pdf")


async def _list_keys(prefix: str) -> list[str]:
    from app.storage import s3 as s3_storage

    return [obj["key"] for obj in await s3_storage.list_objects_async(prefix)]


async def _upload_drawings(
    tenant: Tenant,
    orders_by_title: dict[str, Order],
    authors: dict[UUID, CustomerContact],
) -> list[OrderAttachment]:
    """Render, upload and return the attachment rows (not yet added)."""
    from app.services.attachment_service import build_storage_key, build_thumbnail_key
    from app.storage import s3 as s3_storage

    rows: list[OrderAttachment] = []
    for title, drawing in DRAWINGS.items():
        order = orders_by_title.get(title)
        if order is None:
            continue
        pdf, thumb = await anyio.to_thread.run_sync(_render_files, drawing)
        att_id = uuid4()
        key = build_storage_key(
            tenant=tenant, order_id=order.id, attachment_id=att_id, filename=drawing.filename
        )
        await s3_storage.upload_bytes_async(key, pdf, content_type="application/pdf")
        thumb_key = None
        if thumb is not None:
            thumb_key = build_thumbnail_key(tenant=tenant, order_id=order.id, attachment_id=att_id)
            await s3_storage.upload_bytes_async(thumb_key, thumb, content_type="image/jpeg")
        author = authors.get(order.id)
        rows.append(
            OrderAttachment(
                id=att_id,
                tenant_id=tenant.id,
                order_id=order.id,
                kind=AttachmentKind.DRAWING,
                filename=drawing.filename,
                content_type="application/pdf",
                size_bytes=len(pdf),
                storage_key=key,
                thumbnail_key=thumb_key,
                uploaded_by_contact_id=author.id if author else None,
                # Uploaded with the order, so a day+ ago: the public demo's
                # "20 new files a day" cap never counts the seeded ones.
                created_at=order.created_at + timedelta(hours=2),
            )
        )
    return rows


async def seed_demo(
    *,
    slug: str = "demo",
    password: str | None = None,
    owner_url: str | None = None,
    engine: AsyncEngine | None = None,
    force: bool = False,
    today: date | None = None,
    files: bool = True,
) -> SeedResult:
    """Create (or recreate) the demo tenant. See the module docstring.

    ``files=False`` skips S3 entirely: no sample drawings are uploaded
    and nothing is deleted from the bucket (useful without object
    storage). With ``files=True`` an S3 failure is logged and the seed
    still completes, just without drawings.
    """
    from app.config import get_settings

    own_engine = engine is None
    if engine is None:
        engine = create_async_engine(owner_url or get_settings().database_owner_url, future=True)
    password = password or secrets.token_urlsafe(9)
    now = datetime.now(UTC)
    day0 = today or now.date()

    def at(days_ago: float) -> datetime:
        return now - timedelta(days=days_ago)

    old_keys: list[str] = []
    new_keys: set[str] = set()
    s3_ok = files
    attachments: list[OrderAttachment] = []

    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as session, session.begin():
            tenant = (
                await session.execute(select(Tenant).where(Tenant.slug == slug))
            ).scalar_one_or_none()
            if tenant is not None:
                if not (tenant.settings or {}).get(DEMO_MARKER) and not force:
                    raise DemoSeedRefused(
                        f"Tenant '{slug}' exists and was not created by seed_demo; "
                        "use another --slug, or --force to wipe it."
                    )
                for stmt in _WIPE_SQL:
                    await session.execute(text(stmt), {"tid": tenant.id})
                tenant.name = TENANT_NAME
                tenant.is_active = True
            else:
                tenant = Tenant(
                    id=uuid4(),
                    slug=slug,
                    name=TENANT_NAME,
                    billing_email=_email("fakturace", "dilna-vzorova"),
                    storage_prefix=f"tenants/{slug}/",
                )
                session.add(tenant)
            tenant.settings = {
                **(tenant.settings or {}),
                DEMO_MARKER: True,
                "demo_seeded_at": now.isoformat(),
            }
            await session.flush()
            tid = tenant.id
            pw_hash = hash_password(password)

            # Everything currently stored for this tenant; whatever the new
            # seed does not re-create is deleted once the DB has committed.
            if s3_ok:
                try:
                    old_keys = await _list_keys(tenant_storage_prefix(tenant.storage_prefix))
                except Exception as exc:
                    s3_ok = False
                    log.warning(
                        "demo_seed.s3_unavailable",
                        slug=slug,
                        error=f"{type(exc).__name__}: {exc}"[:200],
                    )

            # ---- staff
            staff: list[User] = []
            for i, (local, name, role) in enumerate(STAFF):
                user = User(
                    id=uuid4(),
                    tenant_id=tid,
                    email=_email(local, STAFF_DOMAIN),
                    full_name=name,
                    role=role,
                    password_hash=pw_hash,
                    last_login_at=at(i),
                )
                staff.append(user)
            session.add_all(staff)

            # ---- clients + contacts
            customers: dict[str, Customer] = {}
            contacts: dict[str, list[CustomerContact]] = {}
            for key, (name, domain, people) in CUSTOMERS.items():
                customer = Customer(id=uuid4(), tenant_id=tid, name=name, preferred_locale="cs")
                customers[key] = customer
                session.add(customer)
                await session.flush()
                contacts[key] = []
                for local, full_name, is_admin, login_ago in people:
                    accepted = login_ago is not None
                    contact = CustomerContact(
                        id=uuid4(),
                        tenant_id=tid,
                        customer_id=customer.id,
                        email=_email(local, domain),
                        full_name=full_name,
                        role=(
                            CustomerContactRole.CUSTOMER_ADMIN
                            if is_admin
                            else CustomerContactRole.CUSTOMER_USER
                        ),
                        password_hash=pw_hash if accepted else None,
                        # A pending invitation is a fresh one: the nightly
                        # stale-invite cleanup deletes anything older than
                        # its expiry, which would quietly thin the demo.
                        invited_at=at(70) if accepted else at(3),
                        accepted_at=at(65) if accepted else None,
                        last_login_at=at(login_ago) if login_ago is not None else None,
                    )
                    contacts[key].append(contact)
                    session.add(contact)
            await session.flush()

            # ---- catalogue
            products: dict[str, Product] = {}
            for sku, (pname, unit, price, client) in PRODUCTS.items():
                product = Product(
                    id=uuid4(),
                    tenant_id=tid,
                    customer_id=customers[client].id if client else None,
                    sku=sku,
                    name=pname,
                    unit=unit,
                    default_price=Decimal(price),
                )
                products[sku] = product
                session.add(product)
            await session.flush()

            # ---- orders
            owner, planner, foreman = staff
            audit: list[AuditEvent] = []
            year = day0.year
            created_orders: list[Order] = []
            orders_by_title: dict[str, Order] = {}
            authors: dict[UUID, CustomerContact] = {}
            for seq, spec in enumerate(sorted(ORDERS, key=lambda o: -o.age_days), start=1):
                # "Today" orders still need room for one history step per
                # status before now, or the steps tie on one timestamp.
                created = (
                    at(spec.age_days)
                    if spec.age_days
                    else now - timedelta(hours=len(_status_path(spec.status)) + 1)
                )
                created_day = created.date()
                author = contacts[spec.client][0]
                lines: list[OrderItem] = []
                total = Decimal("0")
                for pos, (sku, qty) in enumerate(spec.items):
                    product = products[sku]
                    quantity = Decimal(qty)
                    unit_price = product.default_price or Decimal("0")
                    line_total = (unit_price * quantity).quantize(Decimal("0.01"))
                    total += line_total
                    lines.append(
                        OrderItem(
                            id=uuid4(),
                            tenant_id=tid,
                            product_id=product.id,
                            position=pos,
                            description=product.name,
                            quantity=quantity,
                            unit=product.unit,
                            unit_price=unit_price,
                            line_total=line_total,
                            created_at=created,
                        )
                    )
                rank = PIPELINE.index(spec.status) if spec.status in PIPELINE else None
                # Spread the pipeline between creation and now. A fixed 20 h
                # per step pushed fresh orders' history into the future.
                path = _status_path(spec.status)
                gap = min(timedelta(hours=20), (now - created) / (len(path) + 1))

                def step_at(status: OrderStatus, _path=path, _gap=gap, _created=created):
                    return _created + _gap * _path.index(status)

                order = Order(
                    id=uuid4(),
                    tenant_id=tid,
                    customer_id=customers[spec.client].id,
                    number=f"{year}-{seq:06d}",
                    title=spec.title,
                    status=spec.status,
                    created_by_contact_id=author.id if spec.by_contact else None,
                    created_by_user_id=None if spec.by_contact else planner.id,
                    assigned_to_user_id=(foreman.id if rank is not None and rank >= 3 else None),
                    requested_delivery_at=(
                        created_day + timedelta(days=spec.requested_in)
                        if spec.requested_in is not None
                        else None
                    ),
                    promised_delivery_at=(
                        created_day + timedelta(days=spec.promised_in)
                        if spec.promised_in is not None
                        else None
                    ),
                    # As the app does: add_item keeps the running total cached
                    # (F-14), and confirming snapshots it (LOGIC-2).
                    quoted_total=total if lines else None,
                    quoted_at=(
                        step_at(OrderStatus.QUOTED) if rank is not None and rank >= 2 else None
                    ),
                    confirmed_total=(total if rank is not None and rank >= 3 else None),
                    confirmed_at=(
                        step_at(OrderStatus.CONFIRMED) if rank is not None and rank >= 3 else None
                    ),
                    confirmed_by_contact_id=(author.id if rank is not None and rank >= 3 else None),
                    currency="CZK",
                    submitted_at=(
                        step_at(OrderStatus.SUBMITTED) if spec.status != OrderStatus.DRAFT else None
                    ),
                    delivered_at=(
                        min(
                            created_day + timedelta(days=(spec.promised_in or 14) - 1),
                            day0,
                        )
                        if rank is not None and rank >= PIPELINE.index(OrderStatus.DELIVERED)
                        else None
                    ),
                    closed_at=(
                        at(max(spec.age_days - 25, 0))
                        if spec.status == OrderStatus.CLOSED
                        else None
                    ),
                    cancelled_at=(
                        created + timedelta(days=2)
                        if spec.status == OrderStatus.CANCELLED
                        else None
                    ),
                    created_at=created,
                )
                session.add(order)
                await session.flush()
                for line in lines:
                    line.order_id = order.id
                session.add_all(lines)

                # Status history along the pipeline.
                prev: OrderStatus | None = None
                for step, status in enumerate(path):
                    # The client drafts, submits and cancels its own orders;
                    # everything else is the shop moving the order along.
                    by_contact = spec.by_contact and status in (
                        OrderStatus.DRAFT,
                        OrderStatus.SUBMITTED,
                        OrderStatus.CANCELLED,
                    )
                    session.add(
                        OrderStatusHistory(
                            id=uuid4(),
                            tenant_id=tid,
                            order_id=order.id,
                            from_status=prev,
                            to_status=status,
                            changed_by_contact_id=author.id if by_contact else None,
                            changed_by_user_id=None if by_contact else owner.id,
                            created_at=created + gap * step,
                        )
                    )
                    if prev is not None:
                        # The same trail the app writes, so the dashboard's
                        # "Recent activity" and the audit log are not empty.
                        audit.append(
                            _audit(
                                tid,
                                created + gap * step,
                                author if by_contact else owner,
                                "order.status_changed",
                                order,
                                before={"status": prev.value},
                                after={"status": status.value},
                            )
                        )
                    prev = status

                for n, (kind, body) in enumerate(spec.comments):
                    comment = OrderComment(
                        id=uuid4(),
                        tenant_id=tid,
                        order_id=order.id,
                        author_contact_id=author.id if kind == "contact" else None,
                        author_user_id=None if kind == "contact" else planner.id,
                        body=body,
                        is_internal=kind == "internal",
                        created_at=min(
                            created + timedelta(hours=3 + n * 18),
                            now - timedelta(minutes=10 * (len(spec.comments) - n)),
                        ),
                    )
                    session.add(comment)
                    audit.append(
                        _audit(
                            tid,
                            comment.created_at,
                            author if kind == "contact" else planner,
                            "order.comment_added",
                            order,
                            after={
                                "comment_id": str(comment.id),
                                "is_internal": comment.is_internal,
                                "body": body,
                            },
                        )
                    )
                created_orders.append(order)
                orders_by_title[spec.title] = order
                authors[order.id] = author
            await session.flush()

            # ---- drawings (S3). A failure costs the drawings, not the seed.
            if s3_ok:
                try:
                    attachments = await _upload_drawings(tenant, orders_by_title, authors)
                except Exception as exc:
                    attachments = []
                    log.warning(
                        "demo_seed.drawings_failed",
                        slug=slug,
                        error=f"{type(exc).__name__}: {exc}"[:200],
                    )
                for att in attachments:
                    new_keys.add(att.storage_key)
                    if att.thumbnail_key:
                        new_keys.add(att.thumbnail_key)
                session.add_all(attachments)
                contact_by_id = {c.id: c for group in contacts.values() for c in group}
                for att in attachments:
                    uploader = contact_by_id[att.uploaded_by_contact_id]  # type: ignore[index]
                    audit.append(
                        AuditEvent(
                            id=uuid4(),
                            tenant_id=tid,
                            occurred_at=att.created_at,
                            created_at=att.created_at,
                            actor_type="contact",
                            actor_id=uploader.id,
                            actor_label=uploader.full_name,
                            action="attachment.upload",
                            entity_type="attachment",
                            entity_id=att.id,
                            entity_label=att.filename,
                            diff={
                                "before": None,
                                "after": {
                                    "order_id": str(att.order_id),
                                    "size_bytes": att.size_bytes,
                                    "content_type": att.content_type,
                                },
                            },
                        )
                    )
            session.add_all(audit)

            # ---- client-owned material
            first_order_by_client: dict[UUID, Order] = {}
            for order in created_orders:
                first_order_by_client.setdefault(order.customer_id, order)
            for spec_a in ASSETS:
                rec, con, ret = (
                    Decimal(spec_a.received),
                    Decimal(spec_a.consumed),
                    Decimal(spec_a.returned),
                )
                asset = Asset(
                    id=uuid4(),
                    tenant_id=tid,
                    customer_id=customers[spec_a.client].id,
                    code=spec_a.code,
                    name=spec_a.name,
                    unit=spec_a.unit,
                    current_quantity=rec - con - ret,
                    location=spec_a.location,
                )
                session.add(asset)
                await session.flush()
                session.add(
                    AssetMovement(
                        id=uuid4(),
                        tenant_id=tid,
                        asset_id=asset.id,
                        type=AssetMovementType.RECEIVE,
                        quantity=rec,
                        note="Příjem materiálu od zákazníka",
                        occurred_at=at(50),
                        created_by_user_id=foreman.id,
                    )
                )
                if con:
                    ref = first_order_by_client.get(customers[spec_a.client].id)
                    session.add(
                        AssetMovement(
                            id=uuid4(),
                            tenant_id=tid,
                            asset_id=asset.id,
                            type=AssetMovementType.CONSUME,
                            quantity=-con,
                            note="Spotřeba ve výrobě",
                            reference_order_id=ref.id if ref else None,
                            occurred_at=at(20),
                            created_by_user_id=foreman.id,
                        )
                    )
                if ret:
                    session.add(
                        AssetMovement(
                            id=uuid4(),
                            tenant_id=tid,
                            asset_id=asset.id,
                            type=AssetMovementType.ISSUE,
                            quantity=-ret,
                            note="Vrácení zbytků zákazníkovi",
                            occurred_at=at(6),
                            created_by_user_id=foreman.id,
                        )
                    )

            result = SeedResult(
                tenant_id=tid,
                slug=slug,
                staff_email=owner.email,
                contact_email=contacts["ukazkova"][0].email,
                password=password,
                customers=len(customers),
                contacts=sum(len(v) for v in contacts.values()),
                products=len(products),
                orders=len(created_orders),
                assets=len(ASSETS),
                attachments=len(attachments),
            )
    finally:
        if own_engine:
            await engine.dispose()

    # The DB now points only at the new objects: drop everything else under
    # the tenant prefix — earlier seeds' drawings and whatever visitors of
    # the public demo uploaded (attachments + thumbnails).
    stale = [k for k in old_keys if k not in new_keys]
    if s3_ok and stale:
        from app.storage import s3 as s3_storage

        try:
            result.s3_objects_deleted = await s3_storage.delete_objects_async(stale)
        except Exception as exc:
            # The orphan sweep in the retention job catches what we miss.
            log.warning(
                "demo_seed.s3_cleanup_failed",
                slug=slug,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
    return result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the sales-demo tenant (idempotent).")
    parser.add_argument("--slug", default="demo", help="tenant slug / subdomain (default: demo)")
    parser.add_argument(
        "--password",
        default=None,
        help="password for every demo login (default: a random one, printed at the end)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="wipe and reuse an existing tenant with this slug even if seed_demo did not create it",
    )
    parser.add_argument(
        "--no-files",
        action="store_true",
        help="skip S3 entirely: no sample drawings, no clean-up of the tenant's stored files",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    result = asyncio.run(
        seed_demo(
            slug=args.slug,
            password=args.password,
            force=args.force,
            files=not args.no_files,
        )
    )
    print(f"Demo tenant '{result.slug}' seeded ({TENANT_NAME}).")
    print(
        f"  {result.customers} clients, {result.contacts} contacts, {result.products} products, "
        f"{result.orders} orders, {result.assets} material stock items, "
        f"{result.attachments} drawings ({result.s3_objects_deleted} old files removed)"
    )
    print(f"  Staff login:   {result.staff_email}")
    print(f"  Client login:  {result.contact_email}")
    print(f"  Password:      {result.password}")


if __name__ == "__main__":
    main()
