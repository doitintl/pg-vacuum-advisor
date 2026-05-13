-- =============================================================================
-- pg-vacuum-advisor — Teardown
-- =============================================================================
-- Removes all test objects created by setup.sql and the pgbench scripts.
--
-- Usage:
--   psql -h HOST -d DB -U USER -f tests/teardown.sql
-- =============================================================================

\echo 'Dropping vac_test schema and all test tables...'

DROP SCHEMA IF EXISTS vac_test CASCADE;

-- pgbench tables (only if you ran pgbench -i)
DROP TABLE IF EXISTS public.pgbench_events   CASCADE;
DROP TABLE IF EXISTS public.pgbench_history  CASCADE;
DROP TABLE IF EXISTS public.pgbench_accounts CASCADE;
DROP TABLE IF EXISTS public.pgbench_tellers  CASCADE;
DROP TABLE IF EXISTS public.pgbench_branches CASCADE;

\echo 'Done.'
