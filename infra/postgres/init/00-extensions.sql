-- Extensions required by the platform. Runs once on first cluster init.
-- Alembic also guards these with CREATE EXTENSION IF NOT EXISTS so that
-- managed Postgres (Azure Flexible Server / RDS) works without this file.
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "citext";
CREATE EXTENSION IF NOT EXISTS "btree_gin";
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements";
