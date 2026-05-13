-- =============================================================================
-- pg-vacuum-advisor — Test Scenario Setup
-- =============================================================================
-- Creates 9 test tables in the vac_test schema, each designed to trigger a
-- specific status code or recommendation in the advisor output.
--
-- Usage:
--   psql -h HOST -d DB -U USER -f tests/setup.sql
--
-- Then run the advisor:
--   python vacuum_advisor.py -H HOST -d DB -U USER \
--       --platform rds --schema vac_test
--
-- Dead-tuple counts are calculated precisely per platform:
--   RDS      vacuum trigger = 50 + (0.10 × live_rows)
--   RDS      analyze trigger = 50 + (0.05 × live_rows)
--   Aurora   vacuum trigger = 50 + (0.20 × live_rows)
--   Aurora   analyze trigger = 50 + (0.10 × live_rows)
--   Cloud SQL = same as Aurora
-- =============================================================================

\set ON_ERROR_STOP on
\timing on

-- ---------------------------------------------------------------------------
-- Schema
-- ---------------------------------------------------------------------------
\echo ''
\echo '► Dropping and recreating vac_test schema...'
DROP SCHEMA IF EXISTS vac_test CASCADE;
CREATE SCHEMA vac_test;


-- ---------------------------------------------------------------------------
-- Scenario 1 — HIGH BLOAT
-- ---------------------------------------------------------------------------
-- Expected status : 🚫 DISABLED  +  ⚠ HIGH BLOAT  (both shown simultaneously)
-- dead_pct        : 3,000 / (10,000 + 3,000) = 23.1%  (threshold: 20%)
-- All platforms   : trigger = 50 + 0.1×10000 = 1,050  → 285% to trigger
--
-- autovacuum is left disabled so dead tuples persist long enough for the
-- advisor to see them.  On a live RDS instance autovacuum fires within
-- seconds of RESET, which is why we do not RESET here.
-- ---------------------------------------------------------------------------
\echo ''
\echo '► Scenario 1: HIGH BLOAT'

CREATE TABLE vac_test.sc1_high_bloat (
    id      SERIAL PRIMARY KEY,
    val     INTEGER,
    payload TEXT
);
ALTER TABLE vac_test.sc1_high_bloat SET (autovacuum_enabled = false);

INSERT INTO vac_test.sc1_high_bloat (val, payload)
    SELECT g % 100, md5(g::text)
    FROM   generate_series(1, 10000) g;

-- Create 3,000 dead tuples → dead_pct ≈ 23.1%
UPDATE vac_test.sc1_high_bloat
    SET    payload = payload || '_dead'
    WHERE  id <= 3000;

-- autovacuum intentionally left disabled to preserve dead tuples for the test.


-- ---------------------------------------------------------------------------
-- Scenario 2 — NEAR VACUUM TRIGGER  (RDS defaults)
-- ---------------------------------------------------------------------------
-- Expected status : 🚫 DISABLED  +  ⚡ NEAR VAC
-- Platform        : RDS  (vacuum_scale_factor = 0.1)
-- Vacuum trigger  : 50 + 0.1 × 100,000 = 10,050 dead rows
-- Dead rows built : 8,600  →  8,600 / 10,050 = 85.6% to trigger
-- dead_pct        : 8,600 / 108,600 = 7.9%  (not HIGH BLOAT)
--
-- autovacuum left disabled — same reason as sc1.
-- ---------------------------------------------------------------------------
\echo ''
\echo '► Scenario 2: NEAR VAC TRIGGER — RDS  (scale=0.1, target 85%)'

CREATE TABLE vac_test.sc2_near_vac_rds (
    id      SERIAL PRIMARY KEY,
    val     INTEGER,
    payload TEXT
);
ALTER TABLE vac_test.sc2_near_vac_rds SET (autovacuum_enabled = false);

INSERT INTO vac_test.sc2_near_vac_rds (val, payload)
    SELECT g % 500, md5(g::text)
    FROM   generate_series(1, 100000) g;

-- 8,600 dead tuples = 85.6% of the RDS trigger (10,050)
UPDATE vac_test.sc2_near_vac_rds
    SET    payload = payload || '_dead'
    WHERE  id <= 8600;

