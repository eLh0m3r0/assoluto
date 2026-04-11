"""CLI: bootstrap a tenant + its first admin user.

Usage::

    python -m scripts.create_tenant <slug> <owner_email> [--name NAME]
                                    [--password PASSWORD]

Intentionally uses the OWNER database URL (`DATABASE_OWNER_URL`) because
it needs to write the Tenant row itself (the `tenants` table is not
RLS-protected, but the owner role is also how we bypass RLS on the
`users` insert for bootstrap).

If `--password` is omitted, one is generated and printed to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import string
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.enums import UserRole
from app.models.tenant import Tenant
from app.models.user import User
from app.security.passwords import hash_password


def _random_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a new portal tenant.")
    parser.add_argument("slug", help="URL-safe tenant slug, e.g. '4mex'")
    parser.add_argument("owner_email", help="Initial admin email")
    parser.add_argument(
        "--name",
        help="Display name of the tenant (defaults to slug)",
        default=None,
    )
    parser.add_argument(
        "--full-name",
        help="Full name of the owner user (defaults to the email local-part)",
        default=None,
    )
    parser.add_argument(
        "--password",
        help="Admin password (generated if omitted)",
        default=None,
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_owner_url, future=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    password = args.password or _random_password()
    name = args.name or args.slug
    owner_full_name = args.full_name or args.owner_email.split("@")[0]

    try:
        async with sm() as session, session.begin():
            existing = (
                await session.execute(select(Tenant).where(Tenant.slug == args.slug))
            ).scalar_one_or_none()
            if existing is not None:
                print(
                    f"Tenant with slug '{args.slug}' already exists.",
                    file=sys.stderr,
                )
                return 1

            tenant = Tenant(
                slug=args.slug,
                name=name,
                billing_email=args.owner_email,
                storage_prefix=f"tenants/{args.slug}/",
            )
            session.add(tenant)
            await session.flush()

            user = User(
                tenant_id=tenant.id,
                email=args.owner_email.strip().lower(),
                full_name=owner_full_name,
                password_hash=hash_password(password),
                role=UserRole.TENANT_ADMIN,
            )
            session.add(user)
            await session.flush()

        print("Tenant created successfully.")
        print(f"  Tenant:   {tenant.name} ({tenant.slug})")
        print(f"  Tenant ID: {tenant.id}")
        print(f"  Owner:    {user.email}")
        if args.password is None:
            print(f"  Password: {password}")
            print("  (Generated password shown only once — store it safely.)")
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
