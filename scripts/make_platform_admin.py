"""CLI: promote an existing platform Identity to ``is_platform_admin``.

Usage::

    python -m scripts.make_platform_admin <email>
    python -m scripts.make_platform_admin <email> --verify-email

Requires ``FEATURE_PLATFORM=true`` (the identity table lives in the
platform package). Must be run from a node with ``DATABASE_OWNER_URL``
reachable — typically via ``docker compose exec web`` on the VPS.

Flags:
    --verify-email   Also stamp ``email_verified_at`` if it's currently
                     NULL. Useful when SMTP isn't configured yet and
                     you just signed up but can't click the verify link.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Promote a platform Identity to platform_admin.")
    parser.add_argument("email", help="Identity email to promote")
    parser.add_argument(
        "--verify-email",
        action="store_true",
        help="Also mark email_verified_at (helpful when SMTP is down).",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.feature_platform:
        print(
            "FEATURE_PLATFORM is off — the platform Identity table is present "
            "in the schema but no signups can reach it. Turn FEATURE_PLATFORM "
            "on first.",
            file=sys.stderr,
        )
        return 2

    # Import via app.models (which re-exports Identity). Importing
    # app.platform.models directly triggers a circular import because
    # app.platform.models imports from app.models.mixins, and
    # app/models/__init__.py imports Identity back from
    # app.platform.models — bouncing through app.models first breaks
    # the cycle.
    from app.models import Identity  # re-exported

    engine = create_async_engine(settings.database_owner_url, future=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    email = args.email.strip().lower()

    try:
        async with sm() as session, session.begin():
            identity = (
                await session.execute(select(Identity).where(Identity.email == email))
            ).scalar_one_or_none()
            if identity is None:
                print(
                    f"No platform Identity found for {email!r}. Sign up at /platform/signup first.",
                    file=sys.stderr,
                )
                return 1

            changed: list[str] = []
            if not identity.is_platform_admin:
                identity.is_platform_admin = True
                changed.append("is_platform_admin=True")
            if args.verify_email and identity.email_verified_at is None:
                identity.email_verified_at = datetime.now(UTC)
                changed.append("email_verified_at=now()")

            if not changed:
                print(f"Nothing to change — {email} is already a verified admin.")
                return 0

        print(f"Identity {email} updated:")
        for c in changed:
            print(f"  • {c}")
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
