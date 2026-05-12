#!/usr/bin/env python3
"""
pg-vacuum-advisor
-----------------
PostgreSQL Autovacuum Health Checker & Tuning Advisor

Connects to a PostgreSQL database, analyzes vacuum health across all user
tables, and generates ready-to-run ALTER TABLE recommendations for tables
that need per-table vacuum tuning.

Autovacuum fires on a table when:
    dead_rows > vacuum_threshold + (vacuum_scale_factor x live_rows)

With the default scale_factor of 0.2, a 10M-row table needs 2,000,050 dead
rows before autovacuum fires.  This tool shows you that math for every table
and tells you exactly what to change.

Usage:
    python vacuum_advisor.py --conn "postgresql://user:pass@host:5432/mydb"
    python vacuum_advisor.py -H localhost -d mydb -U postgres
    python vacuum_advisor.py -H localhost -d mydb -U postgres --schema public
    python vacuum_advisor.py -H localhost -d mydb -U postgres --min-rows 100000

Author : Aamir Haroon  (github.com/aamir814)
License: MIT
"""

import argparse
import sys
from typing import Dict, List, Optional

try:
    import psycopg2
    import psycopg2.extras
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

console = Console()

# ── Thresholds ────────────────────────────────────────────────────────────────
LARGE_TABLE_ROWS         = 1_000_000   # Tables above this get per-table recommendations
HIGH_DEAD_PCT            = 20.0        # Dead-tuple % considered high bloat
NEAR_TRIGGER_PCT         = 80.0        # % of trigger threshold = "near trigger" warning
RECOMMENDED_SCALE_LARGE  = 0.01        # Recommended scale_factor for large tables
RECOMMENDED_THRESHOLD    = 1_000       # Recommended vacuum_threshold for large tables
XID_WARNING_REMAINING    = 500_000_000 # Warn at 500 M transactions remaining
XID_CRITICAL_REMAINING   = 200_000_000 # Critical at 200 M transactions remaining

# ── SQL ───────────────────────────────────────────────────────────────────────
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

SQL_TABLES = """
    SELECT
        s.schemaname,
        s.relname                                                   AS tablename,
        s.n_live_tup,
        s.n_dead_tup,
        s.last_autovacuum,
        s.last_vacuum,
        s.autovacuum_count,
        s.n_mod_since_analyze,
        CASE
            WHEN s.n_live_tup + s.n_dead_tup > 0
            THEN ROUND(100.0 * s.n_dead_tup / (s.n_live_tup + s.n_dead_tup), 2)
            ELSE 0
        END                                                         AS dead_pct,
        pg_total_relation_size(s.relid)                            AS total_size_bytes,
        c.reloptions
    FROM  pg_stat_user_tables s
    JOIN  pg_class c ON c.oid = s.relid
    WHERE s.schemaname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
    {schema_filter}
    {rows_filter}
    ORDER BY s.n_dead_tup DESC, s.n_live_tup DESC;
"""

SQL_XID = """
    SELECT
        datname,
        age(datfrozenxid)                                          AS xid_age,
        current_setting('autovacuum_freeze_max_age')::bigint       AS freeze_max_age
    FROM  pg_database
    WHERE datname = current_database();
"""

# ── Helpers ───────────────────────────────────────────────────────────────────
def fmt_bytes(n: int) -> str:
    """Format bytes to a human-readable string."""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_num(n) -> str:
    """Format a number with thousands separators."""
    return f"{int(n or 0):,}"


def parse_reloptions(reloptions) -> Dict[str, str]:
    """Parse pg_class.reloptions list into a plain dict."""
    if not reloptions:
        return {}
    return {k: v for k, _, v in (opt.partition("=") for opt in reloptions)}


def effective(param: str, relopts: Dict, gsettings: Dict):
    """Return (value: float, is_table_override: bool) for a vacuum parameter."""
    if param in relopts:
        return float(relopts[param]), True
    return float(gsettings.get(param, 0)), False


def vacuum_trigger(n_live: int, threshold: float, scale: float) -> int:
    """Calculate the dead-row count that will trigger autovacuum."""
    return int(threshold + scale * n_live)


