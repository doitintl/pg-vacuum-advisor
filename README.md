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
| Aurora PostgreSQL     | 0.2 (engine default)  | 0.1 (engine default)   | Engine default; no parameter group override |
| Google Cloud SQL      | 0.2 (engine default)  | 0.1 (engine default)   | Engine default |
| Stock PostgreSQL      | 0.2                   | 0.1                    | PostgreSQL documentation |

Even with RDS's lower default of 0.1, a large table still accumulates a huge
number of dead rows before autovacuum fires:

| Table size    | RDS (scale=0.1) trigger | Aurora/Cloud SQL (scale=0.2) trigger |
|---------------|-------------------------|--------------------------------------|
| 1 M rows      | 100,050                 | 200,050                              |
| **10 M rows** | **1,000,050**           | **2,000,050**                        |
| 100 M rows    | 10,000,050              | 20,000,050                           |
| 500 M rows    | 50,000,050              | 100,000,050                          |

Those dead rows bloat your tables, slow down sequential scans, waste storage,
and — left long enough — risk transaction ID wraparound.

The fix is to give large tables their own per-table `autovacuum_vacuum_scale_factor`
via `ALTER TABLE ... SET (...)`. This tool tells you exactly which tables need
it and generates the SQL, with scale factors **tiered by table size**.

---

## Features

- **Platform-aware defaults** — pass `--platform rds`, `--platform aurora`, or
  `--platform cloudsql` to compare live settings against the correct baseline
  (RDS uses 0.1/0.05; Aurora and Cloud SQL use PostgreSQL engine defaults)
- **Global settings panel** — every autovacuum parameter with its live value,
  platform default, and a plain-English description; parameters that differ from
  the platform default are highlighted with ★
- **Vacuum + Analyze health table** — live rows, dead rows, dead %, vacuum trigger
  threshold, % to vacuum trigger, modified-since-analyze count, % to analyze
  trigger, last autovacuum timestamp, and combined status
- **Multi-status indicators** — a table can carry multiple flags simultaneously:
  `🚫 DISABLED`, `⚠ HIGH BLOAT`, `⚡ NEAR VAC`, `📈 NEAR ANA`, `✓ OK`
- **Tiered ALTER TABLE recommendations** — scale factors sized to table row count,
  covering both vacuum and analyze tuning in a single statement
- **`autovacuum_enabled=false` detection** — critical warning panel with the exact
  `RESET` SQL for each disabled table
- **XID wraparound check** — scans all databases (not just the current one), with
  RDS- and Cloud SQL-specific remediation guidance
- **`--format json|csv`** — structured output for scripting, CI pipelines, and
  monitoring; JSON includes the complete `ALTER TABLE` SQL for each recommendation
- **`--top N`** — show only the N worst tables by dead row count
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
# Uses RDS parameter group defaults: vacuum_scale=0.1, analyze_scale=0.05
python3 vacuum_advisor.py -H mydb.abc123.us-east-1.rds.amazonaws.com \
    -d mydb -U postgres --platform rds

# Aurora PostgreSQL
# Uses engine defaults: vacuum_scale=0.2, analyze_scale=0.1
python3 vacuum_advisor.py -H cluster.cluster-abc123.us-east-1.rds.amazonaws.com \
    -d mydb -U postgres --platform aurora

# Google Cloud SQL (via Cloud SQL Auth Proxy or public IP)
# Uses engine defaults: vacuum_scale=0.2, analyze_scale=0.1
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
| `--platform` | `rds` | `rds` / `aurora` / `cloudsql` — sets the platform default baseline |
| `--schema` | all | Restrict analysis to one schema |
| `--min-rows N` | 0 | Only report tables with ≥ N live rows |
| `--top N` | all | Show only top N tables by dead row count |
| `--format` | `console` | `console` / `json` / `csv` |
| `--output FILE` | stdout | Write json/csv output to a file |
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

One row per table. Columns show the current dead-row count, how far it is from
the vacuum trigger (% to Vac), the modified-row count relative to the analyze
trigger (% to Ana), and a combined status.

```
╭───────────────────────────────────╮
│ 📊  Table Vacuum & Analyze Health │
╰───────────────────────────────────╯

                                               Vac Trigger               Mod Since    % to    Last
  Schema.Table              Size    Live Rows  (dead rows)  % to Vac     Analyze       Ana   Autovac   Status
  ──────────────────────────────────────────────────────────────────────────────────────────────────────────
  public.orders           2.3 GB    8,500,000    850,050       91%       420,000       100%   2026-05   ⚡ NEAR VAC
                                                                                                         📈 NEAR ANA
  public.events †       189.0 MB    2,000,000    200,050        0%             0         0%   2026-05   ✓ OK
  public.archived_logs   56.7 MB      500,000     50,050      200%       600,000      999%      Never   🚫 DISABLED
  public.users            9.9 MB      100,000     10,050       43%         4,300        85%      Never   🚫 DISABLED

  † Table has per-table autovacuum storage parameters set
  % to Vac / % to Ana = current dead/modified rows as % of the trigger threshold (≥80% → warning)
```

**Status flags:**

| Flag | Meaning |
|------|---------|
| `✓ OK` | No issues detected |
| `⚡ NEAR VAC` | Dead rows ≥ 80% of the vacuum trigger threshold |
| `📈 NEAR ANA` | Modified rows ≥ 80% of the analyze trigger threshold |
| `⚠ HIGH BLOAT` | Dead-tuple percentage ≥ 20% |
| `🚫 DISABLED` | `autovacuum_enabled = false` is set on this table |

A table can carry multiple flags at once (e.g. `🚫 DISABLED` + `⚠ HIGH BLOAT`).

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

Shown when any database's XID age is within 50 M transactions of the forced
anti-wraparound vacuum threshold. Covers all databases in the cluster, not just
the one you connected to.

```
╭─────────────────────── Transaction ID Wraparound ────────────────────────╮
│ ⚠  XID Wraparound Approaching                                            │
│                                                                          │
│   Database : mydb (current database)                                     │
│   XID age  : 155,000,000                                                 │
│   Remaining: 45,000,000 transactions                                     │
│                                                                          │
│   Monitor closely — ensure autovacuum is keeping up on high-write tables │
╰──────────────────────────────────────────────────────────────────────────╯
```

### 7 — Summary

```
╭──────────────────────────────╮
│ Summary                      │
│                              │
│   Tables analyzed        : 9 │
│   Autovacuum disabled    : 1 │
│   High bloat (≥20% dead) : 0 │
│   Never autovacuumed     : 2 │
│   Need per-table tuning  : 1 │
╰──────────────────────────────╯
```

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
