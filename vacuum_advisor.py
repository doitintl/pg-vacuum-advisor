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

Platform default scale_factors (verified via AWS API and AWS documentation):
  AWS RDS PostgreSQL  : vacuum_scale=0.1,  analyze_scale=0.05  (AWS parameter group override)
  Aurora PostgreSQL   : vacuum_scale=0.1,  analyze_scale=0.05  (same AWS override as RDS)
  Google Cloud SQL    : vacuum_scale=0.2,  analyze_scale=0.1   (stock PostgreSQL defaults)

With the AWS default scale_factor of 0.1, a 10M-row table still needs 1,000,050
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
import math
import os
import re
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
# Sources:
#   AWS RDS    : aws rds describe-db-parameters --db-parameter-group-name default.postgres<VER>
#   Aurora     : https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/
#                    AuroraPostgreSQL.Reference.ParameterGroups.html
#   Cloud SQL  : Google Cloud SQL documentation (engine defaults)
#
# Key finding: BOTH RDS and Aurora override the same two scale factors vs stock PostgreSQL:
#   autovacuum_vacuum_scale_factor : AWS (RDS & Aurora)=0.1   (PG stock=0.2, CloudSQL=0.2)
#   autovacuum_analyze_scale_factor: AWS (RDS & Aurora)=0.05  (PG stock=0.1, CloudSQL=0.1)
# Cloud SQL uses stock PostgreSQL engine defaults.
#
# Note: autovacuum_vacuum_cost_delay changed from 20 ms → 2 ms in PG 13.
# Note: maintenance_work_mem on RDS/Aurora is instance-size-dependent:
#       GREATEST({DBInstanceClassMemory/63963136*1024}, 65536) — shown as live value.

# Stock PostgreSQL engine defaults (used by Google Cloud SQL)
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

# AWS overrides — applied to both RDS and Aurora parameter groups
_AWS_OVERRIDES: Dict[str, str] = {
    "autovacuum_vacuum_scale_factor":  "0.1",   # half of PG stock default
    "autovacuum_analyze_scale_factor": "0.05",  # half of PG stock default
}

PLATFORM_DEFAULTS: Dict[str, Dict[str, str]] = {
    "rds":      {**_PG_ENGINE_DEFAULTS, **_AWS_OVERRIDES},
    "aurora":   {**_PG_ENGINE_DEFAULTS, **_AWS_OVERRIDES},  # same AWS overrides as RDS
    "cloudsql": {**_PG_ENGINE_DEFAULTS},                    # stock PG engine defaults
}

PLATFORM_LABELS: Dict[str, str] = {
    "rds":      "AWS RDS PostgreSQL",
    "aurora":   "Aurora PostgreSQL",
    "cloudsql": "Google Cloud SQL",
}

# Platform-internal admin databases that --all-databases should skip by default.
# Unlike template0/template1 (datistemplate=true, already excluded by
# SQL_LIST_DATABASES), these are ordinary, connectable, non-template databases
# the cloud provider creates for its own management use — nothing a customer
# runs vacuum tuning against. --exclude-db can still add more on top of these.
PLATFORM_INTERNAL_DATABASES: Dict[str, List[str]] = {
    "rds":      ["rdsadmin"],
    "aurora":   ["rdsadmin"],
    "cloudsql": ["cloudsqladmin"],
}

# Convenience alias used throughout — set in main() based on --platform flag
_platform_defaults: Dict[str, str] = _PG_ENGINE_DEFAULTS  # overwritten at startup

# ── Thresholds ─────────────────────────────────────────────────────────────────
HIGH_DEAD_PCT          = 20.0         # Dead-tuple % considered high bloat
# Percentage-based bloat (HIGH_DEAD_PCT) misses large tables whose dead-tuple
# percentage is modest but whose absolute dead-row count / dead-byte volume is
# still what actually drives I/O and disk usage (e.g. an 844 GB table at 10.76%
# dead — 34M dead tuples — sits under the 20% threshold yet dwarfs 130 flagged
# tables under it combined).  These two absolute thresholds catch that case
# without touching the existing percentage flag, so both dimensions are
# reported side by side.
HIGH_DEAD_ROWS_ABS     = 1_000_000          # ≥ 1 M dead rows, regardless of %
HIGH_DEAD_BYTES_ABS     = 1 * 1024 ** 3     # ≥ 1 GB estimated dead bytes, regardless of %
NEAR_TRIGGER_PCT       = 80.0         # % of trigger threshold = "near trigger" warning
# Tables below this size are omitted from the health display — autovacuum handles
# small tables well by default (trigger fires after ~5-10% of rows, not millions).
# Exception: tables with autovacuum explicitly disabled are always shown.
HEALTH_MIN_BYTES       = 50 * 1024 * 1024   # 50 MB
# Remaining transactions until autovacuum_freeze_max_age (the SOFT limit) is hit.
# freeze_max_age is NOT the wraparound point — the hard limit is 2^31 (~2.1 B).
# Once xid_age exceeds freeze_max_age, PostgreSQL's anti-wraparound autovacuum
# is already firing aggressively on affected tables.  These thresholds warn that
# the soft window is nearly exhausted, which means autovacuum may be lagging.
XID_WARNING_REMAINING  = 50_000_000   # < 50 M remaining until freeze_max_age → warn
XID_CRITICAL_REMAINING = 10_000_000   # < 10 M remaining until freeze_max_age → critical

# Tiered recommended vacuum scale_factor by live row count (cloud-tuned).
# Each entry: (min_live_rows, recommended_scale_factor, tier_label)
SCALE_TIERS: List[Tuple[int, float, str]] = [
    (500_000_000, 0.0005, "> 500 M rows"),
    (100_000_000, 0.001,  "> 100 M rows"),
    ( 10_000_000, 0.005,  ">  10 M rows"),
    (  1_000_000, 0.01,   ">   1 M rows"),
]
RECOMMENDED_THRESHOLD = 1_000  # vacuum/analyze threshold for large tables

# ── Checkpoint & WAL Health Thresholds ──────────────────────────────────────────
# checkpoints_req_pct at/above this bar means a majority of checkpoints are
# being driven by WAL fill (checkpoints_req) rather than the checkpoint_timeout
# timer (checkpoints_timed) — a strong signal that max_wal_size/checkpoint_timeout
# are undersized for the current write rate.  Fixed constant, no CLI override,
# consistent with how HIGH_DEAD_PCT and the other thresholds in this tool work.
CHECKPOINT_REQ_PCT_THRESHOLD = 50.0

# Tiered checkpoint_timeout recommendation, keyed by how far past
# CHECKPOINT_REQ_PCT_THRESHOLD the observed checkpoints_req_pct is — mirrors the
# SCALE_TIERS pattern used for vacuum scale_factor recommendations.  Evaluated
# highest-bar-first; the first tier whose minimum the observed pct meets or
# exceeds wins.
CHECKPOINT_TIMEOUT_TIERS: List[Tuple[float, int, str]] = [
    (90.0, 1800, "≥ 90% requested"),
    (70.0, 1200, "≥ 70% requested"),
    (50.0, 900,  "≥ 50% requested"),
]

# postgresqlco.nf / jberkus annotated.conf rule: below ~1 GB/hour of sustained
# WAL generation, PostgreSQL's stock max_wal_size default is fine; above that,
# size max_wal_size to cover at least one hour of WAL at the current rate.
MAX_WAL_SIZE_HEADROOM_HOURS = 1

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

# pg_stat_user_tables / pg_class are database-scoped catalogs — a connection to
# one database cannot see another database's tables.  --all-databases works
# around that by connecting first to a bootstrap database (default: postgres)
# just to enumerate the real, connectable databases, then reconnecting once
# per database to run SQL_TABLES against each in turn.
SQL_LIST_DATABASES = """
    SELECT datname
    FROM   pg_database
    WHERE  datistemplate = false
    AND    datallowconn  = true
    ORDER BY datname;
"""

# ── Checkpoint & WAL Health SQL ──────────────────────────────────────────────
# Version-gated: PostgreSQL 17 split checkpointer-specific counters out of
# pg_stat_bgwriter into pg_stat_checkpointer. buffers_backend was removed from
# pg_stat_bgwriter back in PG 16 (folded into pg_stat_io, which this tool does
# not query — see CHECKPOINT_HEALTH_PLAN.md §4.2/§9 item 3).  These views are
# instance-wide (not per-database), same as SQL_XID above.

# PG >= 17 — pg_stat_checkpointer has the checkpoint-specific counters
SQL_CHECKPOINTER_PG17 = """
    SELECT
        num_timed           AS checkpoints_timed,
        num_requested        AS checkpoints_req,
        buffers_written      AS buffers_checkpoint,
        write_time           AS checkpoint_write_time_ms,
        sync_time            AS checkpoint_sync_time_ms,
        stats_reset
    FROM pg_stat_checkpointer;
"""