-- autovacuum intentionally left disabled to preserve dead tuples for the test.


-- ---------------------------------------------------------------------------
-- Scenario 3 — NEAR VACUUM TRIGGER  (Aurora / Cloud SQL defaults)
-- ---------------------------------------------------------------------------
-- Expected status : 🚫 DISABLED  +  ⚡ NEAR VAC  (with --platform aurora/cloudsql)
-- Platform        : Aurora / Cloud SQL  (vacuum_scale_factor = 0.2)
-- Vacuum trigger  : 50 + 0.2 × 100,000 = 20,050 dead rows
-- Dead rows built : 17,100  →  17,100 / 20,050 = 85.3% to trigger
-- dead_pct        : 17,100 / 117,100 = 14.6%  (not HIGH BLOAT)
--
-- NOTE: Run with --platform aurora or --platform cloudsql to see NEAR VAC.
--       With --platform rds (scale=0.1, trigger=10,050) it will show NEAR VAC too
--       since 17,100 > 10,050 (170% to trigger → already past threshold).
-- autovacuum left disabled — same reason as sc1.
-- ---------------------------------------------------------------------------
\echo ''
\echo '► Scenario 3: NEAR VAC TRIGGER — Aurora/Cloud SQL  (scale=0.2, target 85%)'

CREATE TABLE vac_test.sc3_near_vac_aurora (
    id      SERIAL PRIMARY KEY,
    val     INTEGER,
    payload TEXT
);
ALTER TABLE vac_test.sc3_near_vac_aurora SET (autovacuum_enabled = false);

INSERT INTO vac_test.sc3_near_vac_aurora (val, payload)
    SELECT g % 500, md5(g::text)
    FROM   generate_series(1, 100000) g;

-- 17,100 dead tuples = 85.3% of the Aurora trigger (20,050)
UPDATE vac_test.sc3_near_vac_aurora
    SET    payload = payload || '_dead'
    WHERE  id <= 17100;

-- autovacuum intentionally left disabled to preserve dead tuples for the test.


-- ---------------------------------------------------------------------------
-- Scenario 4 — NEAR ANALYZE TRIGGER  (RDS defaults)
-- ---------------------------------------------------------------------------
-- Expected status : 📈 NEAR ANA
-- Platform        : RDS  (analyze_scale_factor = 0.05)
-- ANALYZE is run first to reset n_mod_since_analyze to 0, then we create
-- modifications that bring it to ~85% of the analyze trigger.
-- Analyze trigger : 50 + 0.05 × 100,000 = 5,050 modified rows
-- Mods created    : 4,300  →  4,300 / 5,050 = 85.1% to trigger
-- ---------------------------------------------------------------------------
\echo ''
\echo '► Scenario 4: NEAR ANALYZE TRIGGER — RDS  (analyze_scale=0.05, target 85%)'

CREATE TABLE vac_test.sc4_near_analyze_rds (
    id      SERIAL PRIMARY KEY,
    val     INTEGER,
    payload TEXT
);
ALTER TABLE vac_test.sc4_near_analyze_rds SET (autovacuum_enabled = false);

INSERT INTO vac_test.sc4_near_analyze_rds (val, payload)
    SELECT g % 500, md5(g::text)
    FROM   generate_series(1, 100000) g;

-- Run ANALYZE to reset n_mod_since_analyze → 0
ANALYZE vac_test.sc4_near_analyze_rds;

-- Now create 4,300 modifications = 85.1% of the analyze trigger (5,050)
UPDATE vac_test.sc4_near_analyze_rds
    SET    payload = payload || '_mod'
    WHERE  id <= 4300;

-- Keep autovacuum disabled so n_mod_since_analyze stays at 4,300


-- ---------------------------------------------------------------------------
-- Scenario 5 — AUTOVACUUM DISABLED
-- ---------------------------------------------------------------------------
-- Expected status : 🚫 DISABLED
-- autovacuum_enabled = false is set as a permanent storage parameter.
-- The advisor will flag this with a critical warning and show the RESET SQL.
-- ---------------------------------------------------------------------------
\echo ''
\echo '► Scenario 5: AUTOVACUUM DISABLED'

