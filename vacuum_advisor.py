#!/usr/bin/env python3
"""
pg-vacuum-advisor
-----------------
PostgreSQL Autovacuum Health Checker & Tuning Advisor
Optimized for cloud-hosted databases: AWS RDS, Aurora PostgreSQL & Google Cloud SQL

Connects to a PostgreSQL database, analyzes vacuum AND analyze health across
all user tables, and generates ready-to-run ALTER TABLE recommendations for
tables that need per-table tuning.

Autovacuum fires on a table when:
    dead_rows > vacuum_threshold + (vacuum_scale_factor × live_rows)

Platform default scale_factors (verified via AWS API / GCP docs):
  AWS RDS PostgreSQL  : vacuum_scale=0.1,  analyze_scale=0.05  (RDS overrides PG defaults)
  Aurora PostgreSQL   : vacuum_scale=0.2,  analyze_scale=0.1   (engine defaults)
  Google Cloud SQL    : vacuum_scale=0.2,  analyze_scale=0.1   (engine defaults)

With RDS's default scale_factor of 0.1, a 10M-row table still needs 1,000,050
dead rows before autovacuum fires.  This tool shows you that math for every
table and tells you exactly what to change — with thresholds tiered by table size.

Usage:
    python vacuum_advisor.py --conn "postgresql://user:pass@host:5432/mydb" --platform rds
    python vacuum_advisor.py -H localhost -d mydb -U postgres --platform aurora
    python vacuum_advisor.py -H localhost -d mydb -U postgres --platform cloudsql
    python vacuum_advisor.py -H localhost -d mydb -U postgres --schema public
    python vacuum_advisor.py -H localhost -d mydb -U postgres --min-rows 100000
    python vacuum_advisor.py -H localhost -d mydb -U postgres --top 20
    python vacuum_advisor.py -H localhost -d mydb -U postgres --format json --output report.json
    python vacuum_advisor.py -H localhost -d mydb -U postgres --format csv  --output tables.csv

Author : Aamir Haroon  (github.com/aamir814)
License: MIT
"""

import argparse
import csv
import getpass
import io
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

try:
    import psycopg2
    import psycopg2.extras
    from psycopg2 import sql as pgsql
except ImportError:
    print("psycopg2 is required.  Install with:  pip install psycopg2-binary")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
except ImportError:
    print("rich is required.  Install with:  pip install rich")
    sys.exit(1)

__version__ = "2.1.0"

console = Console()

# ── Per-Platform Defaults ─────────────────────────────────────────────────────
# Verified by querying AWS RDS default parameter groups directly via:
#   aws rds describe-db-parameters --db-parameter-group-name default.postgres<VER>
# and cross-checked against Google Cloud SQL documentation.
#
# Key finding: RDS overrides two scale factors vs stock PostgreSQL / Aurora / Cloud SQL:
#   autovacuum_vacuum_scale_factor : RDS=0.1   (PG stock=0.2, Aurora=0.2, CloudSQL=0.2)
#   autovacuum_analyze_scale_factor: RDS=0.05  (PG stock=0.1, Aurora=0.1, CloudSQL=0.1)
# All other autovacuum parameters are at engine defaults across all platforms.
#
# Note: autovacuum_vacuum_cost_delay changed from 20 ms → 2 ms in PG 13.
# Note: maintenance_work_mem on RDS/Aurora is instance-size-dependent:
#       GREATEST({DBInstanceClassMemory/63963136*1024}, 65536) — shown as live value.

# Base defaults shared by Aurora and Cloud SQL (stock PostgreSQL engine defaults)
_PG_ENGINE_DEFAULTS: Dict[str, str] = {
    "autovacuum":                            "on",
    "autovacuum_vacuum_threshold":           "50",
    "autovacuum_vacuum_scale_factor":        "0.2",
    "autovacuum_analyze_threshold":          "50",
    "autovacuum_analyze_scale_factor":       "0.1",
    "autovacuum_vacuum_cost_delay":          "2",        # ms (PG 13+; was 20 ms before)
    "autovacuum_vacuum_cost_limit":          "200",
    "autovacuum_naptime":                    "60",       # seconds
    "autovacuum_max_workers":                "3",
    "autovacuum_freeze_max_age":             "200000000",
    "autovacuum_vacuum_insert_threshold":    "1000",     # PG 13+
    "autovacuum_vacuum_insert_scale_factor": "0.2",      # PG 13+
    "maintenance_work_mem":                  "65536",    # kB = 64 MB (instance-tuned on cloud)
}

# RDS overrides vacuum and analyze scale factors (confirmed PG 12–18 via AWS API)
_RDS_OVERRIDES: Dict[str, str] = {
    "autovacuum_vacuum_scale_factor":  "0.1",   # half of PG default
    "autovacuum_analyze_scale_factor": "0.05",  # half of PG default
}

PLATFORM_DEFAULTS: Dict[str, Dict[str, str]] = {
    "rds":      {**_PG_ENGINE_DEFAULTS, **_RDS_OVERRIDES},
    "aurora":   {**_PG_ENGINE_DEFAULTS},   # engine defaults; rds.adaptive_autovacuum is ON
    "cloudsql": {**_PG_ENGINE_DEFAULTS},   # engine defaults
}

PLATFORM_LABELS: Dict[str, str] = {
    "rds":      "AWS RDS PostgreSQL",
    "aurora":   "Aurora PostgreSQL",
    "cloudsql": "Google Cloud SQL",
}

# Convenience alias used throughout — set in main() based on --platform flag
_platform_defaults: Dict[str, str] = _PG_ENGINE_DEFAULTS  # overwritten at startup

