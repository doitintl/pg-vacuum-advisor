# pg-vacuum-advisor 🧙

> PostgreSQL Autovacuum Health Checker & Tuning Advisor
> Cloud-tuned for **AWS RDS**, **Aurora PostgreSQL**, and **Google Cloud SQL**

Connects to your PostgreSQL database, shows exactly when autovacuum will fire
for every table, flags the ones at risk, and generates ready-to-run
`ALTER TABLE` statements to fix them — using the correct baseline defaults for
your cloud platform.

---

## Why does this exist?

PostgreSQL's autovacuum fires on a table when:

```
dead_rows > autovacuum_vacuum_threshold + (autovacuum_vacuum_scale_factor × live_rows)
```

The default `scale_factor` varies by cloud platform — and it's not what the
PostgreSQL documentation says:

| Platform              | `vacuum_scale_factor` | `analyze_scale_factor` | Source |
|-----------------------|-----------------------|------------------------|--------|
| AWS RDS PostgreSQL    | **0.1** (PG 12–18)    | **0.05** (PG 12–18)    | Verified via `aws rds describe-db-parameters` |
| Aurora PostgreSQL     | **0.1**               | **0.05**               | [Aurora parameter group docs](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Reference.ParameterGroups.html) |
| Google Cloud SQL      | 0.2 (engine default)  | 0.1 (engine default)   | Stock PostgreSQL engine defaults |
| Stock PostgreSQL      | 0.2                   | 0.1                    | PostgreSQL documentation |

Both AWS platforms (RDS and Aurora) apply the same parameter group overrides.
Google Cloud SQL uses stock PostgreSQL defaults.

Even with the AWS default of 0.1, a large table still accumulates a huge
number of dead rows before autovacuum fires:

| Table size    | AWS RDS/Aurora (scale=0.1) trigger | Cloud SQL (scale=0.2) trigger |
|---------------|-------------------------------------|-------------------------------|
| 1 M rows      | 100,050                             | 200,050                       |
| **10 M rows** | **1,000,050**                       | **2,000,050**                 |
| 100 M rows    | 10,000,050                          | 20,000,050                    |
| 500 M rows    | 50,000,050                          | 100,000,050                   |

Those dead rows bloat your tables, slow down sequential scans, waste storage,
and — left long enough — risk transaction ID wraparound.

The fix is to give large tables their own per-table `autovacuum_vacuum_scale_factor`
via `ALTER TABLE ... SET (...)`. This tool tells you exactly which tables need
it and generates the SQL, with scale factors **tiered by table size**.

---

## Features

- **Platform-aware defaults** — pass `--platform rds`, `--platform aurora`, or
  `--platform cloudsql` to compare live settings against the correct baseline
  (RDS and Aurora both use 0.1/0.05; Cloud SQL uses stock PostgreSQL defaults 0.2/0.1)
- **Global settings panel** — every autovacuum parameter with its live value,
  platform default, and a plain-English description; parameters that differ from
  the platform default are highlighted with ★
- **Vacuum + Analyze health table** — live rows, dead rows, dead %, vacuum trigger
  threshold, % to vacuum trigger, % to analyze trigger, last autovacuum date,
  last autoanalyze date, and combined status; tables under 50 MB are omitted
  (autovacuum handles them well with defaults); tables with autovacuum disabled
  are always shown regardless of size
- **Multi-status indicators** — a table can carry multiple flags simultaneously:
  `🚫 DISABLED`, `⚠ HIGH BLOAT`, `⚠ HIGH BLOAT (ABS)`, `⚡ NEAR VAC`, `📈 NEAR ANA`, `✓ OK`
- **Bloat ranked by percentage *and* absolute volume** — `HIGH_BLOAT` (≥20% dead)
  is percentage-only and can hide the tables that actually drive I/O: a huge table
  can sit at a "fine" 10% dead while carrying tens of millions of dead tuples.
  `HIGH_BLOAT_ABSOLUTE` fires independently (≥1M dead rows or ≥1GB estimated dead
  bytes) so those tables surface too — both flags can be set at once, and the
  original percentage-only behavior is unchanged
- **Tiered ALTER TABLE recommendations** — scale factors sized to table row count,
  covering both vacuum and analyze tuning in a single statement, with schema and
  table identifiers always properly quoted (`quote_ident()` semantics: safe for
  mixed-case names, reserved words, and embedded quotes — important for EF Core,
  Hibernate, and other ORMs that create mixed-case or quoted-reserved-word tables)