CREATE TABLE vac_test.sc5_autovac_disabled (
    id      SERIAL PRIMARY KEY,
    val     INTEGER,
    payload TEXT
);
ALTER TABLE vac_test.sc5_autovac_disabled SET (autovacuum_enabled = false);

INSERT INTO vac_test.sc5_autovac_disabled (val, payload)
    SELECT g % 1000, md5(g::text)
    FROM   generate_series(1, 500000) g;

UPDATE vac_test.sc5_autovac_disabled
    SET    payload = payload || '_dead'
    WHERE  id <= 100000;

-- autovacuum intentionally remains disabled


-- ---------------------------------------------------------------------------
-- Scenario 6 — LARGE TABLE, NEEDS TUNING
-- ---------------------------------------------------------------------------
-- Expected        : appears in 🔧 Tuning Recommendations
-- 2M rows on RDS global scale=0.1 → vacuum fires at 200,050 dead rows
-- Recommended     : scale=0.01 → fires at 21,000 dead rows (9.5× more responsive)
-- Also recommended: analyze tuning (0.05 → 0.02)
-- ---------------------------------------------------------------------------
\echo ''
\echo '► Scenario 6: LARGE TABLE, NEEDS TUNING  (2M rows, no per-table settings)'

CREATE TABLE vac_test.sc6_large_needs_tune (
    id      SERIAL PRIMARY KEY,
    val     INTEGER,
    payload TEXT
);
ALTER TABLE vac_test.sc6_large_needs_tune SET (autovacuum_enabled = false);

INSERT INTO vac_test.sc6_large_needs_tune (val, payload)
    SELECT g % 10000, md5(g::text)
    FROM   generate_series(1, 2000000) g;

ALTER TABLE vac_test.sc6_large_needs_tune RESET (autovacuum_enabled);


-- ---------------------------------------------------------------------------
-- Scenario 7 — LARGE TABLE, ALREADY TUNED
-- ---------------------------------------------------------------------------
-- Expected status : ✓ OK  (has per-table override, marked with †)
-- scale_factor=0.005 is within the recommended range for a 5M-row table
-- (recommended = 0.005 for > 10M rows; 0.01 for 1–10M → 0.005 ≤ 0.005×5 = OK)
-- The advisor should NOT include this in recommendations.
-- ---------------------------------------------------------------------------
\echo ''
\echo '► Scenario 7: LARGE TABLE, ALREADY TUNED  (5M rows, good per-table settings)'

CREATE TABLE vac_test.sc7_large_already_tuned (
    id      SERIAL PRIMARY KEY,
    val     INTEGER,
    payload TEXT
);
ALTER TABLE vac_test.sc7_large_already_tuned SET (
    autovacuum_vacuum_scale_factor  = 0.005,
    autovacuum_vacuum_threshold     = 1000,
    autovacuum_analyze_scale_factor = 0.01,
    autovacuum_analyze_threshold    = 1000
);
-- Temporarily disable autovacuum during load
ALTER TABLE vac_test.sc7_large_already_tuned SET (autovacuum_enabled = false);

INSERT INTO vac_test.sc7_large_already_tuned (val, payload)
    SELECT g % 50000, md5(g::text)
    FROM   generate_series(1, 5000000) g;

-- Analyze resets n_mod_since_analyze so the table starts with a clean baseline
ANALYZE vac_test.sc7_large_already_tuned;

ALTER TABLE vac_test.sc7_large_already_tuned RESET (autovacuum_enabled);


-- ---------------------------------------------------------------------------
-- Scenario 8 — SMALL HEALTHY TABLE
-- ---------------------------------------------------------------------------
-- Expected status : ✓ OK
-- Low row count, no dead tuples, autovacuum recently ran (VACUUM ANALYZE forces
-- last_autovacuum-equivalent stats).
-- ---------------------------------------------------------------------------
\echo ''
\echo '► Scenario 8: SMALL HEALTHY TABLE'