# ── Thresholds ─────────────────────────────────────────────────────────────────
HIGH_DEAD_PCT          = 20.0         # Dead-tuple % considered high bloat
NEAR_TRIGGER_PCT       = 80.0         # % of trigger threshold = "near trigger" warning
# Remaining transactions until the forced anti-wraparound vacuum kicks in
# (freeze_max_age - xid_age).  These thresholds are intentionally well inside
# the freeze window so you get advance warning.
XID_WARNING_REMAINING  = 50_000_000   # < 50 M remaining → warn
XID_CRITICAL_REMAINING = 10_000_000   # < 10 M remaining → critical

# Tiered recommended vacuum scale_factor by live row count (cloud-tuned).
# Each entry: (min_live_rows, recommended_scale_factor, tier_label)
SCALE_TIERS: List[Tuple[int, float, str]] = [
    (500_000_000, 0.0005, "> 500 M rows"),
    (100_000_000, 0.001,  "> 100 M rows"),
    ( 10_000_000, 0.005,  ">  10 M rows"),
    (  1_000_000, 0.01,   ">   1 M rows"),
]
RECOMMENDED_THRESHOLD = 1_000  # vacuum/analyze threshold for large tables

# ── SQL ────────────────────────────────────────────────────────────────────────
SQL_SETTINGS = """
    SELECT name, setting
    FROM   pg_settings
    WHERE  name IN (
        'autovacuum',
        'autovacuum_vacuum_threshold',
        'autovacuum_vacuum_scale_factor',
        'autovacuum_analyze_threshold',
        'autovacuum_analyze_scale_factor',
        'autovacuum_vacuum_cost_delay',
        'autovacuum_vacuum_cost_limit',
        'autovacuum_naptime',
        'autovacuum_max_workers',
        'autovacuum_freeze_max_age',
        'autovacuum_vacuum_insert_threshold',
        'autovacuum_vacuum_insert_scale_factor',
        'maintenance_work_mem'
    )
    ORDER BY name;
"""

# {where_clause} is filled in via psycopg2.sql composition — never string format
SQL_TABLES = """
    SELECT
        s.schemaname,
        s.relname                                                       AS tablename,
        s.n_live_tup,
        s.n_dead_tup,
        s.last_autovacuum,
        s.last_vacuum,
        s.last_autoanalyze,
        s.last_analyze,
        s.autovacuum_count,
        s.autoanalyze_count,
        s.n_mod_since_analyze,
        CASE
            WHEN s.n_live_tup + s.n_dead_tup > 0
            THEN ROUND(100.0 * s.n_dead_tup / (s.n_live_tup + s.n_dead_tup), 2)
            ELSE 0
        END                                                             AS dead_pct,
        pg_total_relation_size(s.relid)                                AS total_size_bytes,
        c.reloptions
    FROM  pg_stat_user_tables s
    JOIN  pg_class c ON c.oid = s.relid
    {where_clause}
    ORDER BY s.n_dead_tup DESC, s.n_live_tup DESC;
"""

# Fetches XID age for ALL databases — wraparound risk can exist in any of them
SQL_XID = """
    SELECT
        datname,
        age(datfrozenxid)                                              AS xid_age,
        current_setting('autovacuum_freeze_max_age')::bigint           AS freeze_max_age
    FROM  pg_database
    WHERE datistemplate = false
    ORDER BY xid_age DESC;
"""

SQL_VERSION = "SELECT version();"

# ── Data Classes ───────────────────────────────────────────────────────────────
@dataclass
class TableHealth:
    schema:               str
    table:                str
    n_live:               int
    n_dead:               int
    dead_pct:             float
    size_bytes:           int
    last_autovacuum:      Optional[datetime]
    last_autoanalyze:     Optional[datetime]
    n_mod_since_analyze:  int
    vacuum_trigger:       int
    vacuum_pct:           float   # % of dead rows relative to vacuum trigger
    analyze_trigger:      int
    analyze_pct:          float   # % of modified rows relative to analyze trigger
    has_vacuum_override:  bool
    has_analyze_override: bool
    autovacuum_enabled:   bool
    vac_scale:            float
    vac_threshold:        float
    ana_scale:            float
    ana_threshold:        float
    statuses:             List[str] = field(default_factory=list)


@dataclass
class Recommendation:
    schema:             str
    table:              str
    n_live:             int
    size_bytes:         int
    tier_label:         str
    # vacuum
    cur_vac_scale:      float
    cur_vac_threshold:  float
    cur_vac_trigger:    int
    new_vac_scale:      float
    new_vac_threshold:  int
    new_vac_trigger:    int
    needs_vacuum:       bool
    # analyze
    cur_ana_scale:      float
    cur_ana_threshold:  float
    new_ana_scale:      float
    new_ana_threshold:  int
    needs_analyze:      bool


@dataclass
class AdvisorReport:
    pg_version:       str
    platform:         str           # "rds" | "aurora" | "cloudsql"
    platform_label:   str           # human-readable label
    platform_defaults: Dict[str, str]
    settings:         Dict[str, str]
    tables:           List[TableHealth]
    recommendations:  List[Recommendation]
    xid_rows:         List[Dict]
    generated_at:     str
    current_db:       str = ""      # database the tool connected to


# ── Helpers ────────────────────────────────────────────────────────────────────
def fmt_bytes(n: int) -> str:
    """Format bytes to a human-readable string."""
    val = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(val) < 1024:
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} PB"


def fmt_num(n) -> str:
    """Format a number with thousands separators."""
    return f"{int(n or 0):,}"


def parse_reloptions(reloptions) -> Dict[str, str]:
    """Parse pg_class.reloptions list into a plain dict, skipping malformed entries."""
    if not reloptions:
        return {}
    # Guard against entries with no '=' (corrupt/custom extensions)
    return {k: v for k, sep, v in (opt.partition("=") for opt in reloptions) if sep}


def effective(
    param: str,
    relopts: Dict[str, str],
    gsettings: Dict[str, str],
) -> Tuple[float, bool]:
    """Return (value, is_table_override) for a vacuum/analyze parameter.

    Resolution order: per-table reloption → live GUC → platform default fallback.
    """
    if param in relopts:
        return float(relopts[param]), True
    return float(gsettings.get(param, _platform_defaults.get(param, "0"))), False


