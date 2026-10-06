"""Seed a realistic *sales demo* tenant: a Czech sheet-metal / CNC job shop.

Built for showing the product to prospects (MSV Brno and similar) and for
the public demo (``PUBLIC_DEMO_TENANT``, re-seeded nightly by
:mod:`app.tasks.demo_reset`): six clients, their contacts, a priced
catalogue, 27 orders spread over every status with line items, comments,
requested and promised dates (one in production past its promised date,
quotes waiting for the client's confirmation), client-owned material with
receive / consume / return movements tied to the orders that used it, and
fictional drawings (PDF, plus two PNG "3D previews") with an
"UKÁZKA / SAMPLE" watermark, rendered here with thumbnails.

**Plausibility rules** (a skeptical job-shop owner clicks through it for
two minutes — see the 2026-10-06 demo review):

* Every timestamp falls on a Czech working day between 07:00 and 16:30
  Europe/Prague, with natural minutes. Time is counted in *working days*
  back from the last full working day (:func:`anchor_day`), so a reset at
  02:30 stamps nothing at night and nothing "yesterday 23:30".
* Each order is a timeline of status steps, comments and uploads. Every
  comment names the status it is about (``Comment.anchor``) and comes
  after it; dates quoted in a comment are rendered from the order's own
  requested / promised dates, so they never contradict them.
* Clients submit and confirm their orders and cancel their own; the
  planner quotes, the foreman starts production and marks it ready,
  dispatch delivers, the owner closes.
* Prices are illustrative CZK excl. VAT at Czech job-shop levels; a
  finish (paint, zinc) is either part of the item name or a separate
  line, never both.

Every company and person below is **fictional** — the company names carry
"Ukázková / Vzorová / Příkladná / Modelová / Fiktivní / Demo" on purpose,
and every email address is under ``example.com`` (RFC 2606, reserved for
documentation). No IČO / DIČ is set, because a random 8-digit IČO can
belong to a real company.

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
# multiplication sign on purpose (product names like "600×400, RAL 7035"
# written the Czech way).
# ruff: noqa: RUF001, RUF003

from __future__ import annotations

import argparse
import asyncio
import random
import secrets
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import lru_cache
from itertools import pairwise
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import anyio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.demo import DEMO_MARKER
from app.demo.drawings import (
    SampleDrawing,
    SamplePreview,
    render_drawing_pdf,
    render_preview_png,
)
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

# --------------------------------------------------------------------------
# Showcase: what the demo's "where to start" card points at. Looked up by
# title / code (:func:`find_showcase`), never by a hard-coded id — the ids
# change every night.
# --------------------------------------------------------------------------

#: The clean quote awaiting the client's confirmation: drawing + 3D
#: preview, promised date, ~51 000 Kč, the most recently quoted order.
FLAGSHIP_ORDER_TITLE = "Kryty převodovky K-07 – rev. C"
#: In production, promised two working days before the last working day,
#: with a recent apology and the client's reply.
OVERDUE_ORDER_TITLE = "Upínací desky UD-400 – 4 ks"
#: The client material with the richest movement history (the demo
#: customer's own sheet stock).
SHOWCASE_MATERIAL_CODE = "UK-S235-3"

# --------------------------------------------------------------------------
# Working-day clock (Europe/Prague)
# --------------------------------------------------------------------------

PRAGUE = ZoneInfo("Europe/Prague")
DAY_START = time(7, 0)
DAY_END = time(16, 30)
_DAY_MINUTES = 9 * 60 + 30  # 07:00 – 16:30


def _easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=16)
def czech_holidays(year: int) -> frozenset[date]:
    """Czech public holidays (zákon č. 245/2000 Sb.) — no work, no deliveries."""
    easter = _easter_sunday(year)
    fixed = ((1, 1), (5, 1), (5, 8), (7, 5), (7, 6), (9, 28), (10, 28), (11, 17))
    xmas = ((12, 24), (12, 25), (12, 26))
    days = {date(year, m, d) for m, d in (*fixed, *xmas)}
    days |= {easter - timedelta(days=2), easter + timedelta(days=1)}
    return frozenset(days)


def is_working_day(d: date) -> bool:
    return d.weekday() < 5 and d not in czech_holidays(d.year)


def shift_working_days(d: date, n: int) -> date:
    """``d`` moved by ``n`` working days (negative = back). ``n == 0`` is ``d``."""
    step = 1 if n > 0 else -1
    while n:
        d += timedelta(days=step)
        if is_working_day(d):
            n -= step
    return d


def anchor_day(now: datetime) -> date:
    """The last *complete* working day before ``now`` (Europe/Prague).

    Everything in the demo is stamped relative to it, between 07:00 and
    16:30, so nothing is ever in the future: the nightly reset (02:30)
    builds on yesterday's (or Friday's) business hours, and a seed run in
    the middle of a working day does not borrow the afternoon.
    """
    local = now.astimezone(PRAGUE)
    if is_working_day(local.date()) and local.time() >= DAY_END:
        return local.date()
    return shift_working_days(local.date(), -1)


def _cz_date(d: date) -> str:
    """Czech short date as people write it in a message: ``13. 10.``"""
    return f"{d.day}. {d.month}."


class _WorkdayDate:
    """``"{d:+2}"`` in a comment = the anchor day shifted by 2 working days."""

    def __init__(self, anchor: date) -> None:
        self.anchor = anchor

    def __format__(self, spec: str) -> str:
        return _cz_date(shift_working_days(self.anchor, int(spec or 0)))


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StaffSpec:
    key: str  # how timelines refer to them
    local: str  # e-mail local part
    name: str
    role: UserRole
    login_wd: int  # last sign-in, working days before the anchor day


STAFF: list[StaffSpec] = [
    StaffSpec("owner", STAFF_LOCAL, "Pavel Horák", UserRole.TENANT_ADMIN, 0),
    StaffSpec("planner", "planovani", "Lenka Dvořáková", UserRole.TENANT_STAFF, 0),
    StaffSpec("foreman", "dilna", "Tomáš Kučera", UserRole.TENANT_STAFF, 0),
]
_STAFF_KEYS = {s.key for s in STAFF}


@dataclass(frozen=True)
class PersonSpec:
    local: str  # e-mail local part, also how timelines refer to them
    name: str
    admin: bool = False
    login_wd: int | None = None  # a sign-in on top of the ones their actions imply
    pending: bool = False  # invited a few days ago, not accepted yet


# key -> (name, mail domain, people)
CUSTOMERS: dict[str, tuple[str, str, list[PersonSpec]]] = {
    "ukazkova": (
        "Strojírna Ukázková s.r.o.",
        CONTACT_DOMAIN,
        [
            PersonSpec(CONTACT_LOCAL, "Jana Procházková", admin=True, login_wd=0),
            PersonSpec("konstrukce", "Martin Novotný", login_wd=1),
        ],
    ),
    "vzorova": (
        "Kovovýroba Vzorová a.s.",
        "vzorova",
        [
            PersonSpec("zasobovani", "Petr Svoboda", admin=True, login_wd=0),
            PersonSpec("kvalita", "Eva Marešová", pending=True),
        ],
    ),
    "prikladna": (
        "Zemědělská technika Příkladná s.r.o.",
        "prikladna",
        [PersonSpec("objednavky", "Josef Kratochvíl", admin=True, login_wd=2)],
    ),
    "fiktivni": (
        "Elektro Fiktivní spol. s r.o.",
        "fiktivni",
        [
            PersonSpec("vyroba", "Ivana Benešová", admin=True, login_wd=1),
            PersonSpec("technolog", "Karel Pokorný", pending=True),
        ],
    ),
    "modelova": (
        "Nábytek Modelový s.r.o.",
        "modelovy",
        [PersonSpec("majitel", "Radek Veselý", admin=True, login_wd=3)],
    ),
    "demo_auto": (
        "Automotive Demo Komponenty s.r.o.",
        "demo-komponenty",
        [
            PersonSpec("nakup", "Lucie Marková", admin=True, login_wd=0),
            PersonSpec("sqe", "Ondřej Fiala", login_wd=1),
            PersonSpec("logistika", "Hana Krejčí"),
        ],
    ),
}

#: The shop and its clients went live this many working days ago.
ONBOARDED_WD = 62

# --------------------------------------------------------------------------
# Catalogue — Czech job-shop prices, CZK excl. VAT
# --------------------------------------------------------------------------

# sku -> (name, unit, price, client key or None for the shared catalogue)
PRODUCTS: dict[str, tuple[str, str, str, str | None]] = {
    "LAS-S235-3": ("Laserové řezání – ocel S235 3 mm", "m", "18.00", None),
    "LAS-S235-6": ("Laserové řezání – ocel S235 6 mm", "m", "32.00", None),
    "LAS-TENKY": ("Laserové řezání – plech do 2 mm (ocel, Al)", "m", "14.00", None),
    "LAS-NEREZ-2": ("Laserové řezání – nerez 1.4301 2 mm", "m", "30.00", None),
    "OHYB": ("Ohraňování – jeden ohyb", "ks", "12.00", None),
    "LIS-MATICE": ("Zalisování matice M4–M8 vč. matice", "ks", "6.50", None),
    "CNC-FREZ": ("CNC frézování – strojní hodina", "hod", "980.00", None),
    "CNC-SOUST": ("CNC soustružení – strojní hodina", "hod", "850.00", None),
    "SVAR-MAG": ("Svařování MIG/MAG – hodina", "hod", "690.00", None),
    "SVAR-TIG": ("Svařování TIG nerez – hodina", "hod", "820.00", None),
    "ODJEHL": ("Odjehlení a sražení hran", "ks", "8.00", None),
    "LAK-PRASK": ("Práškové lakování dle RAL", "m²", "260.00", None),
    "ZINEK": ("Žárové zinkování (kooperace)", "kg", "26.00", None),
    "MAT-S235-3": ("Plech S235JR 3 mm – materiál", "kg", "32.00", None),
    "MAT-S235-6": ("Plech S235JR 6 mm – materiál", "kg", "30.00", None),
    "MAT-ALMG3-2": ("Plech AlMg3 2 mm – materiál", "kg", "115.00", None),
    "MAT-1.4301-2": ("Plech nerez 1.4301 2 mm – materiál", "kg", "125.00", None),
    "KONTROLA": ("Výstupní kontrola + měřicí protokol", "ks", "650.00", None),
    # Client parts. Made from the client's own material where they keep
    # stock with us (see ASSETS), so the piece price is labour only.
    "UK-KM120": ("Konzole motoru KM-120 dle výkresu", "ks", "46.00", "ukazkova"),
    "UK-KRYT-07": ("Kryt převodovky K-07 dle výkresu", "ks", "245.00", "ukazkova"),
    "VZ-PRIR-30": ("Příruba P30, soustružená", "ks", "118.00", "vzorova"),
    "PR-DRZAK-H": ("Držák hydrauliky H-4 dle výkresu", "ks", "48.00", "prikladna"),
    "FI-ROZV-600": ("Skříň rozvaděče 600×400×250, lak RAL 7035", "ks", "2860.00", "fiktivni"),
    "AU-PLECH-B2": ("Výztuha B2 – sériový díl", "ks", "16.80", "demo_auto"),
}

# --------------------------------------------------------------------------
# Files: fictional drawings and previews, uploaded by ``Upload`` events
# --------------------------------------------------------------------------

_UK = "Strojírna Ukázková s.r.o."
FILES: dict[str, SampleDrawing | SamplePreview] = {
    "km120_a": SampleDrawing(
        filename="KM-120_konzole_rev-A.pdf",
        number="KM-120",
        title="Konzole motoru KM-120",
        revision="A",
        material="S235JR, plech t = 3 mm",
        client=_UK,
        scale="1:2",
        shape="bracket",
        notes=("Povrch: bez úpravy, naolejovat",),
    ),
    "km120_b": SampleDrawing(
        filename="KM-120_konzole_rev-B.pdf",
        number="KM-120",
        title="Konzole motoru KM-120",
        revision="B",
        material="S235JR, plech t = 3 mm",
        client=_UK,
        scale="1:2",
        shape="bracket",
        notes=("Povrch: bez úpravy, naolejovat", "Rev. B: otvor ø9 posunut o 5 mm"),
    ),
    "k07": SampleDrawing(
        filename="K-07_kryt_prevodovky_rev-C.pdf",
        number="K-07",
        title="Kryt převodovky K-07",
        revision="C",
        material="DC01, plech t = 2 mm",
        client=_UK,
        scale="1:4",
        shape="cover",
        notes=("Povrch: práškový lak RAL 5010, jemná struktura", "4× matice M6 zalisovat"),
    ),
    "k07_3d": SamplePreview(
        filename="K-07_kryt_3D-nahled.png",
        number="K-07",
        title="Kryt převodovky K-07",
        client=_UK,
        shape="cover",
    ),
    "rs01": SampleDrawing(
        filename="RS-01_ram_stojanu_rev-A.pdf",
        number="RS-01",
        title="Rám stojanu – prototyp",
        revision="A",
        material="Jäkl 40×40×3, S235JR",
        client=_UK,
        scale="1:10",
        shape="frame",
        notes=("Svary dle ČSN EN ISO 5817 C",),
    ),
    "dc60": SampleDrawing(
        filename="DC-60_drzak_cidla_rev-A.pdf",
        number="DC-60",
        title="Držák čidla DC-60",
        revision="A",
        material="S235JR, plech t = 3 mm",
        client=_UK,
        scale="1:1",
        shape="bracket",
    ),
    "ud400": SampleDrawing(
        filename="UD-400_upinaci_deska_rev-B.pdf",
        number="UD-400",
        title="Upínací deska UD-400",
        revision="B",
        material="S355J2, výpalek t = 35 mm",
        client="Kovovýroba Vzorová a.s.",
        scale="1:4",
        shape="plate",
    ),
    "p30": SampleDrawing(
        filename="P30_priruba_rev-D.pdf",
        number="P30",
        title="Příruba P30",
        revision="D",
        material="C45, kulatina ø60",
        client="Kovovýroba Vzorová a.s.",
        scale="1:1",
        shape="flange",
    ),
    "h4": SampleDrawing(
        filename="H-4_drzak_hydrauliky_rev-A.pdf",
        number="H-4",
        title="Držák hydrauliky H-4",
        revision="A",
        material="Pásovina 50×6, S235JR",
        client="Zemědělská technika Příkladná s.r.o.",
        scale="1:2",
        shape="bracket",
        notes=("Povrch: žárový zinek",),
    ),
    "ss2": SampleDrawing(
        filename="SS-2_ram_seciho_stroje_rev-A.pdf",
        number="SS-2",
        title="Rám secího stroje",
        revision="A",
        material="S235JR, plech t = 6 mm + jäkl 40×40×3",
        client="Zemědělská technika Příkladná s.r.o.",
        scale="1:10",
        shape="frame",
        notes=("Povrch: práškový lak RAL 6011",),
    ),
    "rozv": SampleDrawing(
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
    "mp60": SampleDrawing(
        filename="MP-60_montazni_panel_rev-A.pdf",
        number="MP-60",
        title="Montážní panel MP-60",
        revision="A",
        material="DC01, plech t = 1,5 mm",
        client="Elektro Fiktivní spol. s r.o.",
        scale="1:4",
        shape="plate",
    ),
    "pk_b2": SampleDrawing(
        filename="PK-B2_kontrolni_pripravek_rev-A.pdf",
        number="PK-B2",
        title="Kontrolní přípravek dílu B2",
        revision="A",
        material="EN AW-5083, deska t = 40 mm",
        client="Automotive Demo Komponenty s.r.o.",
        scale="1:2",
        shape="plate",
    ),
    "b3": SampleDrawing(
        filename="B3_vyztuha_rev-A.pdf",
        number="B3",
        title="Výztuha B3",
        revision="A",
        material="DX51D+Z, plech t = 2 mm",
        client="Automotive Demo Komponenty s.r.o.",
        scale="1:1",
        shape="bracket",
    ),
    "b3_3d": SamplePreview(
        filename="B3_vyztuha_3D-nahled.png",
        number="B3",
        title="Výztuha B3",
        client="Automotive Demo Komponenty s.r.o.",
        shape="bracket",
    ),
}

# --------------------------------------------------------------------------
# Orders: a timeline per order
# --------------------------------------------------------------------------

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
class Item:
    """One line. ``desc`` says what the line covers (how many parts an hour
    line is for); ``price`` overrides the catalogue price."""

    sku: str | None
    qty: str
    desc: str | None = None
    price: str | None = None
    unit: str | None = None


@dataclass(frozen=True)
class Step:
    """The order moved to ``status`` ``wd`` working days before the anchor day."""

    status: OrderStatus
    wd: int
    by: str
    note: str | None = None


@dataclass(frozen=True)
class Comment:
    """A comment about the ``anchor`` status — always written after it."""

    anchor: OrderStatus
    wd: int
    by: str
    body: str
    internal: bool = False


@dataclass(frozen=True)
class Upload:
    file: str  # key into FILES
    wd: int
    by: str


Event = Step | Comment | Upload

S, C, U = Step, Comment, Upload
_D, _SUB, _Q, _CON = (
    OrderStatus.DRAFT,
    OrderStatus.SUBMITTED,
    OrderStatus.QUOTED,
    OrderStatus.CONFIRMED,
)
_IP, _RDY, _DEL, _CLO, _CAN = (
    OrderStatus.IN_PRODUCTION,
    OrderStatus.READY,
    OrderStatus.DELIVERED,
    OrderStatus.CLOSED,
    OrderStatus.CANCELLED,
)


@dataclass(frozen=True)
class OrderSpec:
    client: str
    title: str
    items: tuple[Item, ...]
    #: Chronological: working days never increase along it. Starts with
    #: the DRAFT step; the last step is the order's current status.
    timeline: tuple[Event, ...]
    requested: int | None = None  # working days from the anchor day (negative = past)
    promised: int | None = None

    @property
    def steps(self) -> list[Step]:
        return [e for e in self.timeline if isinstance(e, Step)]

    @property
    def status(self) -> OrderStatus:
        return self.steps[-1].status


ORDERS: list[OrderSpec] = [
    # ---------------------------------------------------- Strojírna Ukázková
    # (the public demo's customer persona: one order in every state)
    OrderSpec(
        "ukazkova",
        "Konzole KM-120 – série 150 ks",
        (Item("UK-KM120", "150"), Item("KONTROLA", "1")),
        (
            S(_D, 46, "nakup"),
            U("km120_a", 46, "konstrukce"),
            S(_SUB, 46, "nakup"),
            S(_Q, 45, "planner"),
            S(_CON, 44, "nakup"),
            S(_IP, 40, "foreman"),
            S(_RDY, 37, "foreman"),
            S(_DEL, 36, "planner"),
            S(_CLO, 31, "owner"),
        ),
        requested=-36,
        promised=-36,
    ),
    OrderSpec(
        "ukazkova",
        "Stojany na palety – 4 ks",
        (
            Item("LAS-S235-6", "30", "Laserové řezání – patky a výztuhy, ocel 6 mm (4 stojany)"),
            Item("SVAR-MAG", "8", "Svařování MIG/MAG – 4 stojany, 2 h/ks"),
            Item("LAK-PRASK", "10", "Práškové lakování RAL 5012 (4 stojany × 2,5 m²)"),
        ),
        (
            S(_D, 42, "planner"),
            C(
                _D,
                42,
                "planner",
                "Poptávka telefonicky od paní Procházkové, jäkl 40×40×3 dodá zákazník.",
                internal=True,
            ),
            S(_Q, 41, "planner"),
            S(_CAN, 38, "nakup", note="Stojany nakonec kupujeme hotové."),
            C(
                _CAN,
                38,
                "nakup",
                "Stojany nakonec kupujeme hotové od dodavatele regálů, poptávku prosím "
                "stornujte. Jäkl si vyzvedneme při příští dodávce.",
            ),
        ),
        requested=-30,
        promised=-31,
    ),
    OrderSpec(
        "ukazkova",
        "Distanční podložky ø40 – 800 ks",
        (
            Item(
                "LAS-S235-3",
                "133",
                "Laserové řezání – podložky ø40/ø13, ocel 3 mm (800 ks × 0,166 m)",
            ),
            Item("ODJEHL", "800", "Omílání v bubnu (800 ks)", price="1.50"),
        ),
        (
            S(_D, 12, "nakup"),
            S(_SUB, 12, "nakup"),
            S(_Q, 11, "planner"),
            S(_CON, 11, "nakup"),
            S(_IP, 9, "foreman"),
            S(_RDY, 7, "foreman"),
            S(_DEL, 6, "planner"),
        ),
        requested=-6,
        promised=-6,
    ),
    OrderSpec(
        "ukazkova",
        "Konzole KM-120 – série 200 ks",
        (Item("UK-KM120", "200"), Item("KONTROLA", "1")),
        (
            S(_D, 12, "nakup"),
            U("km120_b", 12, "konstrukce"),
            S(_SUB, 12, "nakup"),
            C(
                _SUB,
                12,
                "nakup",
                "Prosíme o dodání do {requested}, montáž u nás začíná hned potom. "
                "Ve výkresu rev. B je posunutý otvor ø9 – vyrábějte prosím už jen podle něj.",
            ),
            S(_Q, 11, "planner"),
            S(_CON, 10, "nakup"),
            S(_IP, 4, "foreman"),
            C(
                _IP,
                1,
                "foreman",
                "Laser hotový, všech 200 ks jde na ohraňování. Expedice {promised} platí.",
            ),
        ),
        requested=5,
        promised=5,
    ),
    OrderSpec(
        "ukazkova",
        "Držáky čidel DC-60 – 60 ks",
        (
            Item("LAS-S235-3", "22", "Laserové řezání – ocel 3 mm (60 ks × 0,37 m)"),
            Item("OHYB", "120", "Ohraňování (60 ks × 2 ohyby)"),
            Item("ODJEHL", "60"),
        ),
        (
            S(_D, 9, "nakup"),
            U("dc60", 9, "nakup"),
            S(_SUB, 9, "nakup"),
            S(_Q, 8, "planner"),
            S(_CON, 8, "nakup"),
            S(_IP, 4, "foreman"),
            S(_RDY, 1, "foreman"),
            C(
                _RDY,
                1,
                "planner",
                "Hotovo, 60 ks zabaleno na paletě. K vyzvednutí na rampě 1 kdykoli od 7:00, "
                "jinak je přivezeme {promised} s ostatní dodávkou.",
            ),
        ),
        requested=2,
        promised=2,
    ),
    OrderSpec(
        "ukazkova",
        FLAGSHIP_ORDER_TITLE,
        (
            Item("UK-KRYT-07", "150"),
            Item("LIS-MATICE", "600", "Zalisování matic M6 (150 ks × 4)"),
            Item(
                "LAK-PRASK",
                "37.5",
                "Práškové lakování RAL 5010, jemná struktura (150 ks × 0,25 m²)",
            ),
            Item("KONTROLA", "1"),
        ),
        (
            S(_D, 3, "nakup"),
            U("k07", 3, "konstrukce"),
            U("k07_3d", 3, "konstrukce"),
            S(_SUB, 3, "nakup"),
            C(
                _SUB,
                3,
                "nakup",
                "Posíláme revizi C – oproti rev. B jsou jiné větrací drážky. Termín "
                "potřebujeme do {requested}, lak RAL 5010.",
            ),
            C(
                _SUB,
                2,
                "planner",
                "Lakovna má RAL 5010 jemnou strukturu skladem, kooperace 3 pracovní dny.",
                internal=True,
            ),
            S(_Q, 1, "planner"),
            C(
                _Q,
                1,
                "planner",
                "Nabídka podle výkresu rev. C je v položkách, lakování a zalisování matic "
                "zvlášť. Při potvrzení do 2 pracovních dnů garantujeme dodání {promised}, "
                "tedy den před vaším termínem.",
            ),
        ),
        requested=12,
        promised=11,
    ),
    OrderSpec(
        "ukazkova",
        "Rám stojanu – prototyp",
        (
            Item("LAS-S235-6", "42", "Laserové řezání – patky a výztuhy rámu, ocel 6 mm (1 ks)"),
            Item("OHYB", "16", "Ohraňování – výztuhy (8 ks × 2 ohyby)"),
            Item("SVAR-MAG", "6", "Svařování MIG/MAG – rám prototypu (1 ks)"),
        ),
        (
            S(_D, 1, "konstrukce"),
            U("rs01", 1, "konstrukce"),
            S(_SUB, 1, "konstrukce"),
            C(
                _SUB,
                1,
                "konstrukce",
                "Jäkl 40×40×3 vám přivezeme zítra. Jde o prototyp, po odzkoušení "
                "objednáme sérii 10 ks.",
            ),
        ),
        requested=9,
    ),
    OrderSpec(
        "ukazkova",
        "Výztuhy rámu RS-02",
        (
            Item("LAS-S235-6", "18", "Laserové řezání – výztuhy, ocel 6 mm (12 ks × 1,5 m)"),
            Item("OHYB", "24", "Ohraňování (12 ks × 2 ohyby)"),
        ),
        (S(_D, 0, "nakup"),),
    ),
    # ----------------------------------------------------- Kovovýroba Vzorová
    OrderSpec(
        "vzorova",
        "Pouzdra PB-20 – 120 ks",
        (Item("CNC-SOUST", "6", "CNC soustružení – pouzdro PB-20 (120 ks, 3 min/ks)"),),
        (
            S(_D, 31, "zasobovani"),
            S(_SUB, 31, "zasobovani"),
            S(_Q, 30, "planner"),
            S(_CAN, 27, "zasobovani", note="Projekt u koncového zákazníka odložen."),
            C(
                _CAN,
                27,
                "zasobovani",
                "Koncový zákazník projekt odložil, nabídku prosím stornujte. Kulatinu ø60 "
                "si u vás nechte, použijeme ji na příruby.",
            ),
        ),
        requested=-20,
        promised=-20,
    ),
    OrderSpec(
        "vzorova",
        OVERDUE_ORDER_TITLE,
        (
            Item("CNC-FREZ", "9", "CNC frézování – deska 400×300×30 (4 ks, 2,25 h/ks)"),
            Item(None, "4", "Materiál S355 – výpalek 410×310×35", price="1450.00", unit="ks"),
            Item("ODJEHL", "4"),
        ),
        (
            S(_D, 19, "zasobovani"),
            U("ud400", 19, "zasobovani"),
            S(_SUB, 19, "zasobovani"),
            S(_Q, 18, "planner"),
            S(_CON, 17, "zasobovani"),
            S(_IP, 7, "foreman"),
            C(
                _IP,
                3,
                "foreman",
                "Frézka stojí – vadné ložisko vřetene, servis přijede zítra. "
                "Dvě desky jsou hotové.",
                internal=True,
            ),
            C(
                _IP,
                1,
                "owner",
                "Omlouváme se, kvůli poruše frézky jsme nestihli slíbený termín ({promised}). "
                "Dvě desky jsou hotové, zbylé dvě dokončíme do {d:+2} – hotové kusy můžeme "
                "poslat hned.",
            ),
            C(
                _IP,
                0,
                "zasobovani",
                "Díky za info. Pošlete prosím ty dvě hotové hned, zbytek do {d:+2} nám stačí.",
            ),
        ),
        requested=-2,
        promised=-2,
    ),
    OrderSpec(
        "vzorova",
        "Hřídele ø40 – 30 ks",
        (
            Item("CNC-SOUST", "14", "CNC soustružení – hřídel ø40×320 (30 ks, 28 min/ks)"),
            Item(None, "10", "Materiál C45 ø45 – tyč (30 ks × 0,33 m)", price="210.00", unit="m"),
            Item("KONTROLA", "1"),
        ),
        (
            S(_D, 15, "zasobovani"),
            S(_SUB, 15, "zasobovani"),
            S(_Q, 14, "planner"),
            S(_CON, 13, "zasobovani"),
            S(_IP, 8, "foreman"),
            S(_RDY, 2, "foreman"),
            C(
                _RDY,
                2,
                "planner",
                "Hotovo, 30 ks připraveno k odběru na rampě 2. Měřicí protokol posíláme "
                "s dodacím listem.",
            ),
        ),
        requested=3,
        promised=3,
    ),
    OrderSpec(
        "vzorova",
        "Příruby P30 – 500 ks",
        (
            Item("VZ-PRIR-30", "500"),
            Item("KONTROLA", "5", "Měřicí protokol ke každé dávce 100 ks (5 dávek)"),
        ),
        (
            S(_D, 6, "zasobovani"),
            U("p30", 6, "zasobovani"),
            S(_SUB, 6, "zasobovani"),
            C(
                _SUB,
                6,
                "zasobovani",
                "Kulatinu C45 ø60 doplníme – 18 m přivezeme hned po potvrzení. Prosíme "
                "o měřicí protokol ke každé dávce.",
            ),
            S(_Q, 5, "planner"),
            C(
                _Q,
                5,
                "planner",
                "Pozor: protokol ke každé dávce 100 ks, ne jen k celé zakázce – v nabídce "
                "započteno 5×.",
                internal=True,
            ),
            S(_CON, 4, "zasobovani"),
            C(
                _CON,
                3,
                "foreman",
                "Kulatina převzata (18 m). Výrobu máme naplánovanou tak, aby {promised} platilo.",
            ),
            S(_IP, 2, "foreman"),
        ),
        requested=12,
        promised=12,
    ),
    # ------------------------------------------- Zemědělská technika Příkladná
    OrderSpec(
        "prikladna",
        "Plechové kryty řemenů – 40 ks",
        (
            Item("LAS-S235-3", "80", "Laserové řezání – ocel 3 mm (40 ks × 2 m)"),
            Item("OHYB", "160", "Ohraňování (40 ks × 4 ohyby)"),
            Item("MAT-S235-3", "125", "Plech S235JR 3 mm – materiál (40 ks × 3,1 kg)"),
        ),
        (
            S(_D, 21, "objednavky"),
            S(_SUB, 21, "objednavky"),
            S(_Q, 20, "planner"),
            S(_CON, 20, "objednavky"),
            S(_IP, 14, "foreman"),
            C(
                _IP,
                10,
                "planner",
                "Omlouváme se, ohraňovací lis čeká na náhradní díl. Kryty dodáme {delivered}, "
                "o dva dny později.",
            ),
            S(_RDY, 8, "foreman"),
            S(_DEL, 7, "planner"),
            C(_DEL, 6, "objednavky", "Kryty převzaty, vše v pořádku. Díky."),
        ),
        requested=-9,
        promised=-9,
    ),
    OrderSpec(
        "prikladna",
        "Držáky hydrauliky H-4 – 120 ks",
        (
            Item("PR-DRZAK-H", "120"),
            Item("ZINEK", "75", "Žárové zinkování (kooperace) – 120 ks × 0,62 kg"),
        ),
        (
            S(_D, 9, "objednavky"),
            U("h4", 9, "objednavky"),
            S(_SUB, 9, "objednavky"),
            C(
                _SUB,
                9,
                "objednavky",
                "Pásovinu 50×6 přivezeme po potvrzení objednávky, 8 tyčí po 6 m.",
            ),
            S(_Q, 8, "planner"),
            S(_CON, 7, "objednavky"),
            S(_IP, 4, "foreman"),
            C(
                _IP,
                2,
                "planner",
                "Díly jsou nařezané a ohnuté, zítra odjíždějí do zinkovny. Zinkování trvá "
                "zhruba 4 pracovní dny, termín {promised} držíme.",
            ),
        ),
        requested=8,
        promised=7,
    ),
    OrderSpec(
        "prikladna",
        "Svařenec rámu secího stroje – 2 ks",
        (
            Item("LAS-S235-6", "65", "Laserové řezání – díly rámu, ocel 6 mm (2 rámy)"),
            Item("MAT-S235-6", "186", "Plech S235JR 6 mm – materiál (2 rámy × 93 kg)"),
            Item("OHYB", "24", "Ohraňování (2 rámy × 12 ohybů)"),
            Item("SVAR-MAG", "11", "Svařování MIG/MAG – 2 rámy, 5,5 h/ks"),
            Item("LAK-PRASK", "6", "Práškové lakování RAL 6011 (2 rámy × 3 m²)"),
        ),
        (
            S(_D, 7, "objednavky"),
            U("ss2", 7, "objednavky"),
            S(_SUB, 7, "objednavky"),
            S(_Q, 5, "planner"),
            C(
                _Q,
                5,
                "planner",
                "Nabídka platí 30 dní. Lakujeme v RAL 6011 podle vašeho standardu; "
                "při potvrzení do 5 pracovních dnů stihneme {promised} i s lakováním.",
            ),
            C(_Q, 2, "objednavky", "Čekáme na schválení rozpočtu, ozveme se během pár dní."),
        ),
        requested=16,
        promised=15,
    ),
    OrderSpec(
        "prikladna",
        "Náhradní díly na jarní sezónu",
        (Item("PR-DRZAK-H", "30"),),
        (S(_D, 1, "objednavky"),),
    ),
    # ---------------------------------------------------------- Elektro Fiktivní
    OrderSpec(
        "fiktivni",
        "Skříně rozvaděčů 600×400 – 8 ks",
        (Item("FI-ROZV-600", "8"),),
        (
            S(_D, 42, "vyroba"),
            S(_SUB, 42, "vyroba"),
            S(_Q, 41, "planner"),
            S(_CON, 40, "vyroba"),
            S(_IP, 36, "foreman"),
            S(_RDY, 31, "foreman"),
            S(_DEL, 30, "planner"),
            S(_CLO, 26, "owner"),
        ),
        requested=-30,
        promised=-30,
    ),
    OrderSpec(
        "fiktivni",
        "Úchyty DIN lišty – 400 ks",
        (
            Item("LAS-TENKY", "36", "Laserové řezání – DC01 1,5 mm (400 ks × 0,09 m)"),
            Item("OHYB", "400", "Ohraňování (400 ks × 1 ohyb)", price="4.50"),
            Item("ODJEHL", "400", "Omílání v bubnu (400 ks)", price="1.20"),
        ),
        (
            S(_D, 11, "vyroba"),
            S(_SUB, 11, "vyroba"),
            S(_Q, 10, "planner"),
            S(_CON, 10, "vyroba"),
            S(_IP, 6, "foreman"),
            S(_RDY, 4, "foreman"),
            S(_DEL, 3, "planner"),
        ),
        requested=-3,
        promised=-3,
    ),
    OrderSpec(
        "fiktivni",
        "Skříně rozvaděčů 600×400 – 12 ks",
        (Item("FI-ROZV-600", "12"),),
        (
            S(_D, 5, "vyroba"),
            U("rozv", 5, "vyroba"),
            S(_SUB, 5, "vyroba"),
            C(
                _SUB,
                5,
                "vyroba",
                "Prosím o potvrzení odstínu RAL 7035 – jemná, nebo hrubá struktura?",
            ),
            C(
                _SUB,
                4,
                "planner",
                "RAL 7035 jemná struktura, stejně jako minulá dávka. Nabídku posíláme.",
            ),
            S(_Q, 4, "planner"),
            S(_CON, 3, "vyroba"),
        ),
        requested=15,
        promised=14,
    ),
    OrderSpec(
        "fiktivni",
        "Montážní panely MP-60 – 60 ks",
        (
            Item("LAS-TENKY", "120", "Laserové řezání – DC01 1,5 mm (60 ks × 2 m)"),
            Item("OHYB", "240", "Ohraňování (60 ks × 4 ohyby)"),
            Item("LIS-MATICE", "240", "Zalisování matic M5 (60 ks × 4)"),
        ),
        (
            S(_D, 2, "vyroba"),
            U("mp60", 2, "vyroba"),
            S(_SUB, 2, "vyroba"),
            C(
                _SUB,
                2,
                "vyroba",
                "Panely prosím z našeho plechu DC01 1,5 mm, který u vás leží na skladě.",
            ),
        ),
        requested=10,
    ),
    # --------------------------------------------------------- Nábytek Modelový
    OrderSpec(
        "modelova",
        "Nerezové nohy stolů – vzorek 8 ks",
        (
            Item("LAS-NEREZ-2", "24", "Laserové řezání – nerez 2 mm (8 ks × 3 m)"),
            Item("MAT-1.4301-2", "18", "Plech nerez 1.4301 2 mm – materiál (8 ks × 2,2 kg)"),
            Item("SVAR-TIG", "5", "Svařování TIG – 8 nohou, cca 40 min/ks"),
            Item(None, "8", "Kartáčování K240 (8 ks)", price="120.00", unit="ks"),
        ),
        (
            S(_D, 8, "majitel"),
            S(_SUB, 8, "majitel"),
            C(
                _SUB,
                8,
                "majitel",
                "Šlo by to i v kartáčovaném provedení? Jde o vzorek na dva stoly, když "
                "projde, budeme objednávat po 40 ks.",
            ),
            C(
                _SUB,
                7,
                "planner",
                "Ano, kartáčování K240 přidáme za 120 Kč/ks, v nabídce je jako samostatný řádek.",
            ),
            S(_Q, 7, "planner"),
        ),
        requested=12,
        promised=10,
    ),
    OrderSpec(
        "modelova",
        "Konzole polic – hliník 40 ks",
        (
            Item("LAS-TENKY", "32", "Laserové řezání – AlMg3 2 mm (40 ks × 0,8 m)"),
            Item("MAT-ALMG3-2", "12", "Plech AlMg3 2 mm – materiál (40 ks × 0,3 kg)"),
            Item("OHYB", "80", "Ohraňování (40 ks × 2 ohyby)"),
        ),
        (
            S(_D, 1, "planner"),
            C(
                _D,
                1,
                "planner",
                "Pan Veselý volal – 40 konzolí do showroomu. Nabídku pošlu, až potvrdí rozměry.",
                internal=True,
            ),
        ),
    ),
    # --------------------------------------------- Automotive Demo Komponenty
    OrderSpec(
        "demo_auto",
        "Výztuha B2 – odvolávka č. 7",
        (Item("AU-PLECH-B2", "2000"),),
        (
            S(_D, 47, "nakup"),
            S(_SUB, 47, "nakup"),
            S(_Q, 46, "planner"),
            S(_CON, 46, "nakup"),
            S(_IP, 41, "foreman"),
            S(_RDY, 38, "foreman"),
            S(_DEL, 37, "planner"),
            S(_CLO, 33, "owner"),
        ),
        requested=-37,
        promised=-37,
    ),
    OrderSpec(
        "demo_auto",
        "Výztuha B2 – odvolávka č. 8",
        (
            Item("AU-PLECH-B2", "2400"),
            Item("KONTROLA", "1", "Měřicí protokol – 5 ks z dávky"),
        ),
        (
            S(_D, 25, "nakup"),
            S(_SUB, 25, "nakup"),
            S(_Q, 24, "planner"),
            S(_CON, 24, "nakup"),
            S(_IP, 19, "foreman"),
            S(_RDY, 15, "foreman"),
            S(_DEL, 14, "planner"),
            C(
                _DEL,
                14,
                "logistika",
                "Převzato, 2 400 ks. Dodací list s číslem odvolávky v pořádku.",
            ),
        ),
        requested=-14,
        promised=-14,
    ),
    OrderSpec(
        "demo_auto",
        "Kontrolní přípravek pro díl B2",
        (
            Item("CNC-FREZ", "16", "CNC frézování – kontrolní přípravek (1 ks)"),
            Item(None, "1", "Materiál – deska EN AW-5083 300×200×40", price="2400.00", unit="ks"),
            Item("KONTROLA", "1", "Protokol 3D měření přípravku", price="1800.00"),
        ),
        (
            S(_D, 10, "planner"),
            U("pk_b2", 10, "planner"),
            S(_Q, 10, "planner"),
            S(_CON, 8, "nakup"),
            C(
                _CON,
                8,
                "sqe",
                "Přípravek prosím včetně protokolu z 3D měření, budeme ho používat "
                "při přejímce B2.",
            ),
        ),
        requested=12,
        promised=11,
    ),
    OrderSpec(
        "demo_auto",
        "Výztuha B2 – odvolávka č. 9",
        (
            Item("AU-PLECH-B2", "2400"),
            Item("KONTROLA", "1", "Měřicí protokol – 5 ks z dávky"),
        ),
        (
            S(_D, 8, "nakup"),
            S(_SUB, 8, "nakup"),
            C(_SUB, 8, "nakup", "Dodací list prosím s číslem odvolávky."),
            S(_Q, 8, "planner"),
            S(_CON, 7, "nakup"),
            S(_IP, 3, "foreman"),
        ),
        requested=4,
        promised=4,
    ),
    OrderSpec(
        "demo_auto",
        "Vzorky nového dílu B3 (PPAP)",
        (
            Item("LAS-TENKY", "12", "Laserové řezání – vzorky B3, DX51D+Z 2 mm (50 ks × 0,24 m)"),
            Item("OHYB", "150", "Ohraňování (50 ks × 3 ohyby)"),
            Item("KONTROLA", "1", "Měřicí protokol PPAP – 5 ks", price="2400.00"),
        ),
        (
            S(_D, 2, "sqe"),
            U("b3", 2, "sqe"),
            U("b3_3d", 2, "sqe"),
            S(_SUB, 2, "sqe"),
            C(
                _SUB,
                2,
                "sqe",
                "Posíláme výkres rev. A a 3D náhled, potřebujeme 50 ks vzorků včetně PPAP "
                "úrovně 3. Materiál ze svitku DX51D+Z, který u vás máme.",
            ),
        ),
        requested=14,
    ),
]

# --------------------------------------------------------------------------
# Client-owned material: stock = sum of movements
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Move:
    type: AssetMovementType
    qty: str  # positive; the sign follows from the type
    wd: int
    note: str
    order: str | None = None  # title of the order it belongs to
    by: str = "foreman"


@dataclass(frozen=True)
class AssetSpec:
    client: str
    code: str
    name: str
    unit: str
    location: str
    moves: tuple[Move, ...] = field(default_factory=tuple)


_R, _CS, _I = AssetMovementType.RECEIVE, AssetMovementType.CONSUME, AssetMovementType.ISSUE

ASSETS: list[AssetSpec] = [
    AssetSpec(
        "ukazkova",
        SHOWCASE_MATERIAL_CODE,
        "Plech S235JR 3 mm – tabule 1500×3000",
        "kg",
        "Regál A1",
        (
            Move(_R, "636", 50, "Příjem 6 tabulí od zákazníka"),
            Move(
                _CS, "62", 39, "Výdej do výroby – KM-120, 150 ks", "Konzole KM-120 – série 150 ks"
            ),
            Move(_CS, "31", 8, "Výdej do výroby – podložky ø40", "Distanční podložky ø40 – 800 ks"),
            Move(_CS, "84", 3, "Výdej do výroby – KM-120, 200 ks", "Konzole KM-120 – série 200 ks"),
            Move(_CS, "18", 3, "Výdej do výroby – DC-60, 60 ks", "Držáky čidel DC-60 – 60 ks"),
        ),
    ),
    AssetSpec(
        "ukazkova",
        "UK-TR-40",
        "Jäkl 40×40×3",
        "m",
        "Stojan B2",
        (
            Move(_R, "36", 41, "Příjem 6 tyčí × 6 m k zakázce stojanů", "Stojany na palety – 4 ks"),
            Move(
                _I,
                "36",
                36,
                "Vráceno zákazníkovi po stornu zakázky",
                "Stojany na palety – 4 ks",
                by="planner",
            ),
            Move(_R, "24", 0, "Příjem 4 tyčí × 6 m k prototypu rámu", "Rám stojanu – prototyp"),
        ),
    ),
    AssetSpec(
        "vzorova",
        "VZ-KRUH-60",
        "Kulatina C45 ø60",
        "m",
        "Stojan C1",
        (
            Move(_R, "12", 30, "Příjem 4 tyčí × 3 m", "Pouzdra PB-20 – 120 ks"),
            Move(_R, "18", 3, "Příjem 6 tyčí × 3 m", "Příruby P30 – 500 ks"),
            Move(_CS, "6.4", 1, "Výdej do výroby – 1. a 2. dávka (200 ks)", "Příruby P30 – 500 ks"),
        ),
    ),
    AssetSpec(
        "prikladna",
        "PR-PAS-50",
        "Pásovina 50×6 S235JR",
        "m",
        "Stojan B4",
        (
            Move(_R, "18", 34, "Příjem 3 tyčí × 6 m na náhradní díly"),
            Move(_R, "48", 6, "Příjem 8 tyčí × 6 m", "Držáky hydrauliky H-4 – 120 ks"),
            Move(_CS, "32", 3, "Výdej do výroby – H-4, 120 ks", "Držáky hydrauliky H-4 – 120 ks"),
        ),
    ),
    AssetSpec(
        "fiktivni",
        "FI-DC01-15",
        "Plech DC01 1,5 mm – tabule 1250×2500",
        "kg",
        "Regál A3",
        (
            Move(_R, "800", 47, "Příjem 2 palet od zákazníka"),
            Move(
                _CS,
                "146",
                35,
                "Výdej do výroby – skříně 8 ks",
                "Skříně rozvaděčů 600×400 – 8 ks",
            ),
            Move(_CS, "14", 5, "Výdej do výroby – úchyty 400 ks", "Úchyty DIN lišty – 400 ks"),
            Move(_R, "400", 2, "Příjem 1 palety k zakázce", "Skříně rozvaděčů 600×400 – 12 ks"),
        ),
    ),
    AssetSpec(
        "demo_auto",
        "AU-DX51-2",
        "Svitek DX51D+Z 2 mm",
        "kg",
        "Hala 2",
        (
            Move(_R, "2500", 48, "Příjem svitku od zákazníka"),
            Move(_CS, "660", 40, "Výdej do výroby – B2, 2 000 ks", "Výztuha B2 – odvolávka č. 7"),
            Move(_CS, "792", 18, "Výdej do výroby – B2, 2 400 ks", "Výztuha B2 – odvolávka č. 8"),
            Move(_R, "2500", 16, "Příjem svitku od zákazníka"),
            Move(_CS, "790", 2, "Výdej do výroby – B2, 2 400 ks", "Výztuha B2 – odvolávka č. 9"),
        ),
    ),
]

_SIGN = {
    AssetMovementType.RECEIVE: 1,
    AssetMovementType.CONSUME: -1,
    AssetMovementType.ISSUE: -1,
    AssetMovementType.ADJUST: 1,
}

# --------------------------------------------------------------------------
# Planning: specs -> concrete dates and times (pure, no DB)
# --------------------------------------------------------------------------


@dataclass
class PlannedOrder:
    spec: OrderSpec
    times: list[datetime]  # aligned with spec.timeline, UTC
    requested: date | None
    promised: date | None
    number: str = ""

    def step_time(self, status: OrderStatus) -> datetime | None:
        for event, at in zip(self.spec.timeline, self.times, strict=True):
            if isinstance(event, Step) and event.status == status:
                return at
        return None

    @property
    def created(self) -> datetime:
        return self.times[0]

    @property
    def delivered(self) -> date | None:
        at = self.step_time(OrderStatus.DELIVERED)
        return at.astimezone(PRAGUE).date() if at else None


class DemoClock:
    """Turns "N working days back, k-th thing that day" into a timestamp."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.anchor = anchor_day(now)

    def day(self, wd: int) -> date:
        """The working day ``wd`` working days before the anchor (negative = after)."""
        return shift_working_days(self.anchor, -wd)

    def slots(self, wds: list[int], seed: str) -> list[datetime]:
        """One business-hours timestamp per entry, in order (``wds`` non-increasing).

        Events on the same day split the working day into equal slots and
        get a random minute (and second) inside their own slot, so they
        stay in order, are never closer than a few minutes, and never land
        on a suspiciously round time grid.
        """
        rng = random.Random(seed)
        out: list[datetime] = []
        i = 0
        while i < len(wds):
            j = i
            while j < len(wds) and wds[j] == wds[i]:
                j += 1
            k = j - i
            day = self.day(wds[i])
            start = datetime.combine(day, DAY_START, tzinfo=PRAGUE)
            for n in range(k):
                lo = n * _DAY_MINUTES // k + 4
                hi = (n + 1) * _DAY_MINUTES // k - 5
                at = start + timedelta(minutes=rng.randint(lo, hi), seconds=rng.randint(0, 59))
                out.append(at.astimezone(UTC))
            i = j
        return out

    def at(self, wd: int, seed: str) -> datetime:
        return self.slots([wd], seed)[0]