# PG >= 17 — pg_stat_bgwriter still carries the background-writer counters
SQL_BGWRITER_BUFFERS_CLEAN_ONLY = """
    SELECT buffers_clean
    FROM pg_stat_bgwriter;
"""

# PG < 16 — pg_stat_bgwriter has everything, including buffers_backend
SQL_BGWRITER_PRE16 = """
    SELECT
        checkpoints_timed,
        checkpoints_req,
        buffers_checkpoint,
        buffers_clean,
        buffers_backend,
        stats_reset
    FROM pg_stat_bgwriter;
"""

# PG 16 — same view, but buffers_backend has already been removed
SQL_BGWRITER_PG16 = """
    SELECT
        checkpoints_timed,
        checkpoints_req,
        buffers_checkpoint,
        buffers_clean,
        stats_reset
    FROM pg_stat_bgwriter;
"""

SQL_BLOCK_SIZE = """
    SELECT setting::int AS block_size
    FROM   pg_settings
    WHERE  name = 'block_size';
"""

# pg_stat_wal doesn't exist before PG 14
SQL_WAL = """
    SELECT wal_bytes, stats_reset AS wal_stats_reset
    FROM pg_stat_wal;
"""

SQL_CHECKPOINT_SETTINGS = """
    SELECT name, setting
    FROM   pg_settings
    WHERE  name IN (
        'checkpoint_timeout',
        'max_wal_size',
        'checkpoint_completion_target'
    );
"""

# ── Data Classes ───────────────────────────────────────────────────────────────
@dataclass
class TableHealth:
    schema:               str
    table:                str
    n_live:               int
    n_dead:               int
    dead_pct:             float
    size_bytes:           int
    estimated_dead_bytes: int   # size_bytes * dead_pct — absolute-bloat estimate
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
class CheckpointRecommendation:
    needs_tuning:                     bool
    reason:                           str    # short human-readable trigger explanation
    recommended_checkpoint_timeout_s: int
    recommended_max_wal_size_mb:      int
    alter_system_sql:                 List[str] = field(default_factory=list)


@dataclass
class CheckpointHealth:
    pg_stat_source:          str     # "pg_stat_bgwriter" | "pg_stat_checkpointer" — for debuggability

    # Raw counters (post version-normalization)
    checkpoints_timed:       int
    checkpoints_req:         int
    buffers_checkpoint:      int
    buffers_clean:           int
    buffers_backend:         Optional[int]   # None on PG >= 16 — removed from pg_stat_bgwriter
    block_size:              int
    stats_reset:             Optional[datetime]
    checkpoint_write_time_ms: Optional[int]  # PG >= 17 only (pg_stat_checkpointer.write_time)
    checkpoint_sync_time_ms:  Optional[int]  # PG >= 17 only

    # WAL volume since stats_reset (None if pg_stat_wal unavailable, PG < 14)
    wal_bytes:               Optional[int]
    wal_stats_reset:         Optional[datetime]

    # Derived (computed in the analyze step, not the fetch step)
    checkpoints_total:       int
    checkpoints_req_pct:     float
    avg_checkpoint_write_bytes: int
    total_written_bytes:     int
    checkpoint_write_pct:    float
    backend_write_pct:       Optional[float]   # None if buffers_backend unavailable
    background_write_pct:    float
    stats_window_seconds:    Optional[float]
    wal_bytes_per_hour:      Optional[float]
    avg_minutes_between_checkpoints: Optional[float]

    # Current live settings (from pg_settings)
    checkpoint_timeout_s:         int
    max_wal_size_mb:              int
    checkpoint_completion_target: float

    statuses:       List[str] = field(default_factory=list)
    recommendation: Optional[CheckpointRecommendation] = None


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
    # Optional so replaying old JSON reports that predate this feature doesn't
    # break — same backward-compat pattern used for estimated_dead_bytes.
    checkpoint_health: Optional["CheckpointHealth"] = None


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


def _pg_major_version(version_string: str) -> int:
    """Parse the major version number out of a `SELECT version()` string.

    e.g. "PostgreSQL 17.4 on x86_64-pc-linux-gnu, compiled by ..." -> 17
         "PostgreSQL 9.6.24 on ..." -> 9  (pre-2018 two-part versioning)

    Deliberately a regex, not a naive split — the string has commas and
    parenthetical build info after the version number.  Pure function, no DB
    connection needed, so it's unit-testable on its own.
    """
    match = re.search(r"PostgreSQL\s+(\d+)", version_string)
    if not match:
        raise ValueError(
            f"Could not parse PostgreSQL major version from: {version_string!r}"
        )
    return int(match.group(1))


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


def quote_ident(identifier: str) -> str:
    """Quote a PostgreSQL identifier per quote_ident() semantics.

    Mirrors what the server-side quote_ident() function guarantees: the
    identifier is always safe to use verbatim in generated SQL, regardless
    of case, reserved-word status, or embedded characters.  We always wrap
    in double quotes (unconditional quoting is always valid PostgreSQL
    syntax — it just disables case-folding) and escape any embedded double
    quote by doubling it, exactly as the server does.  This is deliberately
    NOT a naive `f'"{identifier}"'` wrap: that form breaks the moment the
    identifier itself contains a `"` character (e.g. a table literally
    named `foo"bar`), producing invalid/injectable SQL.

    Without this, unquoted mixed-case or reserved-word identifiers (very
    common with ORMs like EF Core or Hibernate, e.g. `CollectedEntitiesMetadata`)
    get folded to lowercase by PostgreSQL and the generated statement fails
    with `relation "..." does not exist`.
    """
    return '"' + identifier.replace('"', '""') + '"'


def qualify_ident(schema: str, table: str) -> str:
    """Build a fully-qualified, properly quoted `schema.table` identifier."""
    return f"{quote_ident(schema)}.{quote_ident(table)}"


def build_alter_sql(rec: Recommendation) -> str:
    """Generate the ALTER TABLE statement for a recommendation."""
    fqtn   = qualify_ident(rec.schema, rec.table)
    params = []
    if rec.needs_vacuum:
        params.append(f"    autovacuum_vacuum_scale_factor  = {rec.new_vac_scale}")
        params.append(f"    autovacuum_vacuum_threshold     = {rec.new_vac_threshold}")
    if rec.needs_analyze:
        params.append(f"    autovacuum_analyze_scale_factor = {rec.new_ana_scale}")
        params.append(f"    autovacuum_analyze_threshold    = {rec.new_ana_threshold}")
    return f"ALTER TABLE {fqtn} SET (\n" + ",\n".join(params) + "\n);"


class DatabaseFetchError(Exception):
    """Raised by fetch_data() on any connection/query failure.

    fetch_data() itself never calls sys.exit() — that decision belongs to the
    caller. A single-database run exits(1) immediately on this (same
    behavior as before this was factored out). --all-databases instead
    catches it per database, prints a warning, and continues with the rest —
    one unreachable database (revoked CONNECT, auth mismatch, etc.) shouldn't
    abort analysis of every other database on the instance.
    """