def calc_trigger(n_live: int, threshold: float, scale: float) -> int:
    """Dead/modified row count at which autovacuum will fire."""
    return int(threshold + scale * n_live)


def recommended_scale(n_live: int) -> float:
    """Tiered vacuum scale_factor recommendation based on live row count."""
    for min_rows, scale, _ in SCALE_TIERS:
        if n_live >= min_rows:
            return scale
    return 0.01


def tier_label(n_live: int) -> str:
    for min_rows, _, label in SCALE_TIERS:
        if n_live >= min_rows:
            return label
    return "> 1 M rows"


def build_alter_sql(rec: Recommendation) -> str:
    """Generate the ALTER TABLE statement for a recommendation."""
    fqtn   = f"{rec.schema}.{rec.table}"
    params = []
    if rec.needs_vacuum:
        params.append(f"    autovacuum_vacuum_scale_factor  = {rec.new_vac_scale}")
        params.append(f"    autovacuum_vacuum_threshold     = {rec.new_vac_threshold}")
    if rec.needs_analyze:
        params.append(f"    autovacuum_analyze_scale_factor = {rec.new_ana_scale}")
        params.append(f"    autovacuum_analyze_threshold    = {rec.new_ana_threshold}")
    return f"ALTER TABLE {fqtn} SET (\n" + ",\n".join(params) + "\n);"


# ── Data Fetching ──────────────────────────────────────────────────────────────
def fetch_data(
    conn_string: str,
    schema: Optional[str],
    min_rows: int,
) -> Tuple[Dict[str, str], List[Dict], List[Dict], str, str]:
    """Open a read-only connection, run all queries, return raw data.

    Uses psycopg2.sql composition for all user-supplied values to prevent
    SQL injection.

    Returns: (gsettings, table_rows, xid_rows, pg_version, current_db)
    """
    try:
        conn = psycopg2.connect(conn_string)
        conn.set_session(readonly=True, autocommit=True)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Global autovacuum settings
        cur.execute(SQL_SETTINGS)
        gsettings: Dict[str, str] = {r["name"]: r["setting"] for r in cur.fetchall()}

        # Server version string and current database name
        cur.execute(SQL_VERSION)
        pg_version: str = cur.fetchone()["version"]  # type: ignore[index]
        cur.execute("SELECT current_database() AS dbname")
        current_db: str = cur.fetchone()["dbname"]  # type: ignore[index]

        # Build WHERE clause safely — no raw user input in SQL string
        conditions = [
            pgsql.SQL(
                "s.schemaname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
            )
        ]
        if schema:
            conditions.append(
                pgsql.SQL("s.schemaname = {}").format(pgsql.Literal(schema))
            )
        if min_rows > 0:
            conditions.append(
                pgsql.SQL("s.n_live_tup >= {}").format(pgsql.Literal(min_rows))
            )
        where_clause = pgsql.SQL("WHERE ") + pgsql.SQL(" AND ").join(conditions)
        query = pgsql.SQL(SQL_TABLES).format(where_clause=where_clause)

        cur.execute(query)
        rows: List[Dict] = cur.fetchall()  # type: ignore[assignment]

        # XID wraparound data for ALL non-template databases
        cur.execute(SQL_XID)
        xid_rows: List[Dict] = cur.fetchall()  # type: ignore[assignment]

        cur.close()
        conn.close()

    except psycopg2.OperationalError as e:
        console.print(f"\n[bold red]Could not connect:[/bold red] {e}")
        sys.exit(1)
    except psycopg2.Error as e:
        console.print(f"\n[bold red]Database error:[/bold red] {e}")
        sys.exit(1)

    return gsettings, rows, xid_rows, pg_version, current_db


# ── Analysis ───────────────────────────────────────────────────────────────────
def analyze_table_row(row: Dict, gsettings: Dict[str, str]) -> TableHealth:
    """Compute full health metrics for a single table row."""
    relopts = parse_reloptions(row["reloptions"])
    n_live  = int(row["n_live_tup"]          or 0)
    n_dead  = int(row["n_dead_tup"]          or 0)
    n_mod   = int(row["n_mod_since_analyze"] or 0)
    dead_pct = float(row["dead_pct"]         or 0)

    # autovacuum_enabled=false in reloptions disables autovacuum for this table
    av_raw     = relopts.get("autovacuum_enabled", "true").strip().lower()
    av_enabled = av_raw not in ("false", "0", "off", "no")

    vac_scale,  vac_override = effective("autovacuum_vacuum_scale_factor",  relopts, gsettings)
    vac_thresh, _            = effective("autovacuum_vacuum_threshold",      relopts, gsettings)
    ana_scale,  ana_override = effective("autovacuum_analyze_scale_factor",  relopts, gsettings)
    ana_thresh, _            = effective("autovacuum_analyze_threshold",     relopts, gsettings)

    v_trigger = calc_trigger(n_live, vac_thresh, vac_scale)
    v_pct     = min(round(n_dead / v_trigger * 100, 1), 999) if v_trigger > 0 else 0.0

    a_trigger = calc_trigger(n_live, ana_thresh, ana_scale)
    a_pct     = min(round(n_mod  / a_trigger * 100, 1), 999) if a_trigger > 0 else 0.0

    # Build a list of status flags — a table can have multiple simultaneously
    statuses: List[str] = []
    if not av_enabled:
        statuses.append("DISABLED")
    if dead_pct >= HIGH_DEAD_PCT:
        statuses.append("HIGH_BLOAT")
    if av_enabled and v_pct >= NEAR_TRIGGER_PCT:
        statuses.append("NEAR_VACUUM_TRIGGER")
    if av_enabled and a_pct >= NEAR_TRIGGER_PCT:
        statuses.append("NEAR_ANALYZE_TRIGGER")
    if not statuses:
        statuses.append("OK")

    return TableHealth(
        schema=row["schemaname"],
        table=row["tablename"],
        n_live=n_live,
        n_dead=n_dead,
        dead_pct=dead_pct,
        size_bytes=int(row["total_size_bytes"] or 0),
        last_autovacuum=row["last_autovacuum"],
        last_autoanalyze=row["last_autoanalyze"],
        n_mod_since_analyze=n_mod,
        vacuum_trigger=v_trigger,
        vacuum_pct=v_pct,
        analyze_trigger=a_trigger,
        analyze_pct=a_pct,
        has_vacuum_override=vac_override,
        has_analyze_override=ana_override,
        autovacuum_enabled=av_enabled,
        vac_scale=vac_scale,
        vac_threshold=vac_thresh,
        ana_scale=ana_scale,
        ana_threshold=ana_thresh,
        statuses=statuses,
    )