- **`autovacuum_enabled=false` detection** — critical warning panel with the exact
  `RESET` SQL (also properly quoted) for each disabled table
- **XID wraparound check** — scans all databases (not just the current one);
  background context printed once with performance impact explanation (SHARE UPDATE
  EXCLUSIVE lock, freeze cost); each database gets a CRITICAL or WARNING panel with
  its % of soft limit shown inline
- **`--all-databases`** — `pg_stat_user_tables`/`pg_class` are database-scoped, so a
  normal run only ever sees the one database it connected to. `--all-databases`
  enumerates every connectable database on the instance (via `pg_database`) and
  analyzes each in turn, merging results into a `{instance, database, report}` list.
  `template0`/`template1` and the platform's own admin database (`rdsadmin` for
  rds/aurora, `cloudsqladmin` for cloudsql) are excluded automatically;
  `--exclude-db` skips additional ones
- **`--replay JSON_FILE`** — re-render the full console output from a saved JSON
  report with no database connection required; useful for reviewing what a customer
  saw or sharing analysis with teammates. Auto-detects single-database reports
  *and* `--all-databases` merged reports (rendering each database in turn)
- **`json_to_report.py`** — companion script that converts a JSON report to a
  human-readable Markdown file; useful when you can't see the customer's console
  output but they can share the JSON file
- **`--format json|csv`** — structured output for scripting, CI pipelines, and
  monitoring; JSON includes the complete `ALTER TABLE` SQL for each recommendation.
  With `--all-databases`, JSON is the same `{instance, database, report}` list and
  CSV flattens every database's table rows into one file tagged with `instance`/
  `database` columns