CREATE TABLE vac_test.sc8_small_ok (
    id      SERIAL PRIMARY KEY,
    payload TEXT
);
INSERT INTO vac_test.sc8_small_ok (payload)
    SELECT md5(g::text)
    FROM   generate_series(1, 5000) g;

VACUUM ANALYZE vac_test.sc8_small_ok;


-- ---------------------------------------------------------------------------
-- Scenario 9 — NEVER VACUUMED
-- ---------------------------------------------------------------------------
-- Expected        : "Never" shown in red in the Last Autovacuum column
-- 200K live rows but last_autovacuum = NULL because autovacuum has never run
-- (either disabled or the table was just created and the threshold was never hit)
-- Also flagged in the Summary as "Never autovacuumed".
-- ---------------------------------------------------------------------------
\echo ''
\echo '► Scenario 9: NEVER VACUUMED  (200K rows, last_autovacuum = NULL)'

CREATE TABLE vac_test.sc9_never_vacuumed (
    id      SERIAL PRIMARY KEY,
    val     INTEGER,
    payload TEXT
);
ALTER TABLE vac_test.sc9_never_vacuumed SET (autovacuum_enabled = false);

INSERT INTO vac_test.sc9_never_vacuumed (val, payload)
    SELECT g % 1000, md5(g::text)
    FROM   generate_series(1, 200000) g;

-- autovacuum remains disabled → last_autovacuum will be NULL forever


-- ---------------------------------------------------------------------------
-- Wait for stats collector
-- ---------------------------------------------------------------------------
-- pg_stat_user_tables is populated asynchronously by the stats collector.
-- 3 seconds is enough for n_live_tup / n_dead_tup and n_mod_since_analyze
-- to be visible, including the reset from the VACUUM ANALYZE on sc8.
SELECT pg_sleep(3);


-- ---------------------------------------------------------------------------
-- Summary
-- ---------------------------------------------------------------------------
\echo ''
\echo '============================================================'
\echo ' Setup complete. Expected advisor output:'
\echo '============================================================'
\echo ''
SELECT
    lpad(relname, 35)                                   AS "Table",
    lpad(n_live_tup::text, 10)                          AS "Live rows",
    lpad(n_dead_tup::text, 10)                          AS "Dead rows",
    lpad(
        CASE WHEN n_live_tup + n_dead_tup > 0
             THEN round(100.0 * n_dead_tup / (n_live_tup + n_dead_tup), 1)::text || '%'
             ELSE '—' END,
        8
    )                                                   AS "Dead %",
    lpad(n_mod_since_analyze::text, 12)                 AS "Mod/Analyze",
    CASE
        WHEN EXISTS (
            SELECT 1 FROM pg_class c
            WHERE c.relname = s.relname
              AND c.relnamespace = 'vac_test'::regnamespace
              AND EXISTS (
                  SELECT 1 FROM unnest(c.reloptions) o
                  WHERE o LIKE 'autovacuum_enabled=false'
              )
        ) THEN '🚫 DISABLED'
        WHEN n_dead_tup::float / NULLIF(n_live_tup + n_dead_tup, 0) >= 0.20 THEN '⚠ HIGH BLOAT'
        ELSE '(check with advisor)'
    END                                                 AS "Expected Status"
FROM   pg_stat_user_tables s
WHERE  schemaname = 'vac_test'
ORDER  BY relname;

\echo ''
\echo 'Run advisor — RDS:'
\echo '  python3 vacuum_advisor.py -H <host> -d <db> -U <user> --platform rds --schema vac_test'
\echo ''
\echo 'Run advisor — Aurora:'
\echo '  python3 vacuum_advisor.py -H <host> -d <db> -U <user> --platform aurora --schema vac_test'
\echo ''
\echo 'Run advisor — Cloud SQL:'
\echo '  python3 vacuum_advisor.py -H <host> -d <db> -U <user> --platform cloudsql --schema vac_test'
\echo ''
\echo 'Export to JSON:'
\echo '  python3 vacuum_advisor.py -H <host> -d <db> -U <user> --platform rds --schema vac_test --format json --output vac_test_report.json'