def build_recommendations(
    tables: List[TableHealth],
) -> List[Recommendation]:
    """Identify tables that need per-table vacuum and/or analyze tuning."""
    recs: List[Recommendation] = []

    for t in tables:
        if t.n_live < 1_000_000:
            continue
        if not t.autovacuum_enabled:
            continue  # disabled table — flagged separately, can't tune it

        rec_vac_scale = recommended_scale(t.n_live)
        rec_ana_scale = round(rec_vac_scale * 2, 6)  # analyze can be less aggressive

        # Needs vacuum tuning? (scale too high and not already overridden to a good value)
        already_vac_tuned = t.has_vacuum_override and t.vac_scale <= rec_vac_scale * 5
        needs_vacuum = not already_vac_tuned and t.vac_scale > rec_vac_scale * 2

        # Needs analyze tuning?
        already_ana_tuned = t.has_analyze_override and t.ana_scale <= rec_ana_scale * 5
        needs_analyze = not already_ana_tuned and t.ana_scale > rec_ana_scale * 2

        if not needs_vacuum and not needs_analyze:
            continue

        recs.append(Recommendation(
            schema=t.schema,
            table=t.table,
            n_live=t.n_live,
            size_bytes=t.size_bytes,
            tier_label=tier_label(t.n_live),
            # vacuum
            cur_vac_scale=t.vac_scale,
            cur_vac_threshold=t.vac_threshold,
            cur_vac_trigger=t.vacuum_trigger,
            new_vac_scale=rec_vac_scale         if needs_vacuum  else t.vac_scale,
            new_vac_threshold=RECOMMENDED_THRESHOLD if needs_vacuum else int(t.vac_threshold),
            new_vac_trigger=calc_trigger(t.n_live, RECOMMENDED_THRESHOLD, rec_vac_scale)
                            if needs_vacuum else t.vacuum_trigger,
            needs_vacuum=needs_vacuum,
            # analyze
            cur_ana_scale=t.ana_scale,
            cur_ana_threshold=t.ana_threshold,
            new_ana_scale=rec_ana_scale          if needs_analyze else t.ana_scale,
            new_ana_threshold=RECOMMENDED_THRESHOLD if needs_analyze else int(t.ana_threshold),
            needs_analyze=needs_analyze,
        ))

    return recs


def build_report(
    gsettings: Dict[str, str],
    raw_rows: List[Dict],
    xid_rows: List[Dict],
    pg_version: str,
    platform: str,
    current_db: str = "",
) -> AdvisorReport:
    tables = [analyze_table_row(r, gsettings) for r in raw_rows]
    recs   = build_recommendations(tables)
    return AdvisorReport(
        pg_version=pg_version,
        platform=platform,
        platform_label=PLATFORM_LABELS.get(platform, platform),
        platform_defaults=PLATFORM_DEFAULTS.get(platform, _PG_ENGINE_DEFAULTS),
        settings=gsettings,
        tables=tables,
        recommendations=recs,
        xid_rows=[dict(x) for x in xid_rows],
        generated_at=datetime.now(timezone.utc).isoformat(),
        current_db=current_db,
    )


# ── Display ────────────────────────────────────────────────────────────────────
SETTING_DESCRIPTIONS: Dict[str, str] = {
    "autovacuum":
        "Master on/off switch for autovacuum",
    "autovacuum_vacuum_threshold":
        "Base dead-row count added to the scale_factor result",
    "autovacuum_vacuum_scale_factor":
        "Fraction of live rows that must be dead to trigger vacuum  ← the big one",
    "autovacuum_analyze_threshold":
        "Base row-change count for analyze trigger",
    "autovacuum_analyze_scale_factor":
        "Fraction of table rows that must change to trigger analyze",
    "autovacuum_naptime":
        "How often the autovacuum launcher checks for tables needing work (s)",
    "autovacuum_max_workers":
        "Max concurrent autovacuum worker processes",
    "autovacuum_vacuum_cost_delay":
        "Throttle pause between I/O cost rounds (ms) — higher = slower/gentler",
    "autovacuum_vacuum_cost_limit":
        "I/O cost budget consumed before a throttle pause kicks in",
    "autovacuum_freeze_max_age":
        "Max XID age before a forced anti-wraparound vacuum is triggered",
    "autovacuum_vacuum_insert_threshold":
        "Inserted-row count before autovacuum fires (PG 13+)",
    "autovacuum_vacuum_insert_scale_factor":
        "Fraction of inserted rows that trigger autovacuum (PG 13+)",
    "maintenance_work_mem":
        "Memory available per vacuum / index build operation (kB)",
}


def show_header(report: AdvisorReport) -> None:
    console.print()
    console.print(Panel(
        f"[bold green]🧙 pg-vacuum-advisor v{__version__}[/bold green]\n"
        "[dim]PostgreSQL Autovacuum Health Checker & Tuning Advisor[/dim]\n\n"
        f"[dim]Platform : {report.platform_label}[/dim]\n"
        f"[dim]Server   : {report.pg_version[:80]}[/dim]\n"
        f"[dim]Generated: {report.generated_at}[/dim]",
        expand=False,
    ))