def _validate(spec: OrderSpec) -> None:
    """Fail loudly on a timeline that would tell an implausible story."""
    tl = spec.timeline
    if not tl or not isinstance(tl[0], Step) or tl[0].status != OrderStatus.DRAFT:
        raise ValueError(f"{spec.title}: the timeline must start with the DRAFT step")
    wds = [e.wd for e in tl]
    if any(b > a for a, b in pairwise(wds)):
        raise ValueError(f"{spec.title}: events must be in chronological order")
    seen: list[OrderStatus] = []
    for e in tl:
        if isinstance(e, Step):
            if seen and seen[-1] == OrderStatus.CANCELLED:
                raise ValueError(f"{spec.title}: nothing happens after CANCELLED")
            forward = e.status == OrderStatus.CANCELLED or not seen
            if not forward and PIPELINE.index(e.status) <= PIPELINE.index(seen[-1]):
                raise ValueError(f"{spec.title}: steps must move forward")
            if e.status == OrderStatus.CONFIRMED and e.by in _STAFF_KEYS:
                raise ValueError(f"{spec.title}: the client confirms a quote, not the shop")
            seen.append(e.status)
        elif isinstance(e, Comment):
            if e.anchor not in seen:
                raise ValueError(f"{spec.title}: comment before its anchor {e.anchor}")
            if e.internal and e.by not in _STAFF_KEYS:
                raise ValueError(f"{spec.title}: only staff write internal notes")
        elif isinstance(e, Upload):
            # The public demo caps visitors' uploads at 20 per rolling
            # 24 h, counted from the audit trail: a seeded upload from the
            # anchor day itself could eat into it.
            if e.wd < 1:
                raise ValueError(f"{spec.title}: seeded uploads must be a working day old")
            if e.file not in FILES:
                raise ValueError(f"{spec.title}: unknown file {e.file}")
    quoted = spec.status in PIPELINE and PIPELINE.index(spec.status) >= 2
    if quoted and spec.promised is None:
        raise ValueError(f"{spec.title}: a quoted order carries a promised date")