# ── Display ───────────────────────────────────────────────────────────────────
SETTING_DESCRIPTIONS = {
    "autovacuum":
        "Master on/off switch for autovacuum",
    "autovacuum_vacuum_threshold":
        "Base dead-row count added to the scale_factor result",
    "autovacuum_vacuum_scale_factor":
        "Fraction of live rows that must be dead to trigger vacuum  ← the big one",
    "autovacuum_analyze_threshold":
        "Base row-change count for analyze trigger",
    "autovacuum_analyze_scale_factor":
        "Fraction of table that must change to trigger analyze",
    "autovacuum_naptime":
        "How often the autovacuum launcher checks for tables needing work",
    "autovacuum_max_workers":
        "Max concurrent autovacuum worker processes",
    "autovacuum_vacuum_cost_delay":
        "Throttle pause between I/O cost rounds (ms) — higher = slower/gentler",
    "autovacuum_vacuum_cost_limit":
        "I/O cost budget consumed before a throttle pause kicks in",
    "autovacuum_freeze_max_age":
        "Max XID age before a forced anti-wraparound vacuum is triggered",
    "autovacuum_vacuum_insert_threshold":
        "Inserted-row count before autovacuum fires (PostgreSQL 13+)",
    "autovacuum_vacuum_insert_scale_factor":
        "Fraction of inserted rows that trigger autovacuum (PostgreSQL 13+)",
    "maintenance_work_mem":
        "Memory available per vacuum / index operation",
}


def show_settings(gsettings: Dict) -> None:
    console.print()
    console.print(Panel("[bold cyan]⚙  Global Autovacuum Settings[/bold cyan]", expand=False))

    t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", padding=(0, 1))
    t.add_column("Parameter",    style="cyan",  no_wrap=True)
    t.add_column("Value",        style="white", justify="right")
    t.add_column("Description",  style="dim")

    for param, desc in SETTING_DESCRIPTIONS.items():
        if param in gsettings:
            t.add_row(param, gsettings[param], desc)

    console.print(t)
    console.print()
    console.print(
        "  [bold]Vacuum trigger formula:[/bold]  "
        "[cyan]dead_rows > vacuum_threshold + (vacuum_scale_factor × live_rows)[/cyan]\n\n"
        "  [dim]With the default scale_factor of 0.2:\n"
        "    •  1 M-row table  → 200,050 dead rows needed to trigger vacuum\n"
        "    • 10 M-row table  → 2,000,050 dead rows\n"
        "    • 100 M-row table → 20,000,050 dead rows\n"
        "  This is why large tables almost always need their own per-table settings.[/dim]"
    )


def show_xid_warning(xid) -> None:
    if not xid:
        return
    remaining = int(xid["freeze_max_age"]) - int(xid["xid_age"])
    if remaining < XID_CRITICAL_REMAINING:
        console.print()
        console.print(Panel(
            f"[bold red]🚨 CRITICAL — XID Wraparound Risk[/bold red]\n\n"
            f"  XID age       : {fmt_num(xid['xid_age'])}\n"
            f"  Freeze max age: {fmt_num(xid['freeze_max_age'])}\n"
            f"  Remaining     : [bold red]{fmt_num(remaining)} transactions[/bold red]\n\n"
            "  Run VACUUM FREEZE ANALYZE on heavily-updated tables immediately.\n"
            "  If autovacuum is disabled on any table, re-enable it now.",
            title="[bold red]Transaction ID Wraparound[/bold red]",
            expand=False,
        ))
    elif remaining < XID_WARNING_REMAINING:
        console.print()
        console.print(Panel(
            f"[bold yellow]⚠  XID Wraparound Approaching[/bold yellow]\n\n"
            f"  XID age  : {fmt_num(xid['xid_age'])}\n"
            f"  Remaining: [yellow]{fmt_num(remaining)} transactions[/yellow]\n\n"
            "  Monitor closely — ensure autovacuum is keeping up on high-write tables.",
            title="[yellow]Transaction ID Wraparound[/yellow]",
            expand=False,
        ))