def show_settings(report: AdvisorReport) -> None:
    console.print()
    console.print(Panel("[bold cyan]⚙  Global Autovacuum Settings[/bold cyan]", expand=False))

    t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", padding=(0, 1))
    t.add_column("Parameter",          style="cyan",  no_wrap=True)
    t.add_column("Live Value",         style="white", justify="right")
    t.add_column("Platform Default",   style="dim",   justify="right")
    t.add_column("Description",        style="dim")

    for param, desc in SETTING_DESCRIPTIONS.items():
        if param not in report.settings:
            continue
        live  = report.settings[param]
        dflt  = report.platform_defaults.get(param, "—")
        # Highlight parameters that differ from the platform default
        live_cell = f"[bold yellow]{live} ★[/bold yellow]" if live != dflt else live
        t.add_row(param, live_cell, dflt, desc)

    # Show the platform-specific trigger example
    vac_scale = float(report.platform_defaults.get("autovacuum_vacuum_scale_factor", "0.2"))
    vac_thresh = float(report.platform_defaults.get("autovacuum_vacuum_threshold", "50"))
    eg_1m  = fmt_num(calc_trigger(1_000_000,   vac_thresh, vac_scale))
    eg_10m = fmt_num(calc_trigger(10_000_000,  vac_thresh, vac_scale))
    eg_100m= fmt_num(calc_trigger(100_000_000, vac_thresh, vac_scale))

    console.print(t)
    console.print(
        f"  [dim]★ = differs from {report.platform_label} default[/dim]\n\n"
        "  [bold]Vacuum trigger formula:[/bold]  "
        "[cyan]dead_rows > vacuum_threshold + (vacuum_scale_factor × live_rows)[/cyan]\n\n"
        f"  [dim]With {report.platform_label} default scale_factor of {vac_scale}:\n"
        f"    •   1 M-row table →  {eg_1m:>12} dead rows needed to trigger vacuum\n"
        f"    •  10 M-row table →  {eg_10m:>12} dead rows\n"
        f"    • 100 M-row table →  {eg_100m:>12} dead rows\n"
        "  This is why large tables almost always need per-table settings.[/dim]"
    )


def show_xid_warnings(report: AdvisorReport) -> None:
    """Show XID wraparound warnings for ALL databases, not just the current one."""
    for xid in report.xid_rows:
        remaining = int(xid["freeze_max_age"]) - int(xid["xid_age"])
        tag = " [dim](current database)[/dim]" if xid["datname"] == report.current_db else ""

        if remaining < XID_CRITICAL_REMAINING:
            console.print()
            console.print(Panel(
                f"[bold red]🚨 CRITICAL — XID Wraparound Risk[/bold red]\n\n"
                f"  Database      : {xid['datname']}{tag}\n"
                f"  XID age       : {fmt_num(xid['xid_age'])}\n"
                f"  Freeze max age: {fmt_num(xid['freeze_max_age'])}\n"
                f"  Remaining     : [bold red]{fmt_num(remaining)} transactions[/bold red]\n\n"
                "  ► Run VACUUM FREEZE ANALYZE on heavily-updated tables immediately.\n"
                "  ► If autovacuum is disabled on any table, re-enable it now.\n"
                "  ► AWS RDS    : check Enhanced Monitoring → autovacuum worker activity.\n"
                "  ► Cloud SQL  : check System Insights → 'PostgreSQL autovacuum' metric.",
                title="[bold red]Transaction ID Wraparound[/bold red]",
                expand=False,
            ))
        elif remaining < XID_WARNING_REMAINING:
            console.print()
            console.print(Panel(
                f"[bold yellow]⚠  XID Wraparound Approaching[/bold yellow]\n\n"
                f"  Database : {xid['datname']}{tag}\n"
                f"  XID age  : {fmt_num(xid['xid_age'])}\n"
                f"  Remaining: [yellow]{fmt_num(remaining)} transactions[/yellow]\n\n"
                "  Monitor closely — ensure autovacuum is keeping up on high-write tables.\n"
                "  ► AWS RDS   : confirm autovacuum_freeze_max_age in your parameter group.\n"
                "  ► Cloud SQL : use pg_stat_user_tables.n_dead_tup to track progress.",
                title="[yellow]Transaction ID Wraparound Warning[/yellow]",
                expand=False,
            ))


def _status_rich(statuses: List[str]) -> str:
    """Convert a list of status flags to a Rich-formatted display string."""
    parts: List[str] = []
    if "DISABLED"             in statuses: parts.append("[bold red]🚫 DISABLED[/bold red]")
    if "HIGH_BLOAT"           in statuses: parts.append("[bold red]⚠ HIGH BLOAT[/bold red]")
    if "NEAR_VACUUM_TRIGGER"  in statuses: parts.append("[bold yellow]⚡ NEAR VAC[/bold yellow]")
    if "NEAR_ANALYZE_TRIGGER" in statuses: parts.append("[bold yellow]📈 NEAR ANA[/bold yellow]")
    if statuses == ["OK"]:                 parts.append("[green]✓ OK[/green]")
    return " ".join(parts)


def show_disabled_tables(report: AdvisorReport) -> None:
    """Warn about tables that have autovacuum explicitly disabled."""
    disabled = [t for t in report.tables if not t.autovacuum_enabled]
    if not disabled:
        return

    body = (
        "[bold red]🚫 autovacuum_enabled = false — Action Required[/bold red]\n\n"
        "  The following tables have autovacuum explicitly disabled via storage\n"
        "  parameters.  They will NOT be vacuumed automatically and are at high\n"
        "  risk of bloat and transaction ID wraparound.\n\n"
        + "\n".join(
            f"    • {t.schema}.{t.table}  "
            f"({fmt_bytes(t.size_bytes)}, {fmt_num(t.n_live)} live rows)"
            for t in disabled
        )
        + "\n\n"
        "  Unless this was intentional (e.g. a bulk-load staging table), re-enable\n"
        "  autovacuum with:\n\n"
        + "\n".join(
            f"    ALTER TABLE {t.schema}.{t.table} RESET (autovacuum_enabled);"
            for t in disabled
        )
    )
    console.print()
    console.print(Panel(body, title="[bold red]Autovacuum Disabled[/bold red]", expand=False))