- **`--top N`** — show only the N worst tables by dead row count
- **Checkpoint & WAL Health** — flags when checkpoints are firing on WAL fill
  (`checkpoints_req`) instead of the `checkpoint_timeout` timer, and recommends
  `checkpoint_timeout`/`max_wal_size` together as `ALTER SYSTEM` statements
  (see [Checkpoint & WAL Health](#checkpoint--wal-health) below)
- **Safe to run on production** — read-only session, no objects created or modified

---

## Installation

```bash
pip install -r requirements.txt
```

Or install dependencies directly:

```bash
pip install psycopg2-binary rich
```

**Requirements:** Python 3.8+, PostgreSQL 12+

---

## Required privileges

**No superuser required.** This tool only reads catalog and statistics
views — it never creates, modifies, or deletes anything (`fetch_data()`
runs with `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`). The
role you connect with needs:

1. **`LOGIN`** — obviously.
2. **`CONNECT`** on every database you want analyzed. PostgreSQL grants
   `CONNECT` on every database to `PUBLIC` by default, so any role can
   already connect anywhere unless a DBA has explicitly revoked it on a
   specific database — common in more locked-down environments. With
   `--all-databases`, this means every database it enumerates.
3. **Read access to `pg_stat_user_tables`, `pg_class`, `pg_settings`,
   and `pg_database`.** These are globally readable by any authenticated
   role in stock PostgreSQL — no grants needed in a default setup.

Point 3 is the one thing that can vary: some hardened environments
`REVOKE` default public read access to catalogs/stats. The safest fix,
rather than tracking down exactly which view lost its default grant, is
the built-in **`pg_monitor`** role (a predefined PostgreSQL role since
PG 10 — not superuser, read-only, bundles `pg_read_all_settings` +
`pg_read_all_stats` + `pg_stat_scan_tables`). It's supported on RDS,
Aurora, and Cloud SQL, and covers everything this tool queries regardless
of any hardening already in place.

### Option A — use an existing role

If your usual monitoring/read-only role already has `pg_monitor` (or
broader access), nothing else to do — just point `-U` at it.

### Option B — create a dedicated role just for this tool

Run as the instance's admin/master user (the RDS/Aurora master user has
`rds_superuser`, which includes `pg_monitor`; Cloud SQL's default user has
`cloudsqlsuperuser` — both are sufficient for the grants below, no true
superuser needed):

```sql
-- Create a dedicated, read-only role for pg-vacuum-advisor.
CREATE ROLE pgvacadvisor WITH LOGIN PASSWORD 'change-me' NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- pg_monitor: built-in, read-only, no write access anywhere — covers every
-- catalog/stats view this tool queries even if public read access has been
-- revoked on this instance.
GRANT pg_monitor TO pgvacadvisor;

-- Explicit CONNECT — belt-and-suspenders in case PUBLIC's default CONNECT
-- has been revoked on this specific database.
GRANT CONNECT ON DATABASE mydb TO pgvacadvisor;
```

For `--all-databases`, grant `CONNECT` on every database in one shot
instead of listing them by hand:

```sql
DO $$
DECLARE
    db RECORD;
BEGIN
    FOR db IN
        SELECT datname FROM pg_database
        WHERE datistemplate = false AND datallowconn = true
    LOOP
        EXECUTE format('GRANT CONNECT ON DATABASE %I TO pgvacadvisor', db.datname);
    END LOOP;
END $$;
```

Run it:

```bash
PGPASSWORD='change-me' python3 vacuum_advisor.py -H myhost -U pgvacadvisor \
    --platform rds --all-databases --format json --output report.json
```

### Cleaning up afterward

```sql
-- Revoke CONNECT on every database (mirrors the DO block above)
DO $$
DECLARE
    db RECORD;
BEGIN
    FOR db IN
        SELECT datname FROM pg_database
        WHERE datistemplate = false AND datallowconn = true
    LOOP
        EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM pgvacadvisor', db.datname);
    END LOOP;
END $$;

DROP ROLE pgvacadvisor;
```

`DROP ROLE` fails if the role owns any objects or has active connections —
it won't, since this role is only ever used to read, but if you hit that
error anyway, terminate its sessions first
(`SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'pgvacadvisor';`)
and retry.

---

## Usage

### Connection

```bash
# Full DSN
python3 vacuum_advisor.py --conn "postgresql://user:pass@host:5432/mydb" --platform rds

# Individual flags (password prompted securely with -W, or via PGPASSWORD env var)
python3 vacuum_advisor.py -H myhost -d mydb -U postgres -W --platform rds

# PGPASSWORD env var (preferred in scripts — keeps password out of process list)
PGPASSWORD=secret python3 vacuum_advisor.py -H myhost -d mydb -U postgres --platform rds
```

### Platform selection

```bash
# AWS RDS PostgreSQL (default)
# AWS parameter group defaults: vacuum_scale=0.1, analyze_scale=0.05
python3 vacuum_advisor.py -H mydb.abc123.us-east-1.rds.amazonaws.com \
    -d mydb -U postgres --platform rds

# Aurora PostgreSQL
# Same AWS parameter group defaults as RDS: vacuum_scale=0.1, analyze_scale=0.05
python3 vacuum_advisor.py -H cluster.cluster-abc123.us-east-1.rds.amazonaws.com \
    -d mydb -U postgres --platform aurora

# Google Cloud SQL (via Cloud SQL Auth Proxy or public IP)
# Stock PostgreSQL defaults: vacuum_scale=0.2, analyze_scale=0.1
python3 vacuum_advisor.py -H 127.0.0.1 -p 5432 -d mydb -U postgres --platform cloudsql
```

### Filtering

```bash
# Restrict to one schema
python3 vacuum_advisor.py -H myhost -d mydb -U postgres --platform rds --schema public

# Only analyse tables with at least 500,000 live rows
python3 vacuum_advisor.py -H myhost -d mydb -U postgres --platform rds --min-rows 500000

# Show only the 20 worst tables in the health table
python3 vacuum_advisor.py -H myhost -d mydb -U postgres --platform rds --top 20
```

### Analyzing every database on an instance

`pg_stat_user_tables` and `pg_class` are database-scoped — a normal run only
ever sees the one database it connected to. `--all-databases` loops over
every connectable database on the instance instead:

```bash
# Console: renders each database in turn, with a divider panel
python3 vacuum_advisor.py -H myhost -U postgres --platform rds --all-databases

# JSON: {instance, database, report} list — one report per database
python3 vacuum_advisor.py -H myhost -U postgres --platform rds --all-databases \
    --format json --output all_dbs_report.json

# CSV: every database's table rows flattened into one file, tagged by instance/database
python3 vacuum_advisor.py -H myhost -U postgres --platform rds --all-databases \
    --format csv --output all_dbs_tables.csv

# Skip additional databases beyond the automatic exclusions (template0/template1,
# and the platform's own admin database — rdsadmin / cloudsqladmin)
python3 vacuum_advisor.py -H myhost -U postgres --platform rds --all-databases \
    --exclude-db staging_db,reporting_db

# Tag the "instance" field in the merged output (default: the -H/--host value) —
# useful when you'll merge results from several instances afterward
python3 vacuum_advisor.py -H myhost -U postgres --platform rds --all-databases \
    --instance-label prod-cluster-1
```

Requires `-H`/`--host` (not `--conn`), since a separate connection string is
built for each database found. It connects once to `--bootstrap-db` (default:
`postgres`) purely to enumerate databases via `pg_database`, then reconnects
per database to run the actual analysis. XID wraparound data is cluster-wide
regardless, so it comes out identical across every entry either way.

If the role can't connect to one particular database (revoked `CONNECT`, an
auth mismatch, anything that fails the connection or a query — see
[Required privileges](#required-privileges)), that database is skipped with a
warning and a final "Skipped Databases" panel listing what and why — the rest
of the run continues normally. It only exits non-zero if *every* database
failed.

### Output formats

```bash
# Default: rich console output
python3 vacuum_advisor.py -H myhost -d mydb -U postgres --platform rds

# JSON — includes ALTER TABLE SQL for every recommendation
python3 vacuum_advisor.py -H myhost -d mydb -U postgres --platform rds \
    --format json --output report.json

# CSV — one row per table, suitable for spreadsheets or further analysis
python3 vacuum_advisor.py -H myhost -d mydb -U postgres --platform rds \
    --format csv --output tables.csv

# JSON to stdout (pipe-friendly)
python3 vacuum_advisor.py -H myhost -d mydb -U postgres --platform rds --format json
```

### Replaying a report (no database connection needed)

If a customer shares their JSON report file, you can re-render the full
console output exactly as they would have seen it:

```bash
python3 vacuum_advisor.py --replay report.json
```

`--replay` auto-detects the shape of the file: a plain single-database report
renders as above; a `--all-databases` merged report (a JSON list of
`{instance, database, report}` entries) renders each database in turn, each
preceded by an `Instance: X   Database: Y` header panel — no separate flag
needed, and both shapes work with the same command.

### Converting JSON to Markdown

For sharing or filing in tickets, convert the JSON to a human-readable Markdown report:

```bash
# Print to stdout
python3 json_to_report.py report.json

# Write to file
python3 json_to_report.py report.json report.md
```

The Markdown report includes XID severity flags, table statistics grouped by size
(Large >1 GB, Medium 50 MB–1 GB), per-table tuning recommendations with SQL, and
a summary. Tables under 50 MB are omitted from the stats section.

### Other flags

```bash
# Show version
python3 vacuum_advisor.py --version

# Full help
python3 vacuum_advisor.py --help
```

---

## All flags

| Flag | Default | Description |
|------|---------|-------------|
| `--conn DSN` | — | Full libpq DSN (`postgresql://user:pass@host/db`) |
| `-H HOST` | — | Hostname (alternative to `--conn`) |
| `-p PORT` | `5432` | Port |
| `-d DB` | — | Database name |
| `-U USER` | — | Database user |
| `-W` | off | Prompt for password interactively |
| `--platform` | `rds` | `rds` / `aurora` / `cloudsql` — sets the platform default baseline (`rds` and `aurora` both use 0.1/0.05; `cloudsql` uses 0.2/0.1) |
| `--schema` | all | Restrict analysis to one schema |
| `--min-rows N` | 0 | Only report tables with ≥ N live rows |
| `--top N` | all | Show only top N tables by dead row count |
| `--all-databases` | off | Analyze every connectable database on the instance (requires `-H`, not `--conn`) — see [Analyzing every database on an instance](#analyzing-every-database-on-an-instance) |
| `--bootstrap-db DB` | `postgres` | Database used only to enumerate other databases with `--all-databases` |
| `--exclude-db DB1,DB2,...` | — | Additional databases to skip with `--all-databases`, on top of the automatic exclusions (`template0`/`template1`, platform admin db) |
| `--instance-label NAME` | `-H` value | Label recorded as `"instance"` in `--all-databases` output |
| `--format` | `console` | `console` / `json` / `csv` |
| `--output FILE` | stdout | Write json/csv output to a file |
| `--replay FILE` | — | Re-render console output from a JSON report (no DB connection needed); auto-detects single-database vs. `--all-databases` merged reports |
| `--version` | — | Print version and exit |

---

## Sample output

```
python3 vacuum_advisor.py -H $PGHOST -d $PGDATABASE -U $PGUSER --platform rds --schema myapp
```

### 1 — Header

```
╭─────────────────────────────────────────────────────────╮
│ 🧙 pg-vacuum-advisor v2.1.0                             │
│ PostgreSQL Autovacuum Health Checker & Tuning Advisor   │
│                                                         │
│ Platform : AWS RDS PostgreSQL                           │
│ Server   : PostgreSQL 14.22 on ...                      │
│ Generated: 2026-05-13T23:00:49.985117+00:00             │
╰─────────────────────────────────────────────────────────╯
```

### 2 — Global Autovacuum Settings

Every autovacuum parameter, its live value, the platform default, and a
plain-English description. Parameters that deviate from the platform default
are marked **★**.

```
╭───────────────────────────────╮
│ ⚙  Global Autovacuum Settings │
╰───────────────────────────────╯

  Parameter                           Live Value   Platform Default   Description
  ──────────────────────────────────────────────────────────────────────────────
  autovacuum                                  on                 on   Master on/off switch
  autovacuum_vacuum_scale_factor             0.1                0.1   Fraction of live rows that must be dead to trigger vacuum  ← the big one
  autovacuum_analyze_scale_factor           0.05               0.05   Fraction of table rows that must change to trigger analyze
  autovacuum_naptime                        15 ★                 60   How often the launcher checks for tables needing work (s)
  ...

  ★ = differs from AWS RDS PostgreSQL default

  Vacuum trigger formula:  dead_rows > vacuum_threshold + (vacuum_scale_factor × live_rows)

  With AWS RDS PostgreSQL default scale_factor of 0.1:
    •   1 M-row table →       100,050 dead rows needed to trigger vacuum
    •  10 M-row table →     1,000,050 dead rows
    • 100 M-row table →    10,000,050 dead rows
  This is why large tables almost always need per-table settings.
```

### 3 — Autovacuum Disabled (critical, only shown when relevant)

Shown when any table has `autovacuum_enabled = false` set as a storage
parameter. Includes the exact `RESET` SQL to re-enable each table.

```
╭─────────────────────────── Autovacuum Disabled ───────────────────────────╮
│ 🚫 autovacuum_enabled = false — Action Required                           │
│                                                                           │
│   The following tables have autovacuum explicitly disabled via storage    │
│   parameters.  They will NOT be vacuumed automatically and are at high    │
│   risk of bloat and transaction ID wraparound.                            │
│                                                                           │
│     • public.orders  (2.3 GB, 8,500,000 live rows)                        │
│                                                                           │
│   Unless this was intentional (e.g. a bulk-load staging table),           │
│   re-enable autovacuum with:                                              │
│                                                                           │
│     ALTER TABLE public.orders RESET (autovacuum_enabled);                 │
╰───────────────────────────────────────────────────────────────────────────╯
```

### 4 — Table Vacuum & Analyze Health

One row per table (50 MB+ only; always includes autovacuum-disabled tables).
Columns show dead-row count, how far from the vacuum trigger (% to Vac), how far
from the analyze trigger (% to Ana), last autovacuum and autoanalyze dates, and
a combined status flag.

```
╭───────────────────────────────────╮
│ 📊  Table Vacuum & Analyze Health │
╰───────────────────────────────────╯

                                               Vac Trigger   % to   % to   Last          Last
  Schema.Table              Size    Live Rows  (dead rows)    Vac    Ana    Autovacuum    Autoanalyze   Status
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
  public.orders           2.3 GB    8,500,000    850,050       91%   100%   2026-05-12    2026-04-01    ⚡ NEAR VAC
                                                                                                        📈 NEAR ANA
  public.events †       189.0 MB    2,000,000    200,050        0%     0%   2026-05-10    2026-05-11    ✓ OK
  public.archived_logs   56.7 MB      500,000     50,050      200%   999%        Never         Never    🚫 DISABLED

  † Table has per-table autovacuum storage parameters set
  % to Vac / % to Ana = current dead/modified rows as % of the trigger threshold (≥80% → warning)
  615 table(s) < 50 MB omitted — autovacuum handles small tables well with default settings.
```

**Status flags:**

| Flag | Meaning |
|------|---------|
| `✓ OK` | No issues detected |
| `⚡ NEAR VAC` | Dead rows ≥ 80% of the vacuum trigger threshold |
| `📈 NEAR ANA` | Modified rows ≥ 80% of the analyze trigger threshold |
| `⚠ HIGH BLOAT` | Dead-tuple percentage ≥ 20% |
| `⚠ HIGH BLOAT (ABS)` | Absolute dead-row count ≥ 1M **or** estimated dead bytes ≥ 1GB — fires independently of dead-tuple percentage, so a huge table with a "fine" percentage but massive absolute volume still gets surfaced |
| `🚫 DISABLED` | `autovacuum_enabled = false` is set on this table |

A table can carry multiple flags at once (e.g. `🚫 DISABLED` + `⚠ HIGH BLOAT` +
`⚠ HIGH BLOAT (ABS)`). The two `HIGH BLOAT` flags are independent: a 400 GB
table at 12% dead can trip `HIGH BLOAT (ABS)` (its ~48 GB of estimated dead
bytes is well past the 1GB bar) while sitting well under the 20% `HIGH BLOAT`
percentage threshold — that's exactly the case this flag exists to catch.

### 5 — Per-Table Tuning Recommendations (only shown when relevant)

Tables with ≥ 1 M live rows that are under-configured get a ready-to-run
`ALTER TABLE` statement. The recommendation shows current vs. proposed trigger
thresholds and the responsiveness improvement factor.

```
╭────────────────────────────────────────────────────────────────────────────╮
│ 🔧  Per-Table Tuning Recommendations — 1 table(s)                          │
│                                                                            │
│ Scale factors are tiered by table size (cloud-tuned):                      │
│   > 500 M rows  → vacuum scale_factor = 0.0005                             │
│   > 100 M rows  → vacuum scale_factor = 0.001                              │
│   >  10 M rows  → vacuum scale_factor = 0.005                              │
│   >   1 M rows  → vacuum scale_factor = 0.01                               │
╰────────────────────────────────────────────────────────────────────────────╯

  public.orders  2.3 GB · 8,500,000 live rows · tier: >  1 M rows
    Vacuum :  currently fires at 850,050 dead rows  (scale=0.1, threshold=50)
               proposed fires at 86,000 dead rows  (scale=0.01, threshold=1000 — 10× more responsive)
    Analyze:  currently fires at 425,050 modified rows  (scale=0.05, threshold=50)
               proposed fires at 171,000 modified rows  (scale=0.02, threshold=1000)

    ALTER TABLE public.orders SET (
        autovacuum_vacuum_scale_factor  = 0.01,
        autovacuum_vacuum_threshold     = 1000,
        autovacuum_analyze_scale_factor = 0.02,
        autovacuum_analyze_threshold    = 1000
    );

    💡 Vacuum will fire ~10× more often. If indexes were already bloated
       before applying this change, consider running REINDEX CONCURRENTLY
       on the table's high-traffic indexes.
```

### 6 — XID Wraparound (only shown when relevant)

Shown when any database's XID age is within 50 M transactions of `freeze_max_age`
(the soft limit). Covers all databases in the cluster, not just the one you
connected to. Background context is printed once, then one panel per affected
database. `freeze_max_age` is a soft limit — the hard wraparound failure limit is
2^31 (~2.1 billion). CRITICAL/WARNING means anti-wraparound autovacuum is behind
schedule, not that the database is about to shut down.

```
╭──────────────── Transaction ID Wraparound — Background ─────────────────╮
│ PostgreSQL must freeze old transaction IDs to prevent wraparound         │
│ failure. autovacuum_freeze_max_age is a soft limit — once crossed,       │
│ anti-wraparound autovacuum runs aggressively to catch up.                │
│ The closer to this limit, the more expensive VACUUM becomes: it holds    │
│ a SHARE UPDATE EXCLUSIVE lock that can block DDL and degrade performance.│
│ The hard limit (actual wraparound failure) is 2^31 (~2.1 billion).       │
╰─────────────────────────────────────────────────────────────────────────╯

╭──────────────────── XID Wraparound Warning — CRITICAL ──────────────────╮
│ 🚨 Anti-Wraparound Autovacuum Is Behind Schedule                         │
│                                                                          │
│   Database       : mydb (current database)                               │
│   XID age        : 198,500,000 (99.3% of soft limit)                    │
│   Freeze max age : 200,000,000                                           │
│   Remaining      : 1,500,000 transactions until soft limit               │
│                                                                          │
│   ► Confirm anti-wraparound autovacuum is actively running.              │
│   ► Check pg_stat_activity for autovacuum workers on high-write tables.  │
╰─────────────────────────────────────────────────────────────────────────╯
```

### 7 — Summary

```
╭────────────────────────────────────────────────────────────────╮
│ Summary                                                         │
│                                                                  │
│   Tables analyzed        : 9                                    │
│   Autovacuum disabled    : 1                                    │
│   High bloat (≥20% dead) : 0                                    │
│   High bloat (absolute)  : 1  (≥1,000,000 dead rows or           │
│                                ≥1.0 GB est. dead bytes)          │
│   Never autovacuumed     : 2                                    │
│   Need per-table tuning  : 1                                    │
╰────────────────────────────────────────────────────────────────╯
```

### 8 — Multi-database output (`--all-databases`, only shown when used)

Console mode prints a divider panel before each database's full report:

```
╭─────────────────────────────────╮
│ Database: IntegrationsService   │
╰─────────────────────────────────╯

[... full report for this database, same sections as above ...]

╭─────────────────────────────────╮
│ Database: CodeScanOrchestrator  │
╰─────────────────────────────────╯

[... full report for this database ...]
```

`--format json` produces a list instead of a single report object — one entry
per database, each carrying the same `report` shape a single-database run
would have produced on its own:

```json
[
  {
    "instance": "myhost",
    "database": "IntegrationsService",
    "report": { "generated_at": "...", "pg_version": "...", "tables": [...], "summary": {...} }
  },
  {
    "instance": "myhost",
    "database": "CodeScanOrchestrator",
    "report": { "...": "..." }
  }
]
```

`--format csv` flattens every database's table rows into one file, with
`instance` and `database` columns prepended to each row.

---

## Understanding the recommendations

Recommendations are **tiered by live row count** so larger tables get more
aggressive settings:

| Table size     | Recommended `vacuum_scale_factor` | Recommended `analyze_scale_factor` |
|----------------|-----------------------------------|------------------------------------|
| > 500 M rows   | 0.0005                            | 0.001                              |
| > 100 M rows   | 0.001                             | 0.002                              |
| > 10 M rows    | 0.005                             | 0.01                               |
| > 1 M rows     | 0.01                              | 0.02                               |

For a 10 M-row table on RDS, the recommended `scale_factor = 0.005` drops the
vacuum trigger from **1,000,050** (RDS default) to **51,000** dead rows —
roughly 20× more responsive. Each recommendation includes both vacuum and
analyze tuning in a single `ALTER TABLE` statement, plus an index bloat hint.

After applying changes, monitor with:

```sql
SELECT schemaname, relname, n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze
FROM   pg_stat_user_tables
ORDER  BY n_dead_tup DESC;
```

---

## Checkpoint & WAL Health

A checkpoint fires one of two ways: on the `checkpoint_timeout` timer, or the
moment WAL written since the last checkpoint approaches `max_wal_size`
(tracked as `checkpoints_req` — "requested"). When checkpoints are mostly
`checkpoints_req` rather than `checkpoints_timed`, it means WAL is filling
faster than the timer would otherwise trigger a checkpoint — usually because
`max_wal_size` (and often `checkpoint_timeout`) are undersized for the
current write rate.

This tool computes `checkpoints_req_pct` from `pg_stat_bgwriter` (PG ≤ 16) or
`pg_stat_checkpointer` (PG ≥ 17, which split checkpointer-specific counters
out of `pg_stat_bgwriter`), combines it with the WAL generation rate from
`pg_stat_wal` (PG ≥ 14), and flags `CHECKPOINT_PRESSURE` once requested
checkpoints reach **50%** of the total. Above that bar, it recommends raising
`checkpoint_timeout` and `max_wal_size` **together** — raising one without the
other just delays the same problem — sized per the rule from
[postgresqlco.nf's annotated `max_wal_size` docs](https://postgresqlco.nf/doc/en/param/max_wal_size/18/):
below roughly 1 GB/hour of sustained WAL, PostgreSQL's stock default is fine;
above that, size `max_wal_size` to at least one hour of WAL at the current
rate (never smaller than what's already configured).

```
╭─ Checkpoint & WAL Health ──────────────────────────────────────────╮
│ ⚠ 83.7% of checkpoints are requested (WAL-triggered), not timed     │
│                                                                      │
│   Checkpoints (since 2026-08-16)  : 14,700 (2,394 timed / 12,306 req)│
│   Avg time between checkpoints    : 3.4 min                         │
│   WAL generated                   : 43.7 TiB  (~53 GiB/hour avg)    │
│   checkpoint_timeout (current)    : 300s                            │
│   max_wal_size (current)          : 6144 MB                         │
│                                                                      │
│   Recommended (raise together):                                     │
│     checkpoint_timeout = 1200s                                      │
│     max_wal_size       = 54272 MB   [≥ 1 hour of WAL at current     │
│                                       rate — sized from the          │
│                                       pg_stat_wal average]           │
│                                                                      │
│   ALTER SYSTEM SET checkpoint_timeout = '1200s';                    │
│   ALTER SYSTEM SET max_wal_size = '54272MB';                         │
│                                                                      │
│   Caveat: more WAL between checkpoints means longer crash/failover  │
│   recovery — a conscious durability trade, reversible, no reboot    │
│   needed (both are dynamic GUCs).                                   │
╰──────────────────────────────────────────────────────────────────────╯
```

Notes:

- **Instance-wide, not per-table.** These stats aren't scoped to a database,
  so with `--all-databases` this is fetched once per instance and the same
  result is attached to every database's report (and shown once, not once
  per database, in console/`--replay` output) — the same pattern already
  used for XID wraparound data.
- **`backend_write_pct` is `null` on PG ≥ 16** — `buffers_backend` was removed
  from `pg_stat_bgwriter` in PG 16 and folded into `pg_stat_io`, which this
  tool doesn't query yet. Everything else (the checkpoint-vs-timer
  percentage, the WAL rate, and the recommendation) is unaffected.
- **RDS auto-configures `max_wal_size` from allocated storage** on PG 16+
  (e.g. 6 GB for ≥100 GB of allocated storage) — this tool always reads the
  *live* value via `pg_settings`, not the parameter-group API, so it already
  reflects that auto-configuration correctly. A large `max_wal_size` on RDS
  is not by itself evidence someone manually overrode it, so there's no
  ★-style "differs from platform default" flag for this setting.
- **CSV export doesn't include this section** — it's cluster-level, not
  per-table, so there's no natural row to attach it to. JSON and console
  output are the only places it appears; `checkpoint_health` is a top-level
  JSON key (`null` when the underlying stats couldn't be read at all —
  permissions issue, very old PG — rather than the key being omitted).
- Older JSON reports made before this feature still `--replay` fine —
  `checkpoint_health` is optional and simply omitted from the console output
  when absent.

---

## Resetting per-table settings

To remove a per-table override and return to the global (platform) default:

```sql
ALTER TABLE my_table RESET (
    autovacuum_vacuum_scale_factor,
    autovacuum_vacuum_threshold,
    autovacuum_analyze_scale_factor,
    autovacuum_analyze_threshold
);
```

---

## Related reading

- [PostgreSQL docs — routine vacuuming](https://www.postgresql.org/docs/current/routine-vacuuming.html)
- [PostgreSQL docs — autovacuum parameters](https://www.postgresql.org/docs/current/runtime-config-autovacuum.html)
- [AWS — Working with PostgreSQL autovacuum on RDS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.Autovacuum.html)
- [AWS — Working with PostgreSQL autovacuum on Aurora](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Appendix.PostgreSQL.CommonDBATasks.Autovacuum.html)
- [Google Cloud SQL — Deep dive into PostgreSQL VACUUM](https://cloud.google.com/blog/products/databases/deep-dive-into-postgresql-vacuum-garbage-collector)
- [When to Use AlloyDB Instead of Cloud SQL for PostgreSQL](https://draft.doit.com/blog/when-to-use-alloydb-instead-of-cloud-sql-for-postgresql) — by Aamir Haroon

---

## Author

**Aamir Haroon** — Senior Cloud Architect @ [DoiT International](https://www.doit.com)
[github.com/aamir814](https://github.com/aamir814) · [aamirharoon.com](https://aamirharoon.com)

---

## License

MIT