# ── Data Fetching ──────────────────────────────────────────────────────────────
def fetch_data(
    conn_string: str,
    schema: Optional[str],
    min_rows: int,
    fetch_checkpoint: bool = True,
) -> Tuple[Dict[str, str], List[Dict], List[Dict], str, str, Optional[Dict]]:
    """Open a read-only connection, run all queries, return raw data.

    Uses psycopg2.sql composition for all user-supplied values to prevent
    SQL injection.

    fetch_checkpoint: set False to skip the checkpoint/WAL health queries —
    used by --all-databases, which fetches checkpoint health once per
    instance (it's cluster-wide, not per-database) and reuses it across every
    database's report rather than re-querying it for each one.

    Returns: (gsettings, table_rows, xid_rows, pg_version, current_db, checkpoint_raw)
    checkpoint_raw is None if fetch_checkpoint=False, or if the checkpoint/WAL
    queries themselves fail (e.g. permissions, very old PG) — callers should
    treat that as "checkpoint health unavailable", not a fatal error for the
    rest of the report.
    Raises: DatabaseFetchError on any connection or query failure (other than
    the checkpoint/WAL queries, which degrade gracefully to None instead).
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

        # Checkpoint/WAL health — instance-wide, cluster-level, not per-database.
        # Degrades to None on failure (permissions, very old PG) rather than
        # failing the whole fetch; the rest of the report is unaffected.
        checkpoint_raw: Optional[Dict] = None
        if fetch_checkpoint:
            try:
                checkpoint_raw = fetch_checkpoint_health(cur, pg_version)
            except (psycopg2.Error, ValueError, KeyError):
                checkpoint_raw = None

        cur.close()
        conn.close()

    except psycopg2.OperationalError as e:
        raise DatabaseFetchError(f"Could not connect: {e}") from e
    except psycopg2.Error as e:
        raise DatabaseFetchError(f"Database error: {e}") from e

    return gsettings, rows, xid_rows, pg_version, current_db, checkpoint_raw


def fetch_checkpoint_health(cur, pg_version: str) -> Dict:
    """Fetch raw checkpoint/WAL counters using the already-open cursor.

    Instance-wide, not per-database (same pattern as SQL_XID) — callers that
    loop over multiple databases on one instance (--all-databases) should
    call this once and reuse the result rather than once per database.

    Returns a plain dict of raw values; build_checkpoint_health() does the
    (pure, unit-testable) derived math separately.
    """
    major = _pg_major_version(pg_version)
    raw: Dict = {"pg_major": major}

    if major >= 17:
        cur.execute(SQL_CHECKPOINTER_PG17)
        cp = cur.fetchone()
        cur.execute(SQL_BGWRITER_BUFFERS_CLEAN_ONLY)
        bg = cur.fetchone()
        raw["pg_stat_source"] = "pg_stat_checkpointer"
        raw["checkpoints_timed"]        = cp["checkpoints_timed"]
        raw["checkpoints_req"]          = cp["checkpoints_req"]
        raw["buffers_checkpoint"]       = cp["buffers_checkpoint"]
        raw["checkpoint_write_time_ms"] = cp["checkpoint_write_time_ms"]
        raw["checkpoint_sync_time_ms"]  = cp["checkpoint_sync_time_ms"]
        raw["stats_reset"]              = cp["stats_reset"]
        raw["buffers_clean"]            = bg["buffers_clean"]
        raw["buffers_backend"]          = None  # folded into pg_stat_io since PG 16 — not queried (v1)
    else:
        query = SQL_BGWRITER_PRE16 if major < 16 else SQL_BGWRITER_PG16
        cur.execute(query)
        bg = cur.fetchone()
        raw["pg_stat_source"]           = "pg_stat_bgwriter"
        raw["checkpoints_timed"]        = bg["checkpoints_timed"]
        raw["checkpoints_req"]          = bg["checkpoints_req"]
        raw["buffers_checkpoint"]       = bg["buffers_checkpoint"]
        raw["buffers_clean"]            = bg["buffers_clean"]
        raw["buffers_backend"]          = bg["buffers_backend"] if major < 16 else None
        raw["stats_reset"]              = bg["stats_reset"]
        raw["checkpoint_write_time_ms"] = None
        raw["checkpoint_sync_time_ms"]  = None

    cur.execute(SQL_BLOCK_SIZE)
    raw["block_size"] = cur.fetchone()["block_size"]

    if major >= 14:
        cur.execute(SQL_WAL)
        wal = cur.fetchone()
        raw["wal_bytes"]       = wal["wal_bytes"]
        raw["wal_stats_reset"] = wal["wal_stats_reset"]
    else:
        raw["wal_bytes"]       = None
        raw["wal_stats_reset"] = None

    cur.execute(SQL_CHECKPOINT_SETTINGS)
    cp_settings = {r["name"]: r["setting"] for r in cur.fetchall()}
    raw["checkpoint_timeout_s"]         = int(cp_settings.get("checkpoint_timeout", 300))
    raw["max_wal_size_mb"]              = int(cp_settings.get("max_wal_size", 1024))
    raw["checkpoint_completion_target"] = float(cp_settings.get("checkpoint_completion_target", 0.9))

    return raw


def list_target_databases(bootstrap_conn_string: str, exclude: Optional[set] = None) -> List[str]:
    """Enumerate real, connectable databases on the instance for --all-databases.

    Connects once to a bootstrap database (typically 'postgres') purely to
    read pg_database, which — unlike pg_stat_user_tables — is cluster-wide.
    Template databases and anything in `exclude` (e.g. 'rdsadmin') are skipped.
    """
    exclude = exclude or set()
    try:
        conn = psycopg2.connect(bootstrap_conn_string)
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
        cur.execute(SQL_LIST_DATABASES)
        names = [r[0] for r in cur.fetchall()]
        cur.close()
        conn.close()
    except psycopg2.OperationalError as e:
        console.print(f"\n[bold red]Could not connect to bootstrap database:[/bold red] {e}")
        sys.exit(1)
    except psycopg2.Error as e:
        console.print(f"\n[bold red]Database error while listing databases:[/bold red] {e}")
        sys.exit(1)

    return [n for n in names if n not in exclude]


# ── Analysis ───────────────────────────────────────────────────────────────────
def analyze_table_row(row: Dict, gsettings: Dict[str, str]) -> TableHealth:
    """Compute full health metrics for a single table row."""
    relopts = parse_reloptions(row["reloptions"])
    n_live  = int(row["n_live_tup"]          or 0)
    n_dead  = int(row["n_dead_tup"]          or 0)
    n_mod   = int(row["n_mod_since_analyze"] or 0)
    dead_pct = float(row["dead_pct"]         or 0)
    size_bytes = int(row["total_size_bytes"] or 0)
    # Estimate: assume dead tuples occupy the same average row density as the
    # table as a whole, so dead_pct of total_size_bytes approximates dead bytes.
    # This is an approximation (real per-row size varies, TOAST/index bytes are
    # included in total_size_bytes) but is good enough to rank absolute impact.
    estimated_dead_bytes = int(size_bytes * dead_pct / 100.0)

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
    # Absolute dimension — independent of the percentage flag above, so a
    # large table with modest dead_pct but huge dead-row/dead-byte volume
    # still gets surfaced (see HIGH_DEAD_ROWS_ABS / HIGH_DEAD_BYTES_ABS comment).
    if n_dead >= HIGH_DEAD_ROWS_ABS or estimated_dead_bytes >= HIGH_DEAD_BYTES_ABS:
        statuses.append("HIGH_BLOAT_ABSOLUTE")
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
        size_bytes=size_bytes,
        estimated_dead_bytes=estimated_dead_bytes,
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


def recommended_checkpoint_timeout_s(checkpoints_req_pct: float) -> Tuple[int, str]:
    """Tiered checkpoint_timeout recommendation, keyed by how far past
    CHECKPOINT_REQ_PCT_THRESHOLD the observed checkpoints_req_pct is.
    Returns (recommended_seconds, tier_label).
    """
    for min_pct, timeout_s, label in CHECKPOINT_TIMEOUT_TIERS:
        if checkpoints_req_pct >= min_pct:
            return timeout_s, label
    # Below every tier — shouldn't be reached when needs_tuning is True, since
    # the lowest tier matches CHECKPOINT_REQ_PCT_THRESHOLD, but fall back safely.
    return CHECKPOINT_TIMEOUT_TIERS[-1][1], CHECKPOINT_TIMEOUT_TIERS[-1][2]


def recommend_checkpoint_tuning(ch: "CheckpointHealth") -> CheckpointRecommendation:
    """Direct implementation of the formula worked out in
    CHECKPOINT_HEALTH_PLAN.md §5:

    A requested checkpoint fires when WAL written since the last checkpoint
    approaches max_wal_size — roughly max_wal_size / (1 + checkpoint_completion_target).
    At the current WAL rate, compute how long that takes to fill; if it's less
    than checkpoint_timeout, checkpoints are WAL-triggered, not time-triggered.
    """
    needs_tuning = ch.checkpoints_total > 0 and ch.checkpoints_req_pct >= CHECKPOINT_REQ_PCT_THRESHOLD

    if not needs_tuning:
        return CheckpointRecommendation(
            needs_tuning=False,
            reason="",
            recommended_checkpoint_timeout_s=ch.checkpoint_timeout_s,
            recommended_max_wal_size_mb=ch.max_wal_size_mb,
            alter_system_sql=[],
        )

    effective_trigger_bytes = (
        ch.max_wal_size_mb * 1024 * 1024 / (1 + ch.checkpoint_completion_target)
    )

    minutes_to_fill: Optional[float] = None
    if ch.wal_bytes_per_hour:
        bytes_per_minute = ch.wal_bytes_per_hour / 60.0
        if bytes_per_minute > 0:
            minutes_to_fill = effective_trigger_bytes / bytes_per_minute

    recommended_timeout_s, _tier_label = recommended_checkpoint_timeout_s(ch.checkpoints_req_pct)

    # Size max_wal_size per the postgresqlco.nf / jberkus annotated.conf rule:
    # at least MAX_WAL_SIZE_HEADROOM_HOURS of WAL at the current generation
    # rate, floored at whatever max_wal_size is already set to (never
    # recommend shrinking it).  No pg_stat_wal (PG < 14) → nothing to size
    # against, leave as-is.
    one_hour_of_wal_mb = (
        (ch.wal_bytes_per_hour * MAX_WAL_SIZE_HEADROOM_HOURS) / (1024 * 1024)
        if ch.wal_bytes_per_hour else None
    )
    recommended_max_wal_size_mb = (
        max(ch.max_wal_size_mb, math.ceil(one_hour_of_wal_mb))
        if one_hour_of_wal_mb is not None
        else ch.max_wal_size_mb
    )

    if minutes_to_fill is not None:
        reason = (
            f"{ch.checkpoints_req_pct:.0f}% of checkpoints are WAL-triggered "
            f"(checkpoints_req), not timer-triggered — at the current WAL rate "
            f"the {fmt_bytes(effective_trigger_bytes)} trigger point fills in "
            f"~{minutes_to_fill:.1f} min, well under checkpoint_timeout "
            f"({ch.checkpoint_timeout_s}s)."
        )
    else:
        reason = (
            f"{ch.checkpoints_req_pct:.0f}% of checkpoints are WAL-triggered "
            f"(checkpoints_req) rather than timer-triggered."
        )

    # Always recommended together — raising max_wal_size without raising
    # checkpoint_timeout just delays the same problem.
    alter_system_sql = [
        f"ALTER SYSTEM SET checkpoint_timeout = '{recommended_timeout_s}s';",
        f"ALTER SYSTEM SET max_wal_size = '{recommended_max_wal_size_mb}MB';",
    ]

    return CheckpointRecommendation(
        needs_tuning=True,
        reason=reason,
        recommended_checkpoint_timeout_s=recommended_timeout_s,
        recommended_max_wal_size_mb=recommended_max_wal_size_mb,
        alter_system_sql=alter_system_sql,
    )


def build_checkpoint_health(raw: Dict, now: Optional[datetime] = None) -> CheckpointHealth:
    """Pure function: compute derived percentages/rates from the raw dict
    fetch_checkpoint_health() returns, then attach a recommendation.
    Unit-testable with synthetic input dicts, same pattern as
    analyze_table_row().
    """
    now = now or datetime.now(timezone.utc)

    checkpoints_timed = int(raw.get("checkpoints_timed") or 0)
    checkpoints_req    = int(raw.get("checkpoints_req")   or 0)
    checkpoints_total  = checkpoints_timed + checkpoints_req
    checkpoints_req_pct = (
        round(100.0 * checkpoints_req / checkpoints_total, 1)
        if checkpoints_total > 0 else 0.0
    )

    buffers_checkpoint = int(raw.get("buffers_checkpoint") or 0)
    buffers_clean       = int(raw.get("buffers_clean")      or 0)
    _bb = raw.get("buffers_backend")
    buffers_backend: Optional[int] = int(_bb) if _bb is not None else None
    block_size = int(raw.get("block_size") or 8192)

    avg_checkpoint_write_bytes = (
        int(buffers_checkpoint * block_size / checkpoints_total)
        if checkpoints_total > 0 else 0
    )
    total_written_bytes = block_size * (
        buffers_checkpoint + buffers_clean + (buffers_backend or 0)
    )
    checkpoint_write_pct = (
        round(100.0 * buffers_checkpoint * block_size / total_written_bytes, 1)
        if total_written_bytes > 0 else 0.0
    )
    backend_write_pct: Optional[float] = (
        round(100.0 * buffers_backend * block_size / total_written_bytes, 1)
        if (buffers_backend is not None and total_written_bytes > 0) else None
    )
    background_write_pct = (
        round(100.0 * buffers_clean * block_size / total_written_bytes, 1)
        if total_written_bytes > 0 else 0.0
    )

    stats_reset = raw.get("stats_reset")
    stats_window_seconds: Optional[float] = None
    if stats_reset:
        delta = (now - stats_reset).total_seconds()
        stats_window_seconds = delta if delta > 0 else None

    avg_minutes_between_checkpoints: Optional[float] = None
    if stats_window_seconds and checkpoints_total > 0:
        avg_minutes_between_checkpoints = round(
            stats_window_seconds / 60.0 / checkpoints_total, 2
        )

    wal_bytes = raw.get("wal_bytes")
    wal_bytes_per_hour: Optional[float] = None
    if wal_bytes is not None and stats_window_seconds:
        wal_bytes_per_hour = float(wal_bytes) / (stats_window_seconds / 3600.0)

    statuses: List[str] = []
    if checkpoints_total > 0 and checkpoints_req_pct >= CHECKPOINT_REQ_PCT_THRESHOLD:
        statuses.append("CHECKPOINT_PRESSURE")
    if not statuses:
        statuses.append("OK")

    ch = CheckpointHealth(
        pg_stat_source=raw.get("pg_stat_source", "pg_stat_bgwriter"),
        checkpoints_timed=checkpoints_timed,
        checkpoints_req=checkpoints_req,
        buffers_checkpoint=buffers_checkpoint,
        buffers_clean=buffers_clean,
        buffers_backend=buffers_backend,
        block_size=block_size,
        stats_reset=stats_reset,
        checkpoint_write_time_ms=raw.get("checkpoint_write_time_ms"),
        checkpoint_sync_time_ms=raw.get("checkpoint_sync_time_ms"),
        wal_bytes=int(wal_bytes) if wal_bytes is not None else None,
        wal_stats_reset=raw.get("wal_stats_reset"),
        checkpoints_total=checkpoints_total,
        checkpoints_req_pct=checkpoints_req_pct,
        avg_checkpoint_write_bytes=avg_checkpoint_write_bytes,
        total_written_bytes=total_written_bytes,
        checkpoint_write_pct=checkpoint_write_pct,
        backend_write_pct=backend_write_pct,
        background_write_pct=background_write_pct,
        stats_window_seconds=stats_window_seconds,
        wal_bytes_per_hour=wal_bytes_per_hour,
        avg_minutes_between_checkpoints=avg_minutes_between_checkpoints,
        checkpoint_timeout_s=int(raw.get("checkpoint_timeout_s", 300)),
        max_wal_size_mb=int(raw.get("max_wal_size_mb", 1024)),
        checkpoint_completion_target=float(raw.get("checkpoint_completion_target", 0.9)),
        statuses=statuses,
    )
    ch.recommendation = recommend_checkpoint_tuning(ch)
    return ch


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
    checkpoint_health: Optional[CheckpointHealth] = None,
) -> AdvisorReport:
    """checkpoint_health is already-built (via build_checkpoint_health()), not
    raw — this lets --all-databases build it once per instance and pass the
    same object into every database's report instead of recomputing it.
    """
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
        checkpoint_health=checkpoint_health,
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
    alerts = [
        (xid, int(xid["freeze_max_age"]) - int(xid["xid_age"]))
        for xid in report.xid_rows
        if int(xid["freeze_max_age"]) - int(xid["xid_age"]) < XID_WARNING_REMAINING
    ]
    if not alerts:
        return

    # Print the context note once — not repeated per database
    console.print()
    console.print(Panel(
        "[dim]PostgreSQL must freeze old transaction IDs to prevent wraparound failure.\n"
        "autovacuum_freeze_max_age is a [bold]soft limit[/bold] — once a database's XID age\n"
        "crosses it, anti-wraparound autovacuum runs aggressively to catch up.\n"
        "The closer to this limit, the more expensive VACUUM becomes: it must freeze\n"
        "more rows, takes longer, and holds a SHARE UPDATE EXCLUSIVE lock that can\n"
        "block DDL statements and degrade overall performance.\n"
        "The [bold]hard limit[/bold] (actual wraparound failure) is 2^31 (~2.1 billion transactions).[/dim]",
        title="[bold yellow]Transaction ID Wraparound — Background[/bold yellow]",
        expand=False,
    ))

    for xid, remaining in alerts:
        tag = " [dim](current database)[/dim]" if xid["datname"] == report.current_db else ""
        pct_used = int(xid["xid_age"]) / int(xid["freeze_max_age"]) * 100

        if remaining < XID_CRITICAL_REMAINING:
            console.print()
            console.print(Panel(
                f"[bold red]🚨 Anti-Wraparound Autovacuum Is Behind Schedule[/bold red]\n\n"
                f"  Database       : {xid['datname']}{tag}\n"
                f"  XID age        : {fmt_num(xid['xid_age'])} ({pct_used:.1f}% of soft limit)\n"
                f"  Freeze max age : {fmt_num(xid['freeze_max_age'])}\n"
                f"  Remaining      : [bold red]{fmt_num(remaining)} transactions[/bold red] until soft limit\n\n"
                "  ► Confirm anti-wraparound autovacuum is actively running on this database.\n"
                "  ► Check pg_stat_activity for autovacuum workers on high-write tables.\n"
                "  ► If autovacuum is disabled on any table, re-enable it immediately.\n"
                "  ► AWS RDS   : check Enhanced Monitoring → autovacuum worker activity.\n"
                "  ► Cloud SQL : check System Insights → 'PostgreSQL autovacuum' metric.",
                title="[bold red]XID Wraparound Warning — CRITICAL[/bold red]",
                expand=False,
            ))
        else:
            console.print()
            console.print(Panel(
                f"[bold yellow]⚠  XID Age Approaching Soft Freeze Limit[/bold yellow]\n\n"
                f"  Database       : {xid['datname']}{tag}\n"
                f"  XID age        : {fmt_num(xid['xid_age'])} ({pct_used:.1f}% of soft limit)\n"
                f"  Freeze max age : {fmt_num(xid['freeze_max_age'])}\n"
                f"  Remaining      : [yellow]{fmt_num(remaining)} transactions[/yellow] until soft limit\n\n"
                "  ► Monitor that autovacuum is keeping up on high-write tables.\n"
                "  ► AWS RDS   : confirm autovacuum_freeze_max_age in your parameter group.\n"
                "  ► Cloud SQL : use pg_stat_user_tables.n_dead_tup to track progress.",
                title="[yellow]XID Wraparound Warning[/yellow]",
                expand=False,
            ))


def _status_rich(statuses: List[str]) -> str:
    """Convert a list of status flags to a Rich-formatted display string."""
    parts: List[str] = []
    if "DISABLED"             in statuses: parts.append("[bold red]🚫 DISABLED[/bold red]")
    if "HIGH_BLOAT"           in statuses: parts.append("[bold red]⚠ HIGH BLOAT[/bold red]")
    if "HIGH_BLOAT_ABSOLUTE"  in statuses: parts.append("[bold red]⚠ HIGH BLOAT (ABS)[/bold red]")
    if "NEAR_VACUUM_TRIGGER"  in statuses: parts.append("[bold yellow]⚡ NEAR VAC[/bold yellow]")
    if "NEAR_ANALYZE_TRIGGER" in statuses: parts.append("[bold yellow]📈 NEAR ANA[/bold yellow]")
    if statuses == ["OK"]:                 parts.append("[green]✓ OK[/green]")
    return " ".join(parts)


def show_checkpoint_health(report: AdvisorReport) -> None:
    """Checkpoint & WAL Health panel — placement in render_console() is after
    show_xid_warnings(), before show_disabled_tables(), since checkpoint/WAL
    pressure is architecturally closer to "instance health" than to "table
    health."
    """
    ch = report.checkpoint_health
    if ch is None:
        return

    reset_str = ch.stats_reset.strftime("%Y-%m-%d") if ch.stats_reset else "unknown"
    checkpoints_line = (
        f"  Checkpoints (since {reset_str})  : {fmt_num(ch.checkpoints_total)} "
        f"({fmt_num(ch.checkpoints_timed)} timed / {fmt_num(ch.checkpoints_req)} req)"
    )
    interval_line = (
        f"  Avg time between checkpoints    : {ch.avg_minutes_between_checkpoints:.1f} min"
        if ch.avg_minutes_between_checkpoints is not None
        else "  Avg time between checkpoints    : n/a"
    )
    if ch.wal_bytes is not None:
        wal_rate = f" (~{fmt_bytes(ch.wal_bytes_per_hour)}/hour avg)" if ch.wal_bytes_per_hour else ""
        wal_line = f"  WAL generated                   : {fmt_bytes(ch.wal_bytes)}{wal_rate}"
    else:
        wal_line = "  WAL generated                   : n/a (pg_stat_wal unavailable — PG < 14)"

    backend_line = (
        f"  Backend writes (unbuffered)     : {ch.backend_write_pct:.1f}% of all buffer writes"
        if ch.backend_write_pct is not None
        else "  Backend writes (unbuffered)     : unavailable on PG ≥ 16 "
             "(removed from pg_stat_bgwriter, folded into pg_stat_io — not queried in v1)"
    )

    pressure = "CHECKPOINT_PRESSURE" in ch.statuses
    header = (
        f"[bold red]⚠ {ch.checkpoints_req_pct:.1f}% of checkpoints are requested "
        "(WAL-triggered), not timed[/bold red]"
        if pressure else
        f"[bold green]✓ {ch.checkpoints_req_pct:.1f}% of checkpoints are requested "
        "— within normal range[/bold green]"
    )

    body_lines = [
        header,
        "",
        checkpoints_line,
        interval_line,
        wal_line,
        backend_line,
        f"  checkpoint_timeout (current)    : {ch.checkpoint_timeout_s}s",
        f"  max_wal_size (current)          : {ch.max_wal_size_mb} MB",
        f"  checkpoint_completion_target    : {ch.checkpoint_completion_target}",
    ]

    rec = ch.recommendation
    if rec and rec.needs_tuning:
        body_lines += [
            "",
            f"  [dim]{rec.reason}[/dim]",
            "",
            "  [bold yellow]Recommended (raise together):[/bold yellow]",
            f"    checkpoint_timeout = {rec.recommended_checkpoint_timeout_s}s",
            f"    max_wal_size       = {rec.recommended_max_wal_size_mb} MB   "
            "[dim](≥ 1 hour of WAL at current rate — sized from the pg_stat_wal\n"
            "                                    average over the stats window)[/dim]",
            "",
        ]
        for line in rec.alter_system_sql:
            body_lines.append(f"  [bold green]{line}[/bold green]")
        body_lines += [
            "",
            "  [dim]Caveat: more WAL between checkpoints means longer crash/failover\n"
            "  recovery — a conscious durability trade, reversible, no reboot\n"
            "  needed (both are dynamic GUCs).[/dim]",
        ]
    elif pressure:
        body_lines += [
            "",
            "  [dim]Below the tuning threshold, but keep an eye on this if write volume grows.[/dim]",
        ]

    console.print()
    console.print(Panel(
        "\n".join(body_lines),
        title="[bold cyan]Checkpoint & WAL Health[/bold cyan]",
        expand=False,
    ))


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
            f"    ALTER TABLE {qualify_ident(t.schema, t.table)} RESET (autovacuum_enabled);"
            for t in disabled
        )
    )
    console.print()
    console.print(Panel(body, title="[bold red]Autovacuum Disabled[/bold red]", expand=False))


def show_table_health(report: AdvisorReport, top: Optional[int] = None) -> None:
    # Exclude temp tables — autovacuum doesn't run on them; they have their own section
    non_temp = [t for t in report.tables if not t.schema.startswith("pg_temp_")]

    # Omit small tables from display — autovacuum handles them well with default settings.
    # Always keep tables with autovacuum disabled regardless of size (wraparound risk).
    tables    = [t for t in non_temp if t.size_bytes >= HEALTH_MIN_BYTES or not t.autovacuum_enabled]
    omitted   = len(non_temp) - len(tables)

    tables = tables[:top] if top else tables

    title = "📊  Table Vacuum & Analyze Health"
    if top:
        title += f"  [dim](top {top} by dead rows)[/dim]"

    console.print()
    console.print(Panel(f"[bold cyan]{title}[/bold cyan]", expand=False))

    t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", padding=(0, 1))
    t.add_column("Schema.Table",      style="cyan", no_wrap=True, max_width=45)
    t.add_column("Size",              justify="right")
    t.add_column("Live Rows",         justify="right")
    t.add_column("Dead Rows",         justify="right")
    t.add_column("Dead %",            justify="right")
    t.add_column("Vac Trigger\n[dim](dead rows)[/dim]", justify="right")
    t.add_column("% to Vac",          justify="right")
    t.add_column("% to Ana",          justify="right")
    t.add_column("Last Autovacuum",   justify="right")
    t.add_column("Last Autoanalyze",  justify="right")
    t.add_column("Status",            justify="left")

    def fmt_date(dt: Optional[datetime], has_rows: bool) -> str:
        if dt:
            return dt.strftime("%Y-%m-%d")
        return "[red]Never[/red]" if has_rows else "—"

    for th in tables:
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
            a_pct_str,
            fmt_date(th.last_autovacuum,  th.n_live > 0),
            fmt_date(th.last_autoanalyze, th.n_live > 0),
            _status_rich(th.statuses),
        )

    console.print(t)
    console.print("  [dim]† Table has per-table autovacuum storage parameters set[/dim]")
    console.print(
        "  [dim]% to Vac / % to Ana  =  current dead/modified rows as % of the "
        "trigger threshold (≥80% → warning)[/dim]"
    )
    if omitted:
        console.print(
            f"  [dim]{omitted} table(s) < 50 MB omitted — autovacuum handles small tables "
            "well with default settings.  Use --min-rows to filter at collection time.[/dim]"
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
    high_bloat_abs = sum(1 for t in tables if "HIGH_BLOAT_ABSOLUTE" in t.statuses)
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
        f"  High bloat (absolute)  : {color(high_bloat_abs, 'bold red')}"
        f"  [dim](≥{fmt_num(HIGH_DEAD_ROWS_ABS)} dead rows or ≥{fmt_bytes(HIGH_DEAD_BYTES_ABS)} est. dead bytes)[/dim]\n"
        f"  Never autovacuumed     : {color(never_av, 'bold red')}\n"
        f"  Need per-table tuning  : {color(tune_count, 'bold yellow')}",
        expand=False,
    ))


def render_console(report: AdvisorReport, top: Optional[int] = None, show_checkpoint: bool = True) -> None:
    show_header(report)
    show_settings(report)
    show_xid_warnings(report)
    if show_checkpoint:
        show_checkpoint_health(report)
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
        "estimated_dead_bytes": t.estimated_dead_bytes,
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


def _checkpoint_health_to_dict(ch: Optional[CheckpointHealth]) -> Optional[Dict]:
    """checkpoint_health is null when the underlying stats couldn't be read at
    all (permissions issue, extremely old PG) rather than omitted — keeps the
    schema predictable for consumers (json_to_report.py etc.) instead of
    requiring a .get() check everywhere.
    """
    if ch is None:
        return None
    rec = ch.recommendation
    return {
        "pg_stat_source":             ch.pg_stat_source,
        "checkpoints_timed":          ch.checkpoints_timed,
        "checkpoints_req":            ch.checkpoints_req,
        "checkpoints_total":          ch.checkpoints_total,
        "checkpoints_req_pct":        ch.checkpoints_req_pct,
        "buffers_checkpoint":         ch.buffers_checkpoint,
        "buffers_clean":              ch.buffers_clean,
        "buffers_backend":            ch.buffers_backend,
        "block_size":                 ch.block_size,
        "avg_checkpoint_write_bytes": ch.avg_checkpoint_write_bytes,
        "total_written_bytes":        ch.total_written_bytes,
        "checkpoint_write_pct":       ch.checkpoint_write_pct,
        "backend_write_pct":          ch.backend_write_pct,
        "background_write_pct":       ch.background_write_pct,
        "stats_reset":                ch.stats_reset.isoformat() if ch.stats_reset else None,
        "stats_window_seconds":       ch.stats_window_seconds,
        "wal_bytes":                  ch.wal_bytes,
        "wal_stats_reset":            ch.wal_stats_reset.isoformat() if ch.wal_stats_reset else None,
        "wal_bytes_per_hour":         ch.wal_bytes_per_hour,
        "avg_minutes_between_checkpoints": ch.avg_minutes_between_checkpoints,
        "checkpoint_timeout_s":       ch.checkpoint_timeout_s,
        "max_wal_size_mb":            ch.max_wal_size_mb,
        "checkpoint_completion_target": ch.checkpoint_completion_target,
        "checkpoint_write_time_ms":   ch.checkpoint_write_time_ms,
        "checkpoint_sync_time_ms":    ch.checkpoint_sync_time_ms,
        "statuses":                   ch.statuses,
        "recommendation": {
            "needs_tuning":                      rec.needs_tuning,
            "reason":                             rec.reason,
            "recommended_checkpoint_timeout_s":   rec.recommended_checkpoint_timeout_s,
            "recommended_max_wal_size_mb":        rec.recommended_max_wal_size_mb,
            "alter_system_sql":                   rec.alter_system_sql,
        } if rec else None,
    }


def _checkpoint_health_from_dict(data: Optional[Dict]) -> Optional[CheckpointHealth]:
    """Reconstruct CheckpointHealth from a report dict's "checkpoint_health"
    key. Returns None if the key is missing/null — older JSON reports that
    predate this feature (or a live run where the stats were unavailable)
    still replay fine; the console section is simply skipped.
    """
    if not data:
        return None

    def _parse_dt(val):
        if not val:
            return None
        try:
            return datetime.fromisoformat(val)
        except (ValueError, TypeError):
            return None

    rec_data = data.get("recommendation")
    rec = CheckpointRecommendation(
        needs_tuning=rec_data["needs_tuning"],
        reason=rec_data["reason"],
        recommended_checkpoint_timeout_s=rec_data["recommended_checkpoint_timeout_s"],
        recommended_max_wal_size_mb=rec_data["recommended_max_wal_size_mb"],
        alter_system_sql=rec_data.get("alter_system_sql", []),
    ) if rec_data else None

    return CheckpointHealth(
        pg_stat_source=data.get("pg_stat_source", "pg_stat_bgwriter"),
        checkpoints_timed=data["checkpoints_timed"],
        checkpoints_req=data["checkpoints_req"],
        buffers_checkpoint=data["buffers_checkpoint"],
        buffers_clean=data["buffers_clean"],
        buffers_backend=data.get("buffers_backend"),
        block_size=data["block_size"],
        stats_reset=_parse_dt(data.get("stats_reset")),
        checkpoint_write_time_ms=data.get("checkpoint_write_time_ms"),
        checkpoint_sync_time_ms=data.get("checkpoint_sync_time_ms"),
        wal_bytes=data.get("wal_bytes"),
        wal_stats_reset=_parse_dt(data.get("wal_stats_reset")),
        checkpoints_total=data["checkpoints_total"],
        checkpoints_req_pct=data["checkpoints_req_pct"],
        avg_checkpoint_write_bytes=data["avg_checkpoint_write_bytes"],
        total_written_bytes=data["total_written_bytes"],
        checkpoint_write_pct=data["checkpoint_write_pct"],
        backend_write_pct=data.get("backend_write_pct"),
        background_write_pct=data["background_write_pct"],
        stats_window_seconds=data.get("stats_window_seconds"),
        wal_bytes_per_hour=data.get("wal_bytes_per_hour"),
        avg_minutes_between_checkpoints=data.get("avg_minutes_between_checkpoints"),
        checkpoint_timeout_s=data["checkpoint_timeout_s"],
        max_wal_size_mb=data["max_wal_size_mb"],
        checkpoint_completion_target=data["checkpoint_completion_target"],
        statuses=data.get("statuses", []),
        recommendation=rec,
    )


def report_to_dict(report: AdvisorReport) -> Dict:
    """Convert an AdvisorReport to the plain-dict shape used by --format json.

    Factored out of output_json() so --all-databases can reuse the exact same
    per-database shape inside its merged {instance, database, report} list —
    each entry's "report" is byte-for-byte what a single-database run would
    have produced on its own.
    """
    return {
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
        "checkpoint_health": _checkpoint_health_to_dict(report.checkpoint_health),
        "summary": {
            "total_tables":       len(report.tables),
            "autovacuum_disabled":sum(1 for t in report.tables if not t.autovacuum_enabled),
            "high_bloat":         sum(1 for t in report.tables if t.dead_pct >= HIGH_DEAD_PCT),
            "high_bloat_absolute": sum(1 for t in report.tables if "HIGH_BLOAT_ABSOLUTE" in t.statuses),
            "never_autovacuumed": sum(1 for t in report.tables if not t.last_autovacuum and t.n_live > 0),
            "need_tuning":        len(report.recommendations),
        },
    }


def output_json(report: AdvisorReport, output_file: Optional[str]) -> None:
    data = report_to_dict(report)
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


# ── Multi-database output (--all-databases) ────────────────────────────────────
def output_merged_json(
    merged: List[Tuple[str, str, AdvisorReport]],
    output_file: Optional[str],
) -> None:
    """Write the {instance, database, report} list produced by --all-databases.

    This is the exact shape support teams have already been hand-assembling
    by running the tool once per database and concatenating the JSON output
    themselves — --all-databases now produces it directly.
    """
    data = [
        {"instance": instance, "database": dbname, "report": report_to_dict(report)}
        for instance, dbname, report in merged
    ]
    out = json.dumps(data, indent=2, default=str)
    if output_file:
        with open(output_file, "w") as fh:
            fh.write(out)
        console.print(f"[green]✓ Merged JSON report ({len(merged)} database(s)) written to {output_file}[/green]")
    else:
        print(out)


def output_merged_csv(
    merged: List[Tuple[str, str, AdvisorReport]],
    output_file: Optional[str],
) -> None:
    """Flatten every database's table rows into one CSV, tagged by instance/database."""
    rows: List[Dict] = []
    for instance, dbname, report in merged:
        for t in report.tables:
            row = {"instance": instance, "database": dbname, **_table_to_dict(t)}
            row["statuses"] = "|".join(row["statuses"])  # type: ignore[arg-type]
            rows.append(row)

    if not rows:
        console.print("[yellow]No tables to export across any database.[/yellow]")
        return

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    out = buf.getvalue()
    if output_file:
        with open(output_file, "w") as fh:
            fh.write(out)
        console.print(f"[green]✓ Merged CSV report ({len(merged)} database(s)) written to {output_file}[/green]")
    else:
        print(out)