def show_table_health(report: AdvisorReport, top: Optional[int] = None) -> None:
    tables = report.tables[:top] if top else report.tables
    title  = "📊  Table Vacuum & Analyze Health"
    if top:
        title += f"  [dim](top {top} by dead rows)[/dim]"

    console.print()
    console.print(Panel(f"[bold cyan]{title}[/bold cyan]", expand=False))

    t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", padding=(0, 1))
    t.add_column("Schema.Table",    style="cyan", no_wrap=True, max_width=45)
    t.add_column("Size",            justify="right")
    t.add_column("Live Rows",       justify="right")
    t.add_column("Dead Rows",       justify="right")
    t.add_column("Dead %",          justify="right")
    t.add_column("Vac Trigger\n[dim](dead rows)[/dim]", justify="right")
    t.add_column("% to Vac",        justify="right")
    t.add_column("Mod Since\n[dim]Analyze[/dim]",      justify="right")
    t.add_column("% to Ana",        justify="right")
    t.add_column("Last Autovacuum", justify="right")
    t.add_column("Status",          justify="left")

    for th in tables:
        last_av = th.last_autovacuum
        if last_av:
            last_av_str = last_av.strftime("%Y-%m-%d %H:%M")
        elif th.n_live > 0:
            last_av_str = "[red]Never[/red]"
        else:
            last_av_str = "—"

        label = f"{th.schema}.{th.table}"
        if th.has_vacuum_override or th.has_analyze_override:
            label += " [dim]†[/dim]"

        dead_pct_str = (
            f"[bold red]{th.dead_pct:.1f}%[/bold red]"
            if th.dead_pct >= HIGH_DEAD_PCT
            else f"{th.dead_pct:.1f}%"
        )
        v_pct_str = (
            f"[bold yellow]{th.vacuum_pct:.0f}%[/bold yellow]"
            if th.vacuum_pct >= NEAR_TRIGGER_PCT
            else f"{th.vacuum_pct:.0f}%"
        )
        a_pct_str = (
            f"[bold yellow]{th.analyze_pct:.0f}%[/bold yellow]"
            if th.analyze_pct >= NEAR_TRIGGER_PCT
            else f"{th.analyze_pct:.0f}%"
        )

        t.add_row(
            label,
            fmt_bytes(th.size_bytes),
            fmt_num(th.n_live),
            fmt_num(th.n_dead),
            dead_pct_str,
            fmt_num(th.vacuum_trigger),
            v_pct_str,
            fmt_num(th.n_mod_since_analyze),
            a_pct_str,
            last_av_str,
            _status_rich(th.statuses),
        )

    console.print(t)
    console.print("  [dim]† Table has per-table autovacuum storage parameters set[/dim]")
    console.print(
        "  [dim]% to Vac / % to Ana  =  current dead/modified rows as % of the "
        "trigger threshold (≥80% → warning)[/dim]"
    )


def show_recommendations(report: AdvisorReport) -> None:
    recs = report.recommendations
    console.print()

    if not recs:
        console.print(Panel(
            "[bold green]✓  No per-table tuning needed — "
            "all large tables look well-configured.[/bold green]",
            expand=False,
        ))
        return

    console.print(Panel(
        f"[bold yellow]🔧  Per-Table Tuning Recommendations — {len(recs)} table(s)[/bold yellow]\n\n"
        "[dim]Large tables on the cloud default scale_factor of 0.2 accumulate excessive\n"
        "dead rows before autovacuum fires.  These ALTER TABLE statements lower the\n"
        "threshold so autovacuum keeps pace with your write rate.\n\n"
        "Scale factors are tiered by table size (cloud-tuned):\n"
        + "\n".join(f"  {label:<16} → vacuum scale_factor = {s}" for _, s, label in SCALE_TIERS)
        + "\n\nReview values for your workload before applying.[/dim]",
        expand=False,
    ))

    for rec in recs:
        fqtn = f"{rec.schema}.{rec.table}"
        console.print()
        console.print(
            f"  [bold cyan]{fqtn}[/bold cyan]  "
            f"[dim]{fmt_bytes(rec.size_bytes)} · "
            f"{fmt_num(rec.n_live)} live rows · tier: {rec.tier_label}[/dim]"
        )

        if rec.needs_vacuum:
            improvement = (
                rec.cur_vac_trigger / rec.new_vac_trigger
                if rec.new_vac_trigger > 0
                else 0
            )
            console.print(
                f"    [bold]Vacuum :[/bold]  "
                f"currently fires at [red]{fmt_num(rec.cur_vac_trigger)} dead rows[/red]  "
                f"[dim](scale={rec.cur_vac_scale}, threshold={int(rec.cur_vac_threshold)})[/dim]"
            )
            console.print(
                f"               proposed fires at [green]{fmt_num(rec.new_vac_trigger)} dead rows[/green]  "
                f"[dim](scale={rec.new_vac_scale}, threshold={rec.new_vac_threshold}"
                f" — {improvement:.0f}× more responsive)[/dim]"
            )

        if rec.needs_analyze:
            cur_a_trigger = calc_trigger(
                rec.n_live, rec.cur_ana_threshold, rec.cur_ana_scale
            )
            new_a_trigger = calc_trigger(
                rec.n_live, rec.new_ana_threshold, rec.new_ana_scale
            )
            console.print(
                f"    [bold]Analyze:[/bold]  "
                f"currently fires at [red]{fmt_num(cur_a_trigger)} modified rows[/red]  "
                f"[dim](scale={rec.cur_ana_scale}, threshold={int(rec.cur_ana_threshold)})[/dim]"
            )
            console.print(
                f"               proposed fires at [green]{fmt_num(new_a_trigger)} modified rows[/green]  "
                f"[dim](scale={rec.new_ana_scale}, threshold={rec.new_ana_threshold})[/dim]"
            )

        console.print()
        for line in build_alter_sql(rec).splitlines():
            console.print(f"    [bold green]{line}[/bold green]")

        if rec.needs_vacuum and rec.cur_vac_trigger > 0 and rec.new_vac_trigger > 0:
            factor = rec.cur_vac_trigger / rec.new_vac_trigger
            console.print(
                f"\n    [dim]💡 Vacuum will fire ~{factor:.0f}× more often.  If indexes were already\n"
                "       bloated before applying this change, consider running\n"
                "       REINDEX CONCURRENTLY on the table's high-traffic indexes.[/dim]"
            )


