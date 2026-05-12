# pg-vacuum-advisor 🧙

> PostgreSQL Autovacuum Health Checker & Tuning Advisor

Connects to your PostgreSQL database, shows exactly when autovacuum will fire
for every table, flags the ones at risk, and generates ready-to-run
`ALTER TABLE` statements to fix them.

---

## Why does this exist?

PostgreSQL's autovacuum fires on a table when:

```
dead_rows > autovacuum_vacuum_threshold + (autovacuum_vacuum_scale_factor × live_rows)
```

With the **default `scale_factor` of `0.2`**, autovacuum requires 20% of a
table's rows to be dead before it does anything.  For large tables, that
threshold is enormous:

| Table size       | Dead rows needed to trigger vacuum |
|------------------|------------------------------------|
| 100 K rows       | 20,050                             |
| 1 M rows         | 200,050                            |
| **10 M rows**    | **2,000,050**                      |
| 100 M rows       | 20,000,050                         |

Those dead rows bloat your tables, slow down sequential scans, waste storage,
and — left long enough — risk transaction ID wraparound.

The fix is simple: give large tables their own `autovacuum_vacuum_scale_factor`
using `ALTER TABLE ... SET (...)`.  This tool tells you exactly which tables
need it and gives you the SQL to paste in.

---

## Features

- **Global settings panel** — shows every autovacuum parameter with a plain-English description and the trigger formula explained
- **Per-table health table** — live rows, dead rows, dead %, the exact dead-row count that will trigger vacuum, and how close each table is to that threshold
- **Status indicators** — `⚠ HIGH BLOAT`, `⚡ NEAR TRIGGER`, `⚙ TUNE`, `✓ OK`
- **Ready-to-run ALTER TABLE statements** — for every large table that needs per-table tuning
- **XID wraparound warning** — alerts at 500 M and 200 M transactions remaining
- **Safe to run on production** — read-only session, no objects created

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

```bash
# Using a connection string
python vacuum_advisor.py --conn "postgresql://user:pass@host:5432/mydb"

# Using individual flags
python vacuum_advisor.py -H myhost -d mydb -U postgres

# Limit to one schema
python vacuum_advisor.py -H myhost -d mydb -U postgres --schema public

# Only show tables with at least 500,000 live rows
python vacuum_advisor.py -H myhost -d mydb -U postgres --min-rows 500000

# RDS / Cloud SQL — same flags, just point at your endpoint
python vacuum_advisor.py -H mydb.abc123.us-east-1.rds.amazonaws.com -d mydb -U postgres
```

Password can be passed with `-W` or via the `PGPASSWORD` environment variable.

---

## Sample output

```
╭─────────────────────────────────────────────╮
│ 🧙 pg-vacuum-advisor                         │
│ PostgreSQL Autovacuum Health Checker          │
╰─────────────────────────────────────────────╯

⚙  Global Autovacuum Settings
 Parameter                            Value   Description
 autovacuum                           on      Master on/off switch
 autovacuum_vacuum_scale_factor       0.2     Fraction of live rows that must be dead ← the big one
 autovacuum_vacuum_threshold          50      Base dead-row count added to scale_factor result
 autovacuum_naptime                   60      How often the launcher checks for work (seconds)
 autovacuum_max_workers               3       Max concurrent autovacuum workers
 ...

  Vacuum trigger formula:  dead_rows > vacuum_threshold + (vacuum_scale_factor × live_rows)

📊  Table Vacuum Health
 Schema.Table          Size     Live Rows   Dead Rows  Dead %  Trigger At  % to Trigger  Last Autovacuum    Status
 public.events         8.2 GB  45,230,100    912,400   2.0%   9,046,070        10%       2024-01-15 03:21   ⚙ TUNE
 public.orders         2.1 GB  10,500,000    210,800   2.0%   2,100,050        10%       2024-01-15 02:44   ⚙ TUNE
 public.sessions         800 MB  4,100,000   980,000  23.9%     820,050       120%       2024-01-14 08:10   ⚠ HIGH BLOAT
 public.users            120 MB    500,000     1,200   0.2%     100,050         1%       2024-01-15 01:30   ✓ OK

🔧  Per-Table Tuning Recommendations — 3 table(s)

  public.events  8.2 GB · 45,230,100 live rows
    Current  → vacuum fires at 9,046,070 dead rows  (scale_factor=0.2, threshold=50)
    Proposed → vacuum fires at   453,301 dead rows  (scale_factor=0.01, threshold=1000)

    ALTER TABLE public.events SET (
        autovacuum_vacuum_scale_factor = 0.01,
        autovacuum_vacuum_threshold    = 1000
    );
  ...

  Summary
  Tables analyzed       : 4
  High bloat (≥20% dead): 1
  Never autovacuumed    : 0
  Need per-table tuning : 3
```

---

## Understanding the recommendations

The default recommendation of `scale_factor = 0.01` means autovacuum fires
when 1% of rows are dead (instead of 20%).  For a 10 M-row table that drops
the trigger from **2,000,050** to **101,000** dead rows — far more responsive.

You can tune further based on your workload:

| Table write rate  | Suggested scale_factor |
|-------------------|------------------------|
| Low writes        | 0.05                   |
| Moderate writes   | 0.02                   |
| High writes       | 0.01                   |
| Very high writes  | 0.005                  |

After applying changes, monitor with:

```sql
SELECT schemaname, relname, n_live_tup, n_dead_tup, last_autovacuum
FROM   pg_stat_user_tables
ORDER  BY n_dead_tup DESC;
```

---

## Resetting per-table settings

To remove a per-table override and go back to the global default:

```sql
ALTER TABLE my_table RESET (
    autovacuum_vacuum_scale_factor,
    autovacuum_vacuum_threshold
);
```

---

## Related reading

- [PostgreSQL docs — routine vacuuming](https://www.postgresql.org/docs/current/routine-vacuuming.html)
- [PostgreSQL docs — autovacuum parameters](https://www.postgresql.org/docs/current/runtime-config-autovacuum.html)
- [When to Use AlloyDB Instead of Cloud SQL for PostgreSQL](https://draft.doit.com/blog/when-to-use-alloydb-instead-of-cloud-sql-for-postgresql) — by Aamir Haroon

---

## Author

**Aamir Haroon** — Senior Cloud Architect @ [DoiT International](https://www.doit.com)
[github.com/aamir814](https://github.com/aamir814) · [aamirharoon.com](https://aamirharoon.com)

---

## License

MIT
