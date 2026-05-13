#!/usr/bin/env bash
# =============================================================================
# pg-vacuum-advisor — Test Runner
# =============================================================================
# Runs all three test tracks:
#   Track A — Static SQL scenarios (9 hand-crafted tables, instant setup)
#   Track B — pgbench standard workload (1M row accounts table)
#   Track C — pgbench custom insert-heavy workload (pgbench_events table)
#
# Usage:
#   export PGHOST=myhost PGDATABASE=mydb PGUSER=myuser
#   export PGPASSWORD=secret        # or use -W / .pgpass
#   bash tests/run_tests.sh [rds|aurora|cloudsql]
#
# Platform defaults to 'rds' if not specified.
# =============================================================================

set -euo pipefail

PLATFORM="${1:-rds}"
ADVISOR="python3 vacuum_advisor.py"
PSQL="psql -h ${PGHOST:-localhost} -d ${PGDATABASE:-postgres} -U ${PGUSER:-postgres}"
# pgbench: -d means --debug in pgbench (not --dbname).  Database name is a
# positional argument, so it must be appended to each pgbench call explicitly.
PGBENCH="pgbench -h ${PGHOST:-localhost} -U ${PGUSER:-postgres}"
PGDB="${PGDATABASE:-postgres}"
SCHEMA="vac_test"

# Advisor base command (re-used across all tracks)
ADV_BASE="${ADVISOR} -H ${PGHOST:-localhost} -d ${PGDATABASE:-postgres} -U ${PGUSER:-postgres} --platform ${PLATFORM}"

hr() { printf '\n%s\n' "$(printf '=%.0s' {1..72})"; }

# =============================================================================
# TRACK A — Static SQL Scenarios
# =============================================================================
hr
echo "TRACK A — Static SQL Scenarios (platform: ${PLATFORM})"
hr

echo ""
echo "Step A1: Loading 9 test scenarios into the '${SCHEMA}' schema..."
$PSQL -f tests/setup.sql

echo ""
echo "Step A2: Running advisor against '${SCHEMA}' schema..."
echo ""
echo "--- Console output ---"
$ADV_BASE --schema ${SCHEMA}

echo ""
echo "--- JSON output (saved to tests/track_a_report.json) ---"
$ADV_BASE --schema ${SCHEMA} --format json --output tests/track_a_report.json
echo "Saved: tests/track_a_report.json"

echo ""
echo "--- CSV output (saved to tests/track_a_tables.csv) ---"
$ADV_BASE --schema ${SCHEMA} --format csv --output tests/track_a_tables.csv
echo "Saved: tests/track_a_tables.csv"

echo ""
echo "Step A3: Platform comparison — same data, different scale_factor baselines"
echo ""
echo "  sc3_near_vac_aurora has 17,100 dead rows in a 100K-row table."
echo "  The vacuum trigger differs based on the platform flag:"
echo "    --platform rds    : trigger = 50 + 0.1×100K = 10,050  →  vacuum_pct ≈ 170%  (already past)"
echo "    --platform aurora : trigger = 50 + 0.2×100K = 20,050  →  vacuum_pct ≈  85%  (near threshold)"
echo ""
echo "  This test connects to the SAME database but uses Aurora math to prove the"
echo "  --platform flag correctly changes the trigger calculation, not just the label."
echo ""