def _number_base(d: date) -> int:
    """Orders the shop had already numbered that year before the demo's
    first one — about three a week — so August is not "order no. 1"."""
    return d.timetuple().tm_yday * 2 // 5


def plan_orders(clock: DemoClock) -> list[PlannedOrder]:
    """Every order with concrete times and dates, numbered by creation."""
    titles = [s.title for s in ORDERS]
    if len(set(titles)) != len(titles):
        raise ValueError("order titles must be unique (the showcase looks them up)")
    planned: list[PlannedOrder] = []
    for spec in ORDERS:
        _validate(spec)
        planned.append(
            PlannedOrder(
                spec=spec,
                times=clock.slots([e.wd for e in spec.timeline], spec.title),
                requested=(clock.day(-spec.requested) if spec.requested is not None else None),
                promised=clock.day(-spec.promised) if spec.promised is not None else None,
            )
        )
    planned.sort(key=lambda p: p.created)
    seq: dict[int, int] = {}
    for p in planned:
        local = p.created.astimezone(PRAGUE).date()
        seq[local.year] = seq.get(local.year, _number_base(local)) + 1
        p.number = f"{local.year}-{seq[local.year]:06d}"
    return planned


def _render_text(body: str, p: PlannedOrder, clock: DemoClock) -> str:
    def cz(d: date | None) -> str:
        return _cz_date(d) if d else ""

    return body.format_map(
        {
            "requested": cz(p.requested),
            "promised": cz(p.promised),
            "delivered": cz(p.delivered),
            "d": _WorkdayDate(clock.anchor),
        }
    )


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


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