# ── Replay from JSON ──────────────────────────────────────────────────────────
def _parse_json_file(path: str):
    """Shared file-read/parse error handling for --replay, single- or multi-db."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        console.print(f"[bold red]File not found:[/bold red] {path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        console.print(f"[bold red]Invalid JSON:[/bold red] {e}")
        sys.exit(1)


def report_from_dict(data: Dict) -> AdvisorReport:
    """Reconstruct an AdvisorReport from a single-database report dict —
    i.e. one element of the shape --format json produces, or one entry's
    "report" field inside a --all-databases merged list.
    """
    tables: List[TableHealth] = []
    for t in data["tables"]:
        last_av = last_ana = None
        if t.get("last_autovacuum") and t["last_autovacuum"] not in (None, "Never"):
            try:
                last_av = datetime.fromisoformat(t["last_autovacuum"])
            except (ValueError, TypeError):
                pass
        if t.get("last_autoanalyze") and t["last_autoanalyze"] not in (None, "Never"):
            try:
                last_ana = datetime.fromisoformat(t["last_autoanalyze"])
            except (ValueError, TypeError):
                pass
        tables.append(TableHealth(
            schema=t["schema"],
            table=t["table"],
            n_live=t["n_live"],
            n_dead=t["n_dead"],
            dead_pct=t["dead_pct"],
            size_bytes=t["size_bytes"],
            # Backward-compatible: older JSON reports predate this field.
            estimated_dead_bytes=t.get(
                "estimated_dead_bytes",
                int(t["size_bytes"] * t["dead_pct"] / 100.0),
            ),
            last_autovacuum=last_av,
            last_autoanalyze=last_ana,
            n_mod_since_analyze=t["n_mod_since_analyze"],
            vacuum_trigger=t["vacuum_trigger"],
            vacuum_pct=t["vacuum_pct"],
            analyze_trigger=t["analyze_trigger"],
            analyze_pct=t["analyze_pct"],
            has_vacuum_override=t["has_vacuum_override"],
            has_analyze_override=t["has_analyze_override"],
            autovacuum_enabled=t["autovacuum_enabled"],
            vac_scale=0.0,      # not stored in JSON; unused in display
            vac_threshold=0.0,
            ana_scale=0.0,
            ana_threshold=0.0,
            statuses=t["statuses"],
        ))

    recs: List[Recommendation] = []
    for r in data.get("recommendations", []):
        recs.append(Recommendation(
            schema=r["schema"],
            table=r["table"],
            n_live=r["n_live"],
            size_bytes=r["size_bytes"],
            tier_label=r["tier_label"],
            cur_vac_scale=r["cur_vac_scale"],
            cur_vac_threshold=r["cur_vac_threshold"],
            cur_vac_trigger=r["cur_vac_trigger"],
            new_vac_scale=r["new_vac_scale"],
            new_vac_threshold=r["new_vac_threshold"],
            new_vac_trigger=r["new_vac_trigger"],
            needs_vacuum=r["needs_vacuum"],
            cur_ana_scale=r["cur_ana_scale"],
            cur_ana_threshold=r["cur_ana_threshold"],
            new_ana_scale=r["new_ana_scale"],
            new_ana_threshold=r["new_ana_threshold"],
            needs_analyze=r["needs_analyze"],
        ))

    return AdvisorReport(
        pg_version=data["pg_version"],
        platform=data["platform"],
        platform_label=data["platform_label"],
        platform_defaults=data["platform_defaults"],
        settings=data["settings"],
        tables=tables,
        recommendations=recs,
        xid_rows=data["xid_data"],
        generated_at=data["generated_at"],
        current_db="",  # not stored in JSON; "(current database)" tag will be omitted
        # .get() default: older JSON reports that predate this feature still replay.
        checkpoint_health=_checkpoint_health_from_dict(data.get("checkpoint_health")),
    )


def load_report_from_json(path: str) -> AdvisorReport:
    """Reconstruct an AdvisorReport from a single-database JSON file
    produced by plain `--format json` (not `--all-databases`).
    """
    data = _parse_json_file(path)
    if isinstance(data, list):
        console.print(
            "[bold red]This looks like a multi-database report[/bold red] "
            "(a JSON list, produced by --all-databases) — pass it to --replay "
            "directly; it's detected automatically and doesn't need this "
            "single-database loader."
        )
        sys.exit(1)
    return report_from_dict(data)


def _merged_reports_from_data(data) -> List[Tuple[str, str, AdvisorReport]]:
    """Convert already-parsed JSON data (a list) into (instance, database,
    AdvisorReport) tuples. Shared by load_merged_reports_from_json() and the
    --replay auto-detection path in main(), which has already parsed the file
    once to check whether it's a list or a single object.
    """
    if not isinstance(data, list):
        console.print(
            "[bold red]Expected a multi-database JSON list[/bold red] "
            "(instance/database/report entries) but got a single-database "
            "report object instead."
        )
        sys.exit(1)

    merged: List[Tuple[str, str, AdvisorReport]] = []
    for entry in data:
        try:
            instance  = entry["instance"]
            dbname    = entry["database"]
            report    = report_from_dict(entry["report"])
        except (KeyError, TypeError) as e:
            console.print(f"[bold red]Malformed entry in merged JSON file:[/bold red] {e}")
            sys.exit(1)
        report.current_db = dbname
        merged.append((instance, dbname, report))
    return merged


def load_merged_reports_from_json(path: str) -> List[Tuple[str, str, AdvisorReport]]:
    """Reconstruct the {instance, database, report} list produced by
    `--all-databases --format json`.
    """
    return _merged_reports_from_data(_parse_json_file(path))


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

  # Re-render console output from a JSON file (no database connection needed)
  python vacuum_advisor.py --replay report.json

  # Analyze every database on the instance in one run (pg_stat_user_tables is
  # database-scoped, so a normal run only ever sees one database)
  python vacuum_advisor.py -H myhost -U postgres --all-databases --platform rds \\
      --format json --output all_dbs_report.json
  # rdsadmin/template0/template1 are skipped automatically; --exclude-db adds more
  python vacuum_advisor.py -H myhost -U postgres --all-databases --exclude-db staging_db

  # AWS RDS (scale_factor default is 0.1, analyze_scale_factor default is 0.05)
  python vacuum_advisor.py -H mydb.abc123.us-east-1.rds.amazonaws.com -d mydb -U postgres --platform rds

  # Aurora PostgreSQL (same AWS defaults as RDS: scale_factor=0.1, analyze_scale_factor=0.05)
  python vacuum_advisor.py -H cluster.cluster-xxx.us-east-1.rds.amazonaws.com -d mydb -U postgres --platform aurora

  # Google Cloud SQL (engine defaults: scale_factor=0.2, analyze_scale_factor=0.1)
  python vacuum_advisor.py -H 34.x.x.x -d mydb -U postgres --platform cloudsql
        """,
    )
    ap.add_argument(
        "--version", action="version",
        version=f"pg-vacuum-advisor {__version__}",
    )

    ap.add_argument(
        "--replay", metavar="JSON_FILE",
        help="Re-render console output from a JSON report file (no database connection needed)",
    )

    conn_grp = ap.add_mutually_exclusive_group(required=False)
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
        "--all-databases", action="store_true",
        help=(
            "Analyze every connectable database on the instance, not just one. "
            "pg_stat_user_tables and pg_class are database-scoped, so a normal "
            "run only ever sees the one database it connected to — this "
            "enumerates every database via pg_database, runs the analysis "
            "against each in turn, and merges the results into a list of "
            "{instance, database, report} entries (report has the same shape "
            "as a normal single-database report). Requires -H/--host, not "
            "--conn, since a per-database connection string is built for each "
            "database found. XID wraparound data is already cluster-wide and "
            "will be identical across every entry. template0/template1 are "
            "always skipped; the platform's own admin database (rdsadmin for "
            "rds/aurora, cloudsqladmin for cloudsql) is skipped automatically "
            "based on --platform — use --exclude-db to skip additional ones."
        ),
    )
    ap.add_argument(
        "--bootstrap-db", metavar="DB", default="postgres",
        help=(
            "Database used only to enumerate the other databases when "
            "--all-databases is set (default: postgres). Not analyzed itself "
            "unless it also shows up in the enumerated list and isn't excluded."
        ),
    )
    ap.add_argument(
        "--exclude-db", metavar="DB1,DB2,...", default="",
        help=(
            "Additional comma-separated database names to skip with "
            "--all-databases, on top of the automatic exclusions "
            "(template0/template1, and the platform's admin database)"
        ),
    )
    ap.add_argument(
        "--instance-label", metavar="NAME",
        help=(
            "Label recorded as \"instance\" in --all-databases output "
            "(default: the -H/--host value). Useful for tagging results when "
            "merging output from several instances afterward."
        ),
    )
    ap.add_argument(
        "--platform",
        choices=["rds", "aurora", "cloudsql"],
        default="rds",
        help=(
            "Cloud platform (default: rds). Controls which parameter group defaults "
            "are shown in the settings panel and used as the comparison baseline.\n"
            "  rds      – AWS RDS PostgreSQL       (vacuum_scale=0.1,  analyze_scale=0.05)\n"
            "  aurora   – Aurora PostgreSQL         (vacuum_scale=0.1,  analyze_scale=0.05)\n"
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
    global _platform_defaults

    # ── Replay mode: re-render console output from a JSON file ────────────────
    # Auto-detects shape: a plain object is a single-database report; a list
    # is the {instance, database, report} shape --all-databases produces.
    if args.replay:
        raw = _parse_json_file(args.replay)
        if isinstance(raw, list):
            merged = _merged_reports_from_data(raw)
            # Checkpoint health is identical across every entry in a merged
            # --all-databases report (it's instance-wide) — render it once,
            # before the per-database loop, rather than once per database.
            shown_checkpoint = False
            for instance, dbname, report in merged:
                _platform_defaults = PLATFORM_DEFAULTS.get(report.platform, _PG_ENGINE_DEFAULTS)
                console.print()
                console.print(Panel(
                    f"[bold cyan]Instance: {instance}   Database: {dbname}[/bold cyan]",
                    expand=False,
                ))
                render_console(report, top=args.top, show_checkpoint=not shown_checkpoint)
                if report.checkpoint_health is not None:
                    shown_checkpoint = True
        else:
            report = report_from_dict(raw)
            _platform_defaults = PLATFORM_DEFAULTS.get(report.platform, _PG_ENGINE_DEFAULTS)
            render_console(report, top=args.top)
        return

    # ── Require connection info when not in replay mode ───────────────────────
    if not args.conn and not args.host:
        ap.error("one of --conn / -H is required (or use --replay to render from a JSON file)")

    if args.all_databases and args.conn:
        ap.error(
            "--all-databases requires -H/--host, not --conn — it needs to build a "
            "separate connection string per database, which isn't possible from a "
            "single pre-built DSN"
        )

    # ── Set platform defaults (used by effective() fallback) ───────────────────
    _platform_defaults = PLATFORM_DEFAULTS.get(args.platform, _PG_ENGINE_DEFAULTS)

    # ── --all-databases: loop over every connectable database on the instance ──
    if args.all_databases:
        # Password resolution order: PGPASSWORD env → interactive prompt (-W).
        # Resolved once up front so we don't prompt once per database.
        password = os.environ.get("PGPASSWORD", "")
        if not password and args.password:
            password = getpass.getpass("Password: ")

        def conn_string_for(dbname: str) -> str:
            kwargs = {"host": args.host, "port": args.port, "dbname": dbname}
            if args.username:
                kwargs["user"] = args.username
            if password:
                kwargs["password"] = password
            return psycopg2.extensions.make_dsn(**kwargs)

        exclude = {d.strip() for d in args.exclude_db.split(",") if d.strip()}
        exclude |= set(PLATFORM_INTERNAL_DATABASES.get(args.platform, []))
        instance_label = args.instance_label or args.host

        databases = list_target_databases(conn_string_for(args.bootstrap_db), exclude)
        if not databases:
            console.print("[yellow]No connectable databases found (after exclusions).[/yellow]")
            return

        merged: List[Tuple[str, str, AdvisorReport]] = []
        skipped: List[Tuple[str, str]] = []  # (dbname, error message)
        # Checkpoint/WAL health is instance-wide, not per-database (same as
        # xid_data) — fetched once against the first database that succeeds,
        # then the same built CheckpointHealth object is reused for every
        # other database's report rather than re-querying it each time.
        cached_checkpoint_health: Optional[CheckpointHealth] = None
        checkpoint_fetch_attempted = False
        first_console_checkpoint_shown = False
        for dbname in databases:
            want_checkpoint = not checkpoint_fetch_attempted
            try:
                gsettings, raw_rows, xid_rows, pg_version, current_db, checkpoint_raw = fetch_data(
                    conn_string_for(dbname), args.schema, args.min_rows,
                    fetch_checkpoint=want_checkpoint,
                )
            except DatabaseFetchError as e:
                # One unreachable database (revoked CONNECT, auth mismatch,
                # etc.) shouldn't abort analysis of every other database on
                # the instance — warn, skip it, and keep going.
                console.print(f"[yellow]⚠ Skipping database '{dbname}': {e}[/yellow]")
                skipped.append((dbname, str(e)))
                continue

            if want_checkpoint:
                checkpoint_fetch_attempted = True
                if checkpoint_raw:
                    cached_checkpoint_health = build_checkpoint_health(checkpoint_raw)

            report = build_report(
                gsettings, raw_rows, xid_rows, pg_version, args.platform, current_db,
                checkpoint_health=cached_checkpoint_health,
            )
            merged.append((instance_label, dbname, report))

            if args.format == "console":
                console.print()
                console.print(Panel(
                    f"[bold cyan]Database: {dbname}[/bold cyan]",
                    title="[bold cyan]═══════════════════════════════════[/bold cyan]",
                    expand=False,
                ))
                # Checkpoint health is identical across every database in this
                # merge — show the panel once, not once per database.
                render_console(
                    report, top=args.top,
                    show_checkpoint=not first_console_checkpoint_shown,
                )
                if cached_checkpoint_health is not None:
                    first_console_checkpoint_shown = True

        if args.format == "json":
            output_merged_json(merged, args.output)
        elif args.format == "csv":
            output_merged_csv(merged, args.output)
        # console format already rendered per-database above

        if skipped:
            console.print()
            console.print(Panel(
                f"[bold yellow]⚠ Skipped {len(skipped)} of {len(databases)} database(s) "
                "due to connection/query errors[/bold yellow]\n\n"
                + "\n".join(f"  • {dbname}: {msg}" for dbname, msg in skipped),
                title="[bold yellow]Skipped Databases[/bold yellow]",
                expand=False,
            ))
        if not merged:
            console.print("[bold red]No database could be analyzed — every connection attempt failed.[/bold red]")
            sys.exit(1)
        return

    # ── Build connection string ────────────────────────────────────────────────
    if args.conn:
        conn_string = args.conn
    else:
        if not args.dbname:
            ap.error("--dbname / -d is required when using -H / --host")
        kwargs = {"host": args.host, "port": args.port, "dbname": args.dbname}
        if args.username:
            kwargs["user"] = args.username
        # Password resolution order: PGPASSWORD env → interactive prompt (-W)
        # Never accepted as a plain CLI arg to avoid leaking via process list.
        password = os.environ.get("PGPASSWORD", "")
        if not password and args.password:
            password = getpass.getpass("Password: ")
        if password:
            kwargs["password"] = password
        conn_string = psycopg2.extensions.make_dsn(**kwargs)

    # ── Fetch → Analyse → Output ───────────────────────────────────────────────
    try:
        gsettings, raw_rows, xid_rows, pg_version, current_db, checkpoint_raw = fetch_data(
            conn_string, args.schema, args.min_rows
        )
    except DatabaseFetchError as e:
        console.print(f"\n[bold red]{e}[/bold red]")
        sys.exit(1)
    checkpoint_health = build_checkpoint_health(checkpoint_raw) if checkpoint_raw else None
    report = build_report(
        gsettings, raw_rows, xid_rows, pg_version, args.platform, current_db,
        checkpoint_health=checkpoint_health,
    )

    if args.format == "json":
        output_json(report, args.output)
    elif args.format == "csv":
        output_csv(report, args.output)
    else:
        render_console(report, top=args.top)


if __name__ == "__main__":
    main()
