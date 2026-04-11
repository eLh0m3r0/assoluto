-- Bootstrap SQL run once by the Postgres container on first start.
-- docker-entrypoint-initdb.d/ executes files here as the `postgres`
-- superuser, which is the only thing allowed to CREATE ROLE.

-- Application runtime role: non-owner, subject to RLS.
-- Only created if it doesn't already exist (idempotent across volume reuse).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'portal_app') THEN
        CREATE ROLE portal_app LOGIN PASSWORD 'portal_app';
    END IF;
END $$;

GRANT CONNECT ON DATABASE portal TO portal_app;

\connect portal

GRANT USAGE ON SCHEMA public TO portal_app;
-- Grant on existing tables (tenants may already exist from a previous run).
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO portal_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO portal_app;

-- Grant privileges on future tables/sequences created by the `portal` owner.
ALTER DEFAULT PRIVILEGES FOR ROLE portal IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO portal_app;
ALTER DEFAULT PRIVILEGES FOR ROLE portal IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO portal_app;