def _audit(
    tenant_id: UUID,
    at: datetime,
    actor: User | CustomerContact,
    action: str,
    *,
    entity_type: str,
    entity_id: UUID,
    entity_label: str,
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
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=entity_label,
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


@lru_cache(maxsize=32)
def _render_file(key: str) -> tuple[bytes, str, bytes | None]:
    """(bytes, content type, JPEG thumbnail or None without poppler). Blocking.

    Cached: the files are a pure function of the spec, and the nightly
    reset re-uploads the same bytes every night.
    """
    from app.tasks.thumbnail_tasks import _render_thumbnail

    spec = FILES[key]
    if isinstance(spec, SamplePreview):
        data, ctype = render_preview_png(spec), "image/png"
    else:
        data, ctype = render_drawing_pdf(spec), "application/pdf"
    return data, ctype, _render_thumbnail(data, ctype)


async def _list_keys(prefix: str) -> list[str]:
    from app.storage import s3 as s3_storage

    return [obj["key"] for obj in await s3_storage.list_objects_async(prefix)]


@dataclass
class _PendingUpload:
    order: Order
    file: str
    at: datetime
    by: User | CustomerContact


async def _upload_files(tenant: Tenant, uploads: list[_PendingUpload]) -> list[OrderAttachment]:
    """Render, upload and return the attachment rows (not yet added)."""
    from app.services.attachment_service import build_storage_key, build_thumbnail_key
    from app.storage import s3 as s3_storage

    rows: list[OrderAttachment] = []
    for up in uploads:
        data, ctype, thumb = await anyio.to_thread.run_sync(_render_file, up.file)
        filename = FILES[up.file].filename
        att_id = uuid4()
        key = build_storage_key(
            tenant=tenant, order_id=up.order.id, attachment_id=att_id, filename=filename
        )
        await s3_storage.upload_bytes_async(key, data, content_type=ctype)
        thumb_key = None
        if thumb is not None:
            thumb_key = build_thumbnail_key(
                tenant=tenant, order_id=up.order.id, attachment_id=att_id
            )
            await s3_storage.upload_bytes_async(thumb_key, thumb, content_type="image/jpeg")
        by_contact = isinstance(up.by, CustomerContact)
        rows.append(
            OrderAttachment(
                id=att_id,
                tenant_id=tenant.id,
                order_id=up.order.id,
                kind=AttachmentKind.DRAWING,
                filename=filename,
                content_type=ctype,
                size_bytes=len(data),
                storage_key=key,
                thumbnail_key=thumb_key,
                uploaded_by_contact_id=up.by.id if by_contact else None,
                uploaded_by_user_id=None if by_contact else up.by.id,
                # A working day+ old (see _validate): the public demo's
                # "20 new files a day" cap never counts the seeded ones.
                created_at=up.at,
            )
        )
    return rows


def _login_before(action: datetime, seed: str) -> datetime:
    """A sign-in shortly before ``action``, still inside business hours."""
    rng = random.Random(seed)
    local = action.astimezone(PRAGUE)
    day_start = datetime.combine(local.date(), DAY_START, tzinfo=PRAGUE)
    return max(day_start, local - timedelta(minutes=rng.randint(3, 25))).astimezone(UTC)


async def seed_demo(
    *,
    slug: str = "demo",
    password: str | None = None,
    owner_url: str | None = None,
    engine: AsyncEngine | None = None,
    force: bool = False,
    now: datetime | None = None,
    files: bool = True,
) -> SeedResult:
    """Create (or recreate) the demo tenant. See the module docstring.

    ``files=False`` skips S3 entirely: no sample drawings are uploaded
    and nothing is deleted from the bucket (useful without object
    storage). With ``files=True`` an S3 failure is logged and the seed
    still completes, just without drawings. ``now`` (tests) is the
    moment the seed pretends to run at; it must not be in the future.
    """
    from app.config import get_settings

    own_engine = engine is None
    if engine is None:
        engine = create_async_engine(owner_url or get_settings().database_owner_url, future=True)
    password = password or secrets.token_urlsafe(9)
    now = now or datetime.now(UTC)
    clock = DemoClock(now)
    planned = plan_orders(clock)
    # Accounts first, invitations the next working day, acceptances after.
    onboarded = clock.at(ONBOARDED_WD + 1, "onboarding")

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
                    billing_email=_email("fakturace", STAFF_DOMAIN),
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
            staff: dict[str, User] = {}
            for spec_s in STAFF:
                staff[spec_s.key] = User(
                    id=uuid4(),
                    tenant_id=tid,
                    email=_email(spec_s.local, STAFF_DOMAIN),
                    full_name=spec_s.name,
                    role=spec_s.role,
                    password_hash=pw_hash,
                    created_at=onboarded,
                )
            session.add_all(staff.values())

            # ---- clients + contacts
            customers: dict[str, Customer] = {}
            contacts: dict[str, dict[str, CustomerContact]] = {}
            people: dict[UUID, PersonSpec] = {}
            for key, (name, domain, persons) in CUSTOMERS.items():
                customer = Customer(
                    id=uuid4(),
                    tenant_id=tid,
                    name=name,
                    preferred_locale="cs",
                    created_at=onboarded,
                )
                customers[key] = customer
                session.add(customer)
                await session.flush()
                contacts[key] = {}
                for person in persons:
                    seed = f"{key}/{person.local}"
                    contact = CustomerContact(
                        id=uuid4(),
                        tenant_id=tid,
                        customer_id=customer.id,
                        email=_email(person.local, domain),
                        full_name=person.name,
                        role=(
                            CustomerContactRole.CUSTOMER_ADMIN
                            if person.admin
                            else CustomerContactRole.CUSTOMER_USER
                        ),
                        password_hash=None if person.pending else pw_hash,
                        # A pending invitation is a fresh one: the nightly
                        # stale-invite cleanup deletes anything older than
                        # its expiry, which would quietly thin the demo.
                        invited_at=(
                            clock.at(3, seed + "/inv")
                            if person.pending
                            else clock.at(ONBOARDED_WD, seed + "/inv")
                        ),
                        accepted_at=(
                            None if person.pending else clock.at(ONBOARDED_WD - 1, seed + "/acc")
                        ),
                        created_at=onboarded,
                    )
                    contacts[key][person.local] = contact
                    people[contact.id] = person
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
                    created_at=onboarded,
                )
                products[sku] = product
                session.add(product)
            await session.flush()

            # ---- orders
            audit: list[AuditEvent] = []
            last_action: dict[UUID, datetime] = {}
            orders_by_title: dict[str, Order] = {}
            uploads: list[_PendingUpload] = []

            def actor(client: str, by: str) -> User | CustomerContact:
                if by in staff:
                    return staff[by]
                return contacts[client][by]

            def acted(who: User | CustomerContact, at: datetime) -> None:
                if who.id not in last_action or at > last_action[who.id]:
                    last_action[who.id] = at

            for p in planned:
                spec = p.spec
                lines: list[OrderItem] = []
                total = Decimal("0")
                for pos, item in enumerate(spec.items):
                    item_product: Product | None = products[item.sku] if item.sku else None
                    quantity = Decimal(item.qty)
                    unit_price = Decimal(
                        item.price or (item_product.default_price if item_product else None) or "0"
                    )
                    line_total = (unit_price * quantity).quantize(Decimal("0.01"))
                    total += line_total
                    lines.append(
                        OrderItem(
                            id=uuid4(),
                            tenant_id=tid,
                            product_id=item_product.id if item_product else None,
                            position=pos,
                            description=item.desc or (item_product.name if item_product else ""),
                            quantity=quantity,
                            unit=item.unit or (item_product.unit if item_product else "ks"),
                            unit_price=unit_price,
                            line_total=line_total,
                            created_at=p.created,
                        )
                    )
                status = spec.status
                rank = PIPELINE.index(status) if status in PIPELINE else None
                creator = actor(spec.client, spec.timeline[0].by)
                confirm = next((e for e in spec.steps if e.status == OrderStatus.CONFIRMED), None)
                # A step the order skipped (a phone order the shop typed in
                # goes DRAFT -> QUOTED) is filled the way the app's
                # _backfill_milestones fills it: with the next step's time.
                after_draft = spec.steps[1] if len(spec.steps) > 1 else None
                submitted_at = p.step_time(OrderStatus.SUBMITTED) or (
                    p.step_time(after_draft.status)
                    if after_draft and after_draft.status != OrderStatus.CANCELLED
                    else None
                )
                if status == OrderStatus.SUBMITTED or status == OrderStatus.DRAFT:
                    assignee = None
                elif status == OrderStatus.QUOTED:
                    assignee = staff["planner"]
                elif rank is not None:
                    assignee = staff["foreman"]
                else:
                    assignee = None
                order = Order(
                    id=uuid4(),
                    tenant_id=tid,
                    customer_id=customers[spec.client].id,
                    number=p.number,
                    title=spec.title,
                    status=status,
                    created_by_contact_id=(
                        creator.id if isinstance(creator, CustomerContact) else None
                    ),
                    created_by_user_id=creator.id if isinstance(creator, User) else None,
                    assigned_to_user_id=assignee.id if assignee else None,
                    requested_delivery_at=p.requested,
                    promised_delivery_at=p.promised,
                    # As the app does: add_item keeps the running total cached
                    # (F-14), and confirming snapshots it (LOGIC-2).
                    quoted_total=total if lines else None,
                    quoted_at=p.step_time(OrderStatus.QUOTED),
                    confirmed_total=total if confirm else None,
                    confirmed_at=p.step_time(OrderStatus.CONFIRMED),
                    confirmed_by_contact_id=(
                        actor(spec.client, confirm.by).id if confirm else None
                    ),
                    currency="CZK",
                    submitted_at=submitted_at,
                    delivered_at=p.delivered,
                    closed_at=p.step_time(OrderStatus.CLOSED),
                    cancelled_at=p.step_time(OrderStatus.CANCELLED),
                    created_at=p.created,
                    # updated_at keeps its default (seed time) on purpose:
                    # the hourly auto-close job closes DELIVERED orders by
                    # updated_at, and would otherwise do it at night.
                )
                session.add(order)
                await session.flush()
                for line in lines:
                    line.order_id = order.id
                session.add_all(lines)

                prev: OrderStatus | None = None
                for event, at in zip(spec.timeline, p.times, strict=True):
                    who = actor(spec.client, event.by)
                    acted(who, at)
                    by_contact = isinstance(who, CustomerContact)
                    if isinstance(event, Step):
                        session.add(
                            OrderStatusHistory(
                                id=uuid4(),
                                tenant_id=tid,
                                order_id=order.id,
                                from_status=prev,
                                to_status=event.status,
                                changed_by_contact_id=who.id if by_contact else None,
                                changed_by_user_id=None if by_contact else who.id,
                                note=event.note,
                                created_at=at,
                            )
                        )
                        if prev is not None:
                            # The same trail the app writes, so the dashboard's
                            # "Recent activity" and the audit log are not empty.
                            after: dict[str, Any] = {"status": event.status.value}
                            if event.note:
                                after["note"] = event.note
                            audit.append(
                                _audit(
                                    tid,
                                    at,
                                    who,
                                    "order.status_changed",
                                    entity_type="order",
                                    entity_id=order.id,
                                    entity_label=order.number,
                                    before={"status": prev.value},
                                    after=after,
                                )
                            )
                        prev = event.status
                    elif isinstance(event, Comment):
                        body = _render_text(event.body, p, clock)
                        comment = OrderComment(
                            id=uuid4(),
                            tenant_id=tid,
                            order_id=order.id,
                            author_contact_id=who.id if by_contact else None,
                            author_user_id=None if by_contact else who.id,
                            body=body,
                            is_internal=event.internal,
                            created_at=at,
                        )
                        session.add(comment)
                        audit.append(
                            _audit(
                                tid,
                                at,
                                who,
                                "order.comment_added",
                                entity_type="order",
                                entity_id=order.id,
                                entity_label=order.number,
                                after={
                                    "comment_id": str(comment.id),
                                    "is_internal": comment.is_internal,
                                    "body": body,
                                },
                            )
                        )
                    else:
                        uploads.append(_PendingUpload(order, event.file, at, who))
                orders_by_title[spec.title] = order
            await session.flush()

            # ---- drawings (S3). A failure costs the drawings, not the seed.
            if s3_ok:
                try:
                    attachments = await _upload_files(tenant, uploads)
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
                by_id: dict[UUID, User | CustomerContact] = {
                    **{u.id: u for u in staff.values()},
                    **{c.id: c for group in contacts.values() for c in group.values()},
                }
                for att in attachments:
                    uploader = by_id[att.uploaded_by_contact_id or att.uploaded_by_user_id]  # type: ignore[index]
                    audit.append(
                        _audit(
                            tid,
                            att.created_at,
                            uploader,
                            "attachment.upload",
                            entity_type="attachment",
                            entity_id=att.id,
                            entity_label=att.filename,
                            after={
                                "order_id": str(att.order_id),
                                "size_bytes": att.size_bytes,
                                "content_type": att.content_type,
                            },
                        )
                    )
            session.add_all(audit)

            # ---- client-owned material
            for spec_a in ASSETS:
                times = clock.slots([m.wd for m in spec_a.moves], spec_a.code)
                stock = sum((Decimal(m.qty) * _SIGN[m.type] for m in spec_a.moves), Decimal("0"))
                asset = Asset(
                    id=uuid4(),
                    tenant_id=tid,
                    customer_id=customers[spec_a.client].id,
                    code=spec_a.code,
                    name=spec_a.name,
                    unit=spec_a.unit,
                    current_quantity=stock,
                    location=spec_a.location,
                    created_at=times[0],
                )
                session.add(asset)
                await session.flush()
                for move, at in zip(spec_a.moves, times, strict=True):
                    ref = orders_by_title[move.order] if move.order else None
                    acted(staff[move.by], at)
                    session.add(
                        AssetMovement(
                            id=uuid4(),
                            tenant_id=tid,
                            asset_id=asset.id,
                            type=move.type,
                            quantity=Decimal(move.qty) * _SIGN[move.type],
                            note=move.note,
                            reference_order_id=ref.id if ref else None,
                            occurred_at=at,
                            created_by_user_id=staff[move.by].id,
                            created_at=at,
                        )
                    )

            # ---- last sign-ins: whoever did something signed in to do it.
            for spec_s in STAFF:
                user = staff[spec_s.key]
                user.last_login_at = _last_login(
                    clock, spec_s.login_wd, last_action.get(user.id), f"staff/{spec_s.key}"
                )
            for key, group in contacts.items():
                for local, contact in group.items():
                    person = people[contact.id]
                    if person.pending:
                        if contact.id in last_action:
                            raise ValueError(f"{person.name} is invited only, yet acted")
                        continue
                    contact.last_login_at = _last_login(
                        clock, person.login_wd, last_action.get(contact.id), f"{key}/{local}"
                    )

            result = SeedResult(
                tenant_id=tid,
                slug=slug,
                staff_email=staff["owner"].email,
                contact_email=contacts["ukazkova"][CONTACT_LOCAL].email,
                password=password,
                customers=len(customers),
                contacts=sum(len(v) for v in contacts.values()),
                products=len(products),
                orders=len(planned),
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


def _last_login(
    clock: DemoClock, login_wd: int | None, last_action: datetime | None, seed: str
) -> datetime | None:
    candidates = []
    if login_wd is not None:
        candidates.append(clock.at(login_wd, seed + "/login"))
    if last_action is not None:
        candidates.append(_login_before(last_action, seed + "/act"))
    return max(candidates) if candidates else None


# --------------------------------------------------------------------------
# Showcase lookup (the demo's "where to start" card)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ShowcaseLink:
    id: UUID
    label: str  # order number, or the material's code


@dataclass(frozen=True)
class DemoShowcase:
    flagship_order: ShowcaseLink | None
    overdue_order: ShowcaseLink | None
    material: ShowcaseLink | None


async def find_showcase(db: AsyncSession, tenant_id: UUID) -> DemoShowcase:
    """The flagship quote, the overdue order and the showcase material of
    a seeded demo tenant — by title / code, so it survives the nightly
    re-seed. ``None`` for anything a visitor renamed or deleted today.

    Works with the request's RLS-scoped session as well as the owner's.
    """

    async def order(title: str) -> ShowcaseLink | None:
        row = (
            await db.execute(
                select(Order.id, Order.number)
                .where(Order.tenant_id == tenant_id, Order.title == title)
                .order_by(Order.created_at)
                .limit(1)
            )
        ).first()
        return ShowcaseLink(id=row.id, label=row.number) if row else None

    asset = (
        await db.execute(
            select(Asset.id, Asset.code)
            .where(Asset.tenant_id == tenant_id, Asset.code == SHOWCASE_MATERIAL_CODE)
            .limit(1)
        )
    ).first()
    return DemoShowcase(
        flagship_order=await order(FLAGSHIP_ORDER_TITLE),
        overdue_order=await order(OVERDUE_ORDER_TITLE),
        material=ShowcaseLink(id=asset.id, label=asset.code) if asset else None,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


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