def show_table_health(rows: List, gsettings: Dict) -> None:
    console.print()
    console.print(Panel("[bold cyan]📊  Table Vacuum Health[/bold cyan]", expand=False))

    t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", padding=(0, 1))
    t.add_column("Schema.Table",    style="cyan", no_wrap=True, max_width=45)
    t.add_column("Size",            justify="right")
    t.add_column("Live Rows",       justify="right")
    t.add_column("Dead Rows",       justify="right")
    t.add_column("Dead %",          justify="right")
    t.add_column("Trigger At",      justify="right",
                 header="Trigger At\n[dim](dead rows)[/dim]")
    t.add_column("% to Trigger",    justify="right")
    t.add_column("Last Autovacuum", justify="right")
    t.add_column("Status",          justify="center")

    for row in rows:
        relopts   = parse_reloptions(row["reloptions"])
        n_live    = int(row["n_live_tup"] or 0)
        n_dead    = int(row["n_dead_tup"] or 0)
        dead_pct  = float(row["dead_pct"] or 0)

        threshold, _         = effective("autovacuum_vacuum_threshold",    relopts, gsettings)
        scale, has_override  = effective("autovacuum_vacuum_scale_factor", relopts, gsettings)
        trigger              = vacuum_trigger(n_live, threshold, scale)
        pct_to_trigger       = min(round(n_dead / trigger * 100, 1), 999) if trigger > 0 else 0.0

        # Status label
        if dead_pct >= HIGH_DEAD_PCT:
            status = "[bold red]⚠ HIGH BLOAT[/bold red]"
        elif pct_to_trigger >= NEAR_TRIGGER_PCT:
            status = "[bold yellow]⚡ NEAR TRIGGER[/bold yellow]"
        elif n_live >= LARGE_TABLE_ROWS and scale > RECOMMENDED_SCALE_LARGE * 2 and not has_override:
            status = "[yellow]⚙ TUNE[/yellow]"
        else:
            status = "[green]✓ OK[/green]"

        last_av = row["last_autovacuum"]
        if last_av:
            last_av_str = last_av.strftime("%Y-%m-%d %H:%M")
        elif n_live > 0:
            last_av_str = "[red]Never[/red]"
        else:
            last_av_str = "—"

        label = f"{row['schemaname']}.{row['tablename']}"
        if has_override:
            label += " [dim]†[/dim]"

        t.add_row(
            label,
            fmt_bytes(row["total_size_bytes"] or 0),
            fmt_num(n_live),
            fmt_num(n_dead),
            f"{dead_pct:.1f}%",
            fmt_num(trigger),
            f"{pct_to_trigger:.0f}%",
            last_av_str,
            status,
        )

    console.print(t)
    console.print("  [dim]† Table has per-table autovacuum storage parameters set[/dim]")
    console.print("  [dim]⚙ TUNE = large table on default scale_factor — see recommendations below[/dim]")


def show_recommendations(recs: List) -> None:
    console.print()
    if not recs:
        console.print(Panel(
            "[bold green]✓  No per-table tuning needed — all large tables look well-configured.[/bold green]",
            expand=False,
        ))
        return

    console.print(Panel(
        f"[bold yellow]🔧  Per-Table Tuning Recommendations — {len(recs)} table(s)[/bold yellow]\n\n"
        "[dim]Large tables with the default scale_factor of 0.2 allow excessive bloat to build up\n"
        "before autovacuum fires.  The ALTER TABLE statements below lower that threshold so that\n"
        "vacuum keeps up with your write rate.  Review and adjust the values for your workload.[/dim]",
        expand=False,
    ))

    for rec in recs:
        fqtn = f"{rec['schema']}.{rec['table']}"
        console.print()
        console.print(
            f"  [bold cyan]{fqtn}[/bold cyan]  "
            f"[dim]{fmt_bytes(rec['size'])} · {fmt_num(rec['n_live'])} live rows[/dim]"
        )
        console.print(
            f"    Current  → vacuum fires at [red]{fmt_num(rec['current_trigger'])} dead rows[/red]  "
            f"[dim](scale_factor={rec['current_scale']}, threshold={int(rec['current_threshold'])})[/dim]"
        )
        console.print(
            f"    Proposed → vacuum fires at [green]{fmt_num(rec['new_trigger'])} dead rows[/green]  "
            f"[dim](scale_factor={RECOMMENDED_SCALE_LARGE}, threshold={RECOMMENDED_THRESHOLD})[/dim]"
        )
        console.print()
        console.print(f"    [bold green]ALTER TABLE {fqtn} SET ([/bold green]")
        console.print(f"    [bold green]    autovacuum_vacuum_scale_factor = {RECOMMENDED_SCALE_LARGE},[/bold green]")
        console.print(f"    [bold green]    autovacuum_vacuum_threshold    = {RECOMMENDED_THRESHOLD}[/bold green]")
        console.print(f"    [bold green]);[/bold green]")


def show_summary(rows: List, recs: List) -> None:
    total      = len(rows)
    high_bloat = sum(1 for r in rows if float(r["dead_pct"] or 0) >= HIGH_DEAD_PCT)
    never_av   = sum(1 for r in rows if not r["last_autovacuum"] and int(r["n_live_tup"] or 0) > 0)
    tune_count = len(recs)

    console.print()
    console.print(Panel(
        f"[bold]Summary[/bold]\n\n"
        f"  Tables analyzed      : {total}\n"
        f"  High bloat (≥{HIGH_DEAD_PCT:.0f}% dead): "
        f"[{'bold red' if high_bloat else 'green'}]{high_bloat}[/{'bold red' if high_bloat else 'green'}]\n"
        f"  Never autovacuumed   : "
        f"[{'bold red' if never_av else 'green'}]{never_av}[/{'bold red' if never_av else 'green'}]\n"
        f"  Need per-table tuning: "
        f"[{'bold yellow' if tune_count else 'green'}]{tune_count}[/{'bold yellow' if tune_count else 'green'}]",
        expand=False,
    ))


