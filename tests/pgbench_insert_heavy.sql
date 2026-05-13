-- =============================================================================
-- pg-vacuum-advisor — pgbench custom script: insert-heavy workload
-- =============================================================================
-- Simulates a high-insert workload (e.g. event/log tables).
-- Creates a separate table so it doesn't interfere with the standard
-- pgbench_accounts table.
--
-- One-time setup (run once before starting pgbench):
--   psql -h HOST -d DB -c "
--     CREATE TABLE IF NOT EXISTS pgbench_events (
--         id         BIGSERIAL PRIMARY KEY,
--         aid        INTEGER,
--         event_type TEXT,
--         payload    TEXT,
--         created_at TIMESTAMPTZ DEFAULT now()
--     );"
--
-- Usage:
--   pgbench -c 4 -T 120 -f tests/pgbench_insert_heavy.sql <db>
--
-- This table will be flagged for autovacuum_vacuum_insert_threshold tuning
-- (PostgreSQL 13+) and will accumulate quickly to test the "NEVER VACUUMED"
-- and "NEAR ANALYZE TRIGGER" status codes.
-- =============================================================================

\set aid  random(1, 100000 * :scale)
\set etype random(1, 5)

BEGIN;

INSERT INTO pgbench_events (aid, event_type, payload)
    VALUES (
        :aid,
        CASE :etype
            WHEN 1 THEN 'login'
            WHEN 2 THEN 'logout'
            WHEN 3 THEN 'purchase'
            WHEN 4 THEN 'view'
            ELSE        'search'
        END,
        md5(random()::text)
    );

END;