def show_summary(report: AdvisorReport) -> None:
    tables     = report.tables
    total      = len(tables)
    disabled   = sum(1 for t in tables if not t.autovacuum_enabled)
    high_bloat = sum(1 for t in tables if t.dead_pct >= HIGH_DEAD_PCT)
    never_av   = sum(1 for t in tables if not t.last_autovacuum and t.n_live > 0)
    tune_count = len(report.recommendations)

    def color(n: int, warn_color: str) -> str:
        return f"[{warn_color}]{n}[/{warn_color}]" if n else "[green]0[/green]"

    console.print()
    console.print(Panel(
        f"[bold]Summary[/bold]\n\n"
        f"  Tables analyzed        : {total}\n"
        f"  Autovacuum disabled    : {color(disabled, 'bold red')}\n"
        f"  High bloat (≥{HIGH_DEAD_PCT:.0f}% dead) : {color(high_bloat, 'bold red')}\n"
        f"  Never autovacuumed     : {color(never_av, 'bold red')}\n"
        f"  Need per-table tuning  : {color(tune_count, 'bold yellow')}",
        expand=False,
    ))


def render_console(report: AdvisorReport, top: Optional[int] = None) -> None:
    show_header(report)
    show_settings(report)
    show_xid_warnings(report)
    show_disabled_tables(report)
    show_table_health(report, top=top)
    show_recommendations(report)
    show_summary(report)


# ── Output Formatters ──────────────────────────────────────────────────────────
def _table_to_dict(t: TableHealth) -> Dict:
    return {
        "schema":               t.schema,
        "table":                t.table,
        "n_live":               t.n_live,
        "n_dead":               t.n_dead,
        "dead_pct":             t.dead_pct,
        "size_bytes":           t.size_bytes,
        "last_autovacuum":      t.last_autovacuum.isoformat() if t.last_autovacuum else None,
        "last_autoanalyze":     t.last_autoanalyze.isoformat() if t.last_autoanalyze else None,
        "n_mod_since_analyze":  t.n_mod_since_analyze,
        "vacuum_trigger":       t.vacuum_trigger,
        "vacuum_pct":           t.vacuum_pct,
        "analyze_trigger":      t.analyze_trigger,
        "analyze_pct":          t.analyze_pct,
        "has_vacuum_override":  t.has_vacuum_override,
        "has_analyze_override": t.has_analyze_override,
        "autovacuum_enabled":   t.autovacuum_enabled,
        "statuses":             t.statuses,
    }


def _rec_to_dict(r: Recommendation) -> Dict:
    return {
        "schema":              r.schema,
        "table":               r.table,
        "n_live":              r.n_live,
        "size_bytes":          r.size_bytes,
        "tier_label":          r.tier_label,
        "cur_vac_scale":       r.cur_vac_scale,
        "cur_vac_threshold":   r.cur_vac_threshold,
        "cur_vac_trigger":     r.cur_vac_trigger,
        "new_vac_scale":       r.new_vac_scale,
        "new_vac_threshold":   r.new_vac_threshold,
        "new_vac_trigger":     r.new_vac_trigger,
        "needs_vacuum":        r.needs_vacuum,
        "cur_ana_scale":       r.cur_ana_scale,
        "cur_ana_threshold":   r.cur_ana_threshold,
        "new_ana_scale":       r.new_ana_scale,
        "new_ana_threshold":   r.new_ana_threshold,
        "needs_analyze":       r.needs_analyze,
        "alter_table_sql":     build_alter_sql(r),
    }


def output_json(report: AdvisorReport, output_file: Optional[str]) -> None:
    data = {
        "generated_at":     report.generated_at,
        "pg_version":       report.pg_version,
        "platform":         report.platform,
        "platform_label":   report.platform_label,
        "platform_defaults": report.platform_defaults,
        "settings":         report.settings,
        "xid_data": [
            {k: str(v) for k, v in row.items()}
            for row in report.xid_rows
        ],
        "tables":          [_table_to_dict(t) for t in report.tables],
        "recommendations": [_rec_to_dict(r)   for r in report.recommendations],
        "summary": {
            "total_tables":       len(report.tables),
            "autovacuum_disabled":sum(1 for t in report.tables if not t.autovacuum_enabled),
            "high_bloat":         sum(1 for t in report.tables if t.dead_pct >= HIGH_DEAD_PCT),
            "never_autovacuumed": sum(1 for t in report.tables if not t.last_autovacuum and t.n_live > 0),
            "need_tuning":        len(report.recommendations),
        },
    }
    out = json.dumps(data, indent=2, default=str)
    if output_file:
        with open(output_file, "w") as fh:
            fh.write(out)
        console.print(f"[green]✓ JSON report written to {output_file}[/green]")
    else:
        print(out)