# ── Core ──────────────────────────────────────────────────────────────────────
def build_recommendations(rows: List, gsettings: Dict) -> List:
    """Identify large tables that would benefit from per-table vacuum settings."""
    recs = []
    for row in rows:
        n_live = int(row["n_live_tup"] or 0)
        if n_live < LARGE_TABLE_ROWS:
            continue

        relopts = parse_reloptions(row["reloptions"])
        scale, has_override = effective("autovacuum_vacuum_scale_factor", relopts, gsettings)
        threshold, _        = effective("autovacuum_vacuum_threshold",    relopts, gsettings)

        # Skip if already tuned to a reasonable value
        if has_override and scale <= RECOMMENDED_SCALE_LARGE * 5:
            continue

        # Only flag if scale_factor is meaningfully higher than recommended
        if scale > RECOMMENDED_SCALE_LARGE * 2:
            recs.append({
                "schema":            row["schemaname"],
                "table":             row["tablename"],
                "n_live":            n_live,
                "size":              row["total_size_bytes"] or 0,
                "current_scale":     scale,
                "current_threshold": threshold,
                "current_trigger":   vacuum_trigger(n_live, threshold, scale),
                "new_trigger":       vacuum_trigger(n_live, RECOMMENDED_THRESHOLD,
                                                    RECOMMENDED_SCALE_LARGE),
            })
    return recs


def run(conn_string: str, schema: Optional[str], min_rows: int) -> None:
    console.print()
    console.print(Panel(
        "[bold green]🧙 pg-vacuum-advisor[/bold green]\n"
        "[dim]PostgreSQL Autovacuum Health Checker & Tuning Advisor[/dim]",
        expand=False,
    ))

    try:
        conn = psycopg2.connect(conn_string)
        conn.set_session(readonly=True, autocommit=True)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute(SQL_SETTINGS)
        gsettings = {r["name"]: r["setting"] for r in cur.fetchall()}

        schema_filter = f"AND s.schemaname = '{schema}'" if schema else ""
        rows_filter   = f"AND s.n_live_tup >= {min_rows}" if min_rows > 0 else ""
        cur.execute(SQL_TABLES.format(schema_filter=schema_filter, rows_filter=rows_filter))
        rows = cur.fetchall()

        cur.execute(SQL_XID)
        xid = cur.fetchone()

        cur.close()
        conn.close()

    except psycopg2.OperationalError as e:
        console.print(f"\n[bold red]Could not connect:[/bold red] {e}")
        sys.exit(1)
    except psycopg2.Error as e:
        console.print(f"\n[bold red]Database error:[/bold red] {e}")
        sys.exit(1)

    recs = build_recommendations(rows, gsettings)

    show_settings(gsettings)
    show_xid_warning(xid)
    show_table_health(rows, gsettings)
    show_recommendations(recs)
    show_summary(rows, recs)


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        description="pg-vacuum-advisor — PostgreSQL Autovacuum Health Checker & Tuning Advisor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python vacuum_advisor.py --conn "postgresql://user:pass@host:5432/mydb"
  python vacuum_advisor.py -H myhost -d mydb -U postgres
  python vacuum_advisor.py -H myhost -d mydb -U postgres --schema public
  python vacuum_advisor.py -H myhost -d mydb -U postgres --min-rows 500000
        """,
    )

    conn_grp = ap.add_mutually_exclusive_group(required=True)
    conn_grp.add_argument(
        "--conn", metavar="DSN",
        help="Full DSN: postgresql://user:pass@host:5432/dbname",
    )
    conn_grp.add_argument("-H", "--host", dest="host", metavar="HOST")

    ap.add_argument("-p", "--port",     default="5432", metavar="PORT")
    ap.add_argument("-d", "--dbname",   metavar="DB",   help="Database name")
    ap.add_argument("-U", "--username", metavar="USER", help="Database user")
    ap.add_argument("-W", "--password", metavar="PASS",
                    help="Password (or set the PGPASSWORD environment variable)")
    ap.add_argument("--schema",   metavar="SCHEMA",
                    help="Restrict analysis to a single schema")
    ap.add_argument("--min-rows", metavar="N", type=int, default=0,
                    help="Only report tables with at least N live rows")

    args = ap.parse_args()

    if args.conn:
        conn_string = args.conn
    else:
        if not args.dbname:
            ap.error("--dbname / -d is required when using -H / --host")
        parts = [f"host={args.host}", f"port={args.port}", f"dbname={args.dbname}"]
        if args.username:
            parts.append(f"user={args.username}")
        if args.password:
            parts.append(f"password={args.password}")
        conn_string = " ".join(parts)

    run(conn_string, schema=args.schema, min_rows=args.min_rows)


if __name__ == "__main__":
    main()
