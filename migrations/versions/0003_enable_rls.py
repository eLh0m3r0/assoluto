"""Enable Row-Level Security on tenant-owned tables.

Revision ID: 0003_enable_rls
Revises: 0002_auth_tables
Create Date: 2026-04-11

Design:
* Two DB roles are in play. `portal` (the table OWNER) is used by Alembic
  migrations and by bootstrap scripts that need full visibility; Postgres
  row-level security does not apply to table owners by default, so owner
  sessions bypass the policies automatically. `portal_app` is an
  unprivileged login role used by the running application; because it is
  NOT the owner, all RLS policies are enforced on every statement.
* The `portal_app` role must exist BEFORE this migration runs (created by
  `docker/postgres-init.sql` in docker-compose or by the CI setup step).
  Grants issued here are idempotent and skipped with a NOTICE if the role
  is missing, so the migration remains usable on superuser-less DBs.
* The isolation policy on each tenant-owned table matches rows where
  `tenant_id = current_setting('app.tenant_id')::uuid`. The runtime
  dependency in `app.deps.get_db` sets that session variable on every
  request.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_enable_rls"
down_revision: str | Sequence[str] | None = "0002_auth_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Tables that carry `tenant_id` and therefore need tenant isolation.
RLS_TABLES: tuple[str, ...] = (
    "users",
    "customers",
    "customer_contacts",
)


def upgrade() -> None:
    # Grant runtime privileges on the already-created tenant tables to the
    # application role. Wrapped in a DO block so the migration does not
    # fail if `portal_app` doesn't exist (e.g. on a minimal test setup).
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'portal_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON tenants, users, customers, customer_contacts
                    TO portal_app;
            ELSE
                RAISE NOTICE 'Role portal_app does not exist, skipping grants';
            END IF;
        END $$;
        """
    )

    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        # NO FORCE: table owner (portal) keeps bypassing RLS. Migrations and
        # bootstrap scripts run as `portal` and can seed data across tenants;
        # the application runs as `portal_app` and is fully subject to the
        # policy.
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
            """
        )


def downgrade() -> None:
    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'portal_app') THEN
                REVOKE ALL ON tenants, users, customers, customer_contacts
                    FROM portal_app;
            END IF;
        END $$;
        """
    )