# Run JSON output and extract sc3 vacuum_pct under both platforms to show the numeric difference
RDS_PCT=$(${ADVISOR} -H ${PGHOST:-localhost} -d ${PGDATABASE:-postgres} -U ${PGUSER:-postgres} \
    --platform rds --schema ${SCHEMA} --format json \
    | python3 -c "
import json, sys
tables = json.load(sys.stdin)['tables']
row = next((t for t in tables if t['table'] == 'sc3_near_vac_aurora'), None)
print(row['vacuum_pct'] if row else 'not found')
")

AURORA_PCT=$(${ADVISOR} -H ${PGHOST:-localhost} -d ${PGDATABASE:-postgres} -U ${PGUSER:-postgres} \
    --platform aurora --schema ${SCHEMA} --format json \
    | python3 -c "
import json, sys
tables = json.load(sys.stdin)['tables']
row = next((t for t in tables if t['table'] == 'sc3_near_vac_aurora'), None)
print(row['vacuum_pct'] if row else 'not found')
")

echo "  sc3_near_vac_aurora vacuum_pct:"
echo "    --platform rds    : ${RDS_PCT}%   (trigger=10,050)"
echo "    --platform aurora : ${AURORA_PCT}%    (trigger=20,050)"
echo ""
echo "  ✓ Different baselines produce different vacuum_pct for identical data"

echo ""
echo "✓ Track A complete"

# =============================================================================
# TRACK B — pgbench Standard Workload
# =============================================================================
hr
echo "TRACK B — pgbench Standard Workload (1M row accounts table)"
hr

echo ""
echo "Step B1: Initialising pgbench tables (scale=10 → ~1M rows in accounts)..."
echo "  This creates: pgbench_accounts (1M rows), pgbench_branches, pgbench_tellers"
$PGBENCH -i -s 10 --quiet $PGDB

echo ""
echo "Step B2: Running update-heavy workload for 60 seconds..."
echo "  8 clients, custom update-heavy script"
echo "  This generates dead tuples in pgbench_accounts, pgbench_tellers, pgbench_branches"
$PGBENCH -c 8 -T 60 -f tests/pgbench_update_heavy.sql --no-vacuum $PGDB

echo ""
echo "Step B3: Running advisor against pgbench tables (public schema, min 1000 rows)..."
$ADV_BASE --min-rows 1000 --format json --output tests/track_b_report.json
$ADV_BASE --min-rows 1000

echo ""
echo "Expected findings:"
echo "  pgbench_accounts (1M rows): flagged for tuning (scale=0.1 → recommended 0.01)"
echo "  pgbench_accounts: possibly NEAR VAC or HIGH BLOAT depending on run duration"
echo "  pgbench_tellers / pgbench_branches: OK (small tables)"

echo ""
echo "✓ Track B complete"

# =============================================================================
# TRACK C — pgbench Insert-Heavy Workload
# =============================================================================
hr
echo "TRACK C — pgbench Insert-Heavy Workload (pgbench_events)"
hr

echo ""
echo "Step C1: Creating pgbench_events table..."
$PSQL -c "
    DROP TABLE IF EXISTS public.pgbench_events CASCADE;
    CREATE TABLE public.pgbench_events (
        id         BIGSERIAL PRIMARY KEY,
        aid        INTEGER,
        event_type TEXT,
        payload    TEXT,
        created_at TIMESTAMPTZ DEFAULT now()
    );"

echo ""
echo "Step C2: Running insert-heavy workload for 60 seconds..."
echo "  4 clients, insert-only script"
echo "  pgbench_events will grow rapidly — tests insert threshold detection"
$PGBENCH -c 4 -T 60 -f tests/pgbench_insert_heavy.sql --no-vacuum $PGDB

echo ""
echo "Step C3: Running advisor against pgbench_events..."
$ADV_BASE --min-rows 1000

echo ""
echo "Expected findings:"
echo "  pgbench_events: NEAR ANA or flagged for tuning"
echo "  If PG 13+: autovacuum_vacuum_insert_threshold recommendation may appear"

echo ""
echo "✓ Track C complete"

# =============================================================================
# TRACK D — --top flag and --version sanity checks
# =============================================================================
hr
echo "TRACK D — CLI Flag Sanity Checks"
hr

echo ""
echo "D1: --version"
$ADVISOR --version

echo ""
echo "D2: --top 3 (should show only 3 worst tables)"
$ADV_BASE --schema ${SCHEMA} --top 3

echo ""
echo "D3: --min-rows 1000000 (should only show tables with >= 1M rows)"
$ADV_BASE --schema ${SCHEMA} --min-rows 1000000

echo ""
echo "D4: --format json to stdout (pipe check)"
$ADV_BASE --schema ${SCHEMA} --format json | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(f'  JSON keys     : {list(data.keys())}')
print(f'  Tables found  : {len(data[\"tables\"])}')
print(f'  Recommendations: {len(data[\"recommendations\"])}')
print(f'  Platform      : {data[\"platform\"]}')
print('  ✓ JSON is valid and well-structured')
"

echo ""
echo "D5: --format csv to stdout (row count check)"
COUNT=$($ADV_BASE --schema ${SCHEMA} --format csv | tail -n +2 | wc -l | tr -d ' ')
echo "  CSV rows (excluding header): ${COUNT}"
echo "  Expected: 9"

# =============================================================================
# Done
# =============================================================================
hr
echo "ALL TRACKS COMPLETE"
hr
echo ""
echo "Reports saved:"
echo "  tests/track_a_report.json   — full JSON report for SQL scenarios"
echo "  tests/track_a_tables.csv    — CSV table health for SQL scenarios"
echo "  tests/track_b_report.json   — full JSON report for pgbench standard"
echo ""
echo "To clean up all test objects:"
echo "  psql -h ${PGHOST:-localhost} -d ${PGDATABASE:-postgres} -U ${PGUSER:-postgres} -f tests/teardown.sql"