def output_csv(report: AdvisorReport, output_file: Optional[str]) -> None:
    rows = [_table_to_dict(t) for t in report.tables]
    if not rows:
        console.print("[yellow]No tables to export.[/yellow]")
        return
    # Flatten list fields for CSV compatibility
    for row in rows:
        row["statuses"] = "|".join(row["statuses"])  # type: ignore[arg-type]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    out = buf.getvalue()
    if output_file:
        with open(output_file, "w") as fh:
            fh.write(out)
        console.print(f"[green]✓ CSV report written to {output_file}[/green]")
    else:
        print(out)


# ── CLI ────────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "pg-vacuum-advisor — PostgreSQL Autovacuum Health Checker & Tuning Advisor\n"
            "Cloud-tuned for AWS RDS, Aurora PostgreSQL & Google Cloud SQL"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python vacuum_advisor.py --conn "postgresql://user:pass@host:5432/mydb" --platform rds
  python vacuum_advisor.py -H myhost -d mydb -U postgres --platform aurora
  python vacuum_advisor.py -H myhost -d mydb -U postgres --platform cloudsql
  python vacuum_advisor.py -H myhost -d mydb -U postgres --schema public
  python vacuum_advisor.py -H myhost -d mydb -U postgres --min-rows 500000
  python vacuum_advisor.py -H myhost -d mydb -U postgres --top 20
  python vacuum_advisor.py -H myhost -d mydb -U postgres --format json --output report.json
  python vacuum_advisor.py -H myhost -d mydb -U postgres --format csv  --output tables.csv

  # AWS RDS (scale_factor default is 0.1, analyze_scale_factor default is 0.05)
  python vacuum_advisor.py -H mydb.abc123.us-east-1.rds.amazonaws.com -d mydb -U postgres --platform rds

  # Aurora PostgreSQL (engine defaults: scale_factor=0.2, analyze_scale_factor=0.1)
  python vacuum_advisor.py -H cluster.cluster-xxx.us-east-1.rds.amazonaws.com -d mydb -U postgres --platform aurora

  # Google Cloud SQL (engine defaults: scale_factor=0.2, analyze_scale_factor=0.1)
  python vacuum_advisor.py -H 34.x.x.x -d mydb -U postgres --platform cloudsql
        """,
    )
    ap.add_argument(
        "--version", action="version",
        version=f"pg-vacuum-advisor {__version__}",
    )

    conn_grp = ap.add_mutually_exclusive_group(required=True)
    conn_grp.add_argument(
        "--conn", metavar="DSN",
        help="Full libpq DSN: postgresql://user:pass@host:5432/dbname",
    )
    conn_grp.add_argument("-H", "--host", dest="host", metavar="HOST")

    ap.add_argument("-p", "--port",     default="5432", metavar="PORT")
    ap.add_argument("-d", "--dbname",   metavar="DB",   help="Database name")
    ap.add_argument("-U", "--username", metavar="USER", help="Database user")
    ap.add_argument(
        "-W", "--password", action="store_true",
        help="Prompt for password interactively (preferred over embedding in DSN). "
             "Also accepts the PGPASSWORD environment variable.",
    )
    ap.add_argument(
        "--schema", metavar="SCHEMA",
        help="Restrict analysis to a single schema",
    )
    ap.add_argument(
        "--min-rows", metavar="N", type=int, default=0,
        help="Only report tables with at least N live rows",
    )
    ap.add_argument(
        "--top", metavar="N", type=int,
        help="Show only the top N tables by dead rows in the health table",
    )
    ap.add_argument(
        "--platform",
        choices=["rds", "aurora", "cloudsql"],
        default="rds",
        help=(
            "Cloud platform (default: rds). Controls which parameter group defaults "
            "are shown in the settings panel and used as the comparison baseline.\n"
            "  rds      – AWS RDS PostgreSQL       (vacuum_scale=0.1,  analyze_scale=0.05)\n"
            "  aurora   – Aurora PostgreSQL         (vacuum_scale=0.2,  analyze_scale=0.1)\n"
            "  cloudsql – Google Cloud SQL          (vacuum_scale=0.2,  analyze_scale=0.1)"
        ),
    )
    ap.add_argument(
        "--format", choices=["console", "json", "csv"], default="console",
        help="Output format (default: console)",
    )
    ap.add_argument(
        "--output", metavar="FILE",
        help="Write output to FILE instead of stdout (applies to json/csv formats)",
    )

    args = ap.parse_args()

    # ── Build connection string ────────────────────────────────────────────────
    if args.conn:
        conn_string = args.conn
    else:
        if not args.dbname:
            ap.error("--dbname / -d is required when using -H / --host")
        parts = [
            f"host={args.host}",
            f"port={args.port}",
            f"dbname={args.dbname}",
        ]
        if args.username:
            parts.append(f"user={args.username}")
        # Password resolution order: PGPASSWORD env → interactive prompt (-W)
        # Never accepted as a plain CLI arg to avoid leaking via process list.
        password = os.environ.get("PGPASSWORD", "")
        if not password and args.password:
            password = getpass.getpass("Password: ")
        if password:
            parts.append(f"password={password}")
        conn_string = " ".join(parts)

    # ── Set platform defaults (used by effective() fallback) ───────────────────
    global _platform_defaults
    _platform_defaults = PLATFORM_DEFAULTS.get(args.platform, _PG_ENGINE_DEFAULTS)

    # ── Fetch → Analyse → Output ───────────────────────────────────────────────
    gsettings, raw_rows, xid_rows, pg_version, current_db = fetch_data(
        conn_string, args.schema, args.min_rows
    )
    report = build_report(gsettings, raw_rows, xid_rows, pg_version, args.platform, current_db)

    if args.format == "json":
        output_json(report, args.output)
    elif args.format == "csv":
        output_csv(report, args.output)
    else:
        render_console(report, top=args.top)


if __name__ == "__main__":
    main()
