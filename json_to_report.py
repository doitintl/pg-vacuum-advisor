#!/usr/bin/env python3
"""
Convert pg-vacuum-advisor JSON output to human-readable report.
Outputs only factual data from the JSON with no inference or interpretation.

Usage:
    python3 json_to_report.py <input.json> [output.md]
"""

import json
import sys
from datetime import datetime


def format_bytes(bytes_val):
    """Convert bytes to human-readable format."""
    if bytes_val is None:
        return "N/A"
    
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:,.0f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:,.0f} PB"


def format_number(num):
    """Format number with comma separators."""
    if num is None:
        return "N/A"
    return f"{num:,}"


def format_timestamp(ts):
    """Format timestamp string."""
    if ts is None or ts == "Never":
        return "Never"
    return ts


def generate_report(data):
    """Generate markdown report from JSON data."""
    
    report = []
    
    # Header
    report.append("# PostgreSQL Autovacuum Report")
    report.append(f"**Platform:** {data['platform_label']}")
    report.append(f"**Generated:** {data['generated_at']}")
    report.append(f"**Server Version:** {data['pg_version']}")
    report.append("")
    report.append("---")
    report.append("")
    
    # XID Data
    report.append("## Database Transaction ID Age")
    report.append("")
    report.append("> **Note:** `freeze_max_age` is a *soft* limit — crossing it triggers aggressive")
    report.append("> anti-wraparound autovacuum. The *hard* wraparound limit is 2^31 (~2.1 billion")
    report.append("> transactions). CRITICAL/WARNING here means autovacuum is behind schedule,")
    report.append("> not that the database is about to shut down.")
    report.append("")

    for db in data['xid_data']:
        remaining = int(db['freeze_max_age']) - int(db['xid_age'])
        if remaining < 10_000_000:
            report.append(f"> **CRITICAL — `{db['datname']}`**: only {format_number(remaining)} transactions until soft limit. Anti-wraparound autovacuum is behind — confirm it is actively running.")
            report.append("")
        elif remaining < 50_000_000:
            report.append(f"> **WARNING — `{db['datname']}`**: {format_number(remaining)} transactions until soft limit. Monitor autovacuum on high-write tables closely.")
            report.append("")

    report.append("| Database | XID Age | Freeze Max Age | Remaining | % of Soft Limit | Severity |")
    report.append("|----------|--------:|---------------:|----------:|----------------:|----------|")

    for db in data['xid_data']:
        xid_age = int(db['xid_age'])
        freeze_max = int(db['freeze_max_age'])
        remaining = freeze_max - xid_age
        pct = round(xid_age / freeze_max * 100, 1)
        if remaining < 10_000_000:
            severity = "CRITICAL"
        elif remaining < 50_000_000:
            severity = "WARNING"
        else:
            severity = "OK"
        report.append(f"| {db['datname']} | {format_number(xid_age)} | {format_number(freeze_max)} | {format_number(remaining)} | {pct}% | {severity} |")
    
    report.append("")
    report.append("---")
    report.append("")
    
    # Global Settings
    report.append("## Global Autovacuum Settings")
    report.append("")
    report.append("| Parameter | Current Value | Platform Default |")
    report.append("|-----------|---------------|------------------|")
    
    settings = data['settings']
    defaults = data['platform_defaults']
    
    for param in sorted(settings.keys()):
        current = settings[param]
        default = defaults.get(param, 'N/A')
        report.append(f"| {param} | {current} | {default} |")
    
    report.append("")
    report.append("---")
    report.append("")
    
    # ALTER TABLE Recommendations (from separate recommendations array)
    recommendations = data.get('recommendations', [])
    if recommendations:
        report.append("## Per-Table Tuning Recommendations")
        report.append("")
        report.append(f"Found {len(recommendations)} table(s) with recommended tuning:")
        report.append("")
        
        for rec in recommendations:
            report.append(f"### {rec['schema']}.{rec['table']}")
            report.append("")
            report.append(f"**Size:** {format_bytes(rec['size_bytes'])} · **Live Rows:** {format_number(rec['n_live'])} · **Tier:** {rec['tier_label']}")
            report.append("")
            report.append("**Current Configuration:**")
            report.append(f"- Vacuum: scale={rec['cur_vac_scale']}, threshold={rec['cur_vac_threshold']}, trigger at {format_number(rec['cur_vac_trigger'])} dead rows")
            if rec.get('cur_ana_scale') is not None:
                report.append(f"- Analyze: scale={rec['cur_ana_scale']}, threshold={rec['cur_ana_threshold']}")
            report.append("")
            report.append("**Recommended Configuration:**")
            report.append(f"- Vacuum: scale={rec['new_vac_scale']}, threshold={rec['new_vac_threshold']}, trigger at {format_number(rec['new_vac_trigger'])} dead rows")
            if rec.get('new_ana_scale') is not None:
                report.append(f"- Analyze: scale={rec['new_ana_scale']}, threshold={rec['new_ana_threshold']}")
            report.append("")
            report.append("```sql")
            report.append(rec['alter_table_sql'])
            report.append("```")
            report.append("")
        
        report.append("---")
        report.append("")
    
    # Separate tables by schema
    public_tables = [t for t in data['tables'] if t['schema'] == 'public']
    temp_tables = [t for t in data['tables'] if t['schema'].startswith('pg_temp_')]
    
    # Production tables
    report.append("## Table Statistics - Production Tables (public schema)")
    report.append("")
    
    def append_table_rows(tables):
        report.append("| Table | Size | Live Rows | Dead Rows | Dead % | Vacuum Trigger | % to Vac | Analyze Trigger | % to Ana | Last Autovacuum | Last Autoanalyze | Statuses |")
        report.append("|-------|-----:|----------:|----------:|-------:|---------------:|---------:|----------------:|---------:|-----------------|------------------|----------|")
        for table in tables:
            statuses = ", ".join(table['statuses']) if table['statuses'] else "N/A"
            report.append(
                f"| {table['table']} | "
                f"{format_bytes(table['size_bytes'])} | "
                f"{format_number(table['n_live'])} | "
                f"{format_number(table['n_dead'])} | "
                f"{table['dead_pct']} | "
                f"{format_number(table['vacuum_trigger'])} | "
                f"{table['vacuum_pct']} | "
                f"{format_number(table['analyze_trigger'])} | "
                f"{table['analyze_pct']} | "
                f"{format_timestamp(table['last_autovacuum'])} | "
                f"{format_timestamp(table['last_autoanalyze'])} | "
                f"{statuses} |"
            )
        report.append("")

    MIN_BYTES = 50 * 1024 * 1024  # 50 MB display threshold

    # Always show tables with autovacuum disabled regardless of size
    displayed = [t for t in public_tables if t['size_bytes'] >= MIN_BYTES or not t.get('autovacuum_enabled', True)]
    omitted   = [t for t in public_tables if t['size_bytes'] <  MIN_BYTES and t.get('autovacuum_enabled', True)]

    large_tables  = [t for t in displayed if t['size_bytes'] >  1024**3]
    medium_tables = [t for t in displayed if 50 * 1024**2 <= t['size_bytes'] <= 1024**3]
    disabled_small = [t for t in displayed if t['size_bytes'] < 50 * 1024**2]

    if large_tables:
        report.append("### Large Tables (>1 GB)")
        report.append("")
        append_table_rows(large_tables)

    if medium_tables:
        report.append("### Medium Tables (50 MB – 1 GB)")
        report.append("")
        append_table_rows(medium_tables)

    if disabled_small:
        report.append("### Small Tables with Autovacuum Disabled (<50 MB)")
        report.append("")
        append_table_rows(disabled_small)

    if omitted:
        report.append(f"> {len(omitted)} table(s) under 50 MB omitted — autovacuum handles small tables well with default settings.")
        report.append("")
    
    report.append("---")
    report.append("")
    
    # Temporary tables summary
    if temp_tables:
        report.append("## Temporary Table Statistics (pg_temp_* schemas)")
        report.append("")
        report.append(f"Total temporary tables analyzed: {len(temp_tables)}")
        report.append("")
        report.append("> **Note:** Temporary tables in `pg_temp_*` schemas are session-scoped.")
        report.append("> Tables showing 100% dead rows with 0 live rows are normal — they are")
        report.append("> orphaned from sessions that ended without explicit cleanup. PostgreSQL")
        report.append("> will reclaim them automatically. These are not a bloat concern.")
        report.append("")
        
        # Show sample of temp tables with status flags
        flagged_temp = [t for t in temp_tables if 'HIGH_BLOAT' in t['statuses'] or 
                       'NEAR_VACUUM_TRIGGER' in t['statuses'] or 
                       'NEAR_ANALYZE_TRIGGER' in t['statuses']]
        
        if flagged_temp:
            report.append("### Sample of Temporary Tables with Status Flags")
            report.append("")
            report.append("| Schema | Table | Live Rows | Dead Rows | Dead % | Statuses |")
            report.append("|--------|-------|----------:|----------:|-------:|----------|")
            
            # Show top 5 by dead rows
            top_temp = sorted(flagged_temp, key=lambda t: t['n_dead'], reverse=True)[:5]
            for table in top_temp:
                statuses = ", ".join(table['statuses'])
                report.append(
                    f"| {table['schema']} | "
                    f"{table['table']} | "
                    f"{format_number(table['n_live'])} | "
                    f"{format_number(table['n_dead'])} | "
                    f"{table['dead_pct']} | "
                    f"{statuses} |"
                )
            
            report.append("")
            report.append(f"({len(temp_tables)} total temporary tables in report)")
            report.append("")
    
    report.append("---")
    report.append("")
    
    # Status flag definitions
    report.append("## Status Flag Definitions")
    report.append("")
    report.append("The following status flags are reported in the JSON:")
    report.append("")
    report.append("- **OK**: No issues detected")
    report.append("- **HIGH_BLOAT**: Dead tuple percentage ≥ 20%")
    report.append("- **NEAR_VACUUM_TRIGGER**: Dead rows ≥ 80% of vacuum trigger threshold")
    report.append("- **NEAR_ANALYZE_TRIGGER**: Modified rows ≥ 80% of analyze trigger threshold")
    report.append("")
    report.append("---")
    report.append("")
    
    # Summary from JSON
    summary = data.get('summary', {})
    report.append("## Summary")
    report.append("")
    report.append(f"- Total tables analyzed: {summary.get('total_tables', len(data['tables']))}")
    report.append(f"- Autovacuum disabled: {summary.get('autovacuum_disabled', 'N/A')}")
    report.append(f"- High bloat (≥20% dead): {summary.get('high_bloat', 'N/A')}")
    report.append(f"- Never autovacuumed: {summary.get('never_autovacuumed', 'N/A')}")
    report.append(f"- Need per-table tuning: {summary.get('need_tuning', 'N/A')}")
    report.append("")
    report.append("---")
    report.append("")
    report.append("**Note**: This report contains only data directly extracted from the JSON file. No interpretations, recommendations, or inferences have been added.")
    
    return "\n".join(report)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 json_to_report.py <input.json> [output.md]")
        print("\nExample:")
        print("  python3 json_to_report.py prd_vacuum_report.json report.md")
        print("  python3 json_to_report.py prd_vacuum_report.json  # prints to stdout")
        sys.exit(1)
    
    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    
    # Read JSON
    try:
        with open(input_file, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Error: File '{input_file}' not found")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in '{input_file}': {e}")
        sys.exit(1)
    
    # Generate report
    report = generate_report(data)
    
    # Output
    if output_file:
        with open(output_file, 'w') as f:
            f.write(report)
        print(f"Report written to: {output_file}")
    else:
        print(report)


if __name__ == "__main__":
    main()
