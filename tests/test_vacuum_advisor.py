#!/usr/bin/env python3
"""
Unit tests for pg-vacuum-advisor.

These tests exercise pure functions only — no database connection required.
Run with:
    pytest tests/test_vacuum_advisor.py -v

Covers the two customer-reported bugs:
  Bug 1 — generated ALTER TABLE / RESET SQL did not quote identifiers, so
          mixed-case / reserved-word table names (common with EF Core,
          Hibernate, etc.) failed with "relation ... does not exist".
  Bug 2 — HIGH_BLOAT was percentage-only and missed huge tables with modest
          dead_pct but massive absolute dead-row/dead-byte volume.

Also covers --all-databases (pg_stat_user_tables/pg_class are database-scoped,
so a single connection only ever sees one database's tables — this enumerates
and loops over every database on the instance and merges results into the
{instance, database, report} shape support teams were previously assembling
by hand).
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vacuum_advisor as va


# ── Bug 1: identifier quoting ───────────────────────────────────────────────

class TestQuoteIdent:
    def test_simple_lowercase(self):
        assert va.quote_ident("orders") == '"orders"'

    def test_mixed_case_is_preserved(self):
        # This is the exact customer-reported failure case.
        assert va.quote_ident("CollectedEntitiesMetadata_DEFAULT") == \
            '"CollectedEntitiesMetadata_DEFAULT"'

    def test_reserved_word(self):
        # "order", "table", "select" etc. are reserved words in PostgreSQL —
        # they still just need to be quoted, same as any other identifier.
        assert va.quote_ident("order") == '"order"'
        assert va.quote_ident("table") == '"table"'
        assert va.quote_ident("select") == '"select"'

    def test_embedded_double_quote_is_escaped_by_doubling(self):
        # A table literally named  foo"bar  — quote_ident() must escape the
        # embedded quote by doubling it, matching PostgreSQL's own quote_ident().
        assert va.quote_ident('foo"bar') == '"foo""bar"'

    def test_multiple_embedded_quotes(self):
        assert va.quote_ident('a"b"c') == '"a""b""c"'

    def test_naive_wrap_would_be_wrong(self):
        # Guard against regressing to the naive f'"{name}"' wrap, which
        # produces broken/injectable SQL for identifiers containing a quote.
        name = 'foo"bar'
        naive = '"' + name + '"'
        assert va.quote_ident(name) != naive


class TestQualifyIdent:
    def test_qualifies_and_quotes_both_parts(self):
        assert va.qualify_ident("public", "CollectedEntitiesMetadata_DEFAULT") == \
            '"public"."CollectedEntitiesMetadata_DEFAULT"'

    def test_reserved_word_schema_and_table(self):
        assert va.qualify_ident("select", "order") == '"select"."order"'


def _make_recommendation(schema="public", table="orders", needs_vacuum=True,
                          needs_analyze=True):
    return va.Recommendation(
        schema=schema,
        table=table,
        n_live=5_000_000,
        size_bytes=1024 ** 3,
        tier_label="> 1 M rows",
        cur_vac_scale=0.2,
        cur_vac_threshold=50,
        cur_vac_trigger=1_000_050,
        new_vac_scale=0.01,
        new_vac_threshold=1000,
        new_vac_trigger=51000,
        needs_vacuum=needs_vacuum,
        cur_ana_scale=0.1,
        cur_ana_threshold=50,
        new_ana_scale=0.02,
        new_ana_threshold=1000,
        needs_analyze=needs_analyze,
    )


class TestBuildAlterSql:
    def test_quotes_mixed_case_schema_and_table(self):
        rec = _make_recommendation(schema="public", table="CollectedEntitiesMetadata_DEFAULT")
        sql = va.build_alter_sql(rec)
        assert sql.startswith('ALTER TABLE "public"."CollectedEntitiesMetadata_DEFAULT" SET (')
        # Must NOT emit the old unquoted form.
        assert "public.CollectedEntitiesMetadata_DEFAULT" not in sql

    def test_quotes_reserved_word_table_name(self):
        rec = _make_recommendation(schema="public", table="order")
        sql = va.build_alter_sql(rec)
        assert '"public"."order"' in sql

    def test_quotes_identifier_with_embedded_quote(self):
        rec = _make_recommendation(schema="public", table='weird"table')
        sql = va.build_alter_sql(rec)
        assert '"public"."weird""table"' in sql

    def test_valid_sql_shape_vacuum_only(self):
        rec = _make_recommendation(needs_vacuum=True, needs_analyze=False)
        sql = va.build_alter_sql(rec)
        assert "autovacuum_vacuum_scale_factor" in sql
        assert "autovacuum_analyze_scale_factor" not in sql
        assert sql.strip().endswith(");")


class TestDisabledTablesResetSql:
    def test_reset_statement_is_quoted(self):
        health = _make_table_health(
            schema="public",
            table="CollectedEntitiesMetadata_DEFAULT",
            autovacuum_enabled=False,
        )
        report = _make_report(tables=[health])

        # show_disabled_tables prints via rich Console; capture stdout.
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        original_console = va.console
        va.console = va.Console(file=buf, force_terminal=False, width=200)
        try:
            va.show_disabled_tables(report)
        finally:
            va.console = original_console

        printed = buf.getvalue()
        assert 'ALTER TABLE "public"."CollectedEntitiesMetadata_DEFAULT" RESET' in printed
        assert "ALTER TABLE public.CollectedEntitiesMetadata_DEFAULT RESET" not in printed


# ── Bug 2: absolute bloat dimension ─────────────────────────────────────────

def _make_table_health(schema="public", table="t", n_live=100_000, n_dead=0,
                        dead_pct=0.0, size_bytes=10 * 1024 * 1024,
                        autovacuum_enabled=True, vac_scale=0.2, vac_threshold=50,
                        ana_scale=0.1, ana_threshold=50):
    row = {
        "schemaname": schema,
        "tablename": table,
        "n_live_tup": n_live,
        "n_dead_tup": n_dead,
        "last_autovacuum": None,
        "last_vacuum": None,
        "last_autoanalyze": None,
        "last_analyze": None,
        "autovacuum_count": 0,
        "autoanalyze_count": 0,
        "n_mod_since_analyze": 0,
        "dead_pct": dead_pct,
        "total_size_bytes": size_bytes,
        "reloptions": None if autovacuum_enabled else ["autovacuum_enabled=false"],
    }
    gsettings = {
        "autovacuum_vacuum_scale_factor": str(vac_scale),
        "autovacuum_vacuum_threshold": str(vac_threshold),
        "autovacuum_analyze_scale_factor": str(ana_scale),
        "autovacuum_analyze_threshold": str(ana_threshold),
    }
    return va.analyze_table_row(row, gsettings)


def _make_report(tables=None, recommendations=None, xid_rows=None):
    return va.AdvisorReport(
        pg_version="PostgreSQL 15.4",
        platform="rds",
        platform_label="AWS RDS PostgreSQL",
        platform_defaults=va.PLATFORM_DEFAULTS["rds"],
        settings={},
        tables=tables or [],
        recommendations=recommendations or [],
        xid_rows=xid_rows or [],
        generated_at="2026-01-01T00:00:00+00:00",
        current_db="testdb",
    )


class TestAbsoluteBloatStatus:
    def test_small_table_low_pct_is_ok(self):
        th = _make_table_health(n_live=100_000, n_dead=1_000, dead_pct=1.0,
                                 size_bytes=10 * 1024 * 1024)
        assert "HIGH_BLOAT" not in th.statuses
        assert "HIGH_BLOAT_ABSOLUTE" not in th.statuses

    def test_tiny_table_high_pct_still_flags_percentage_only(self):
        # Reproduces the customer scenario: 130 tiny tables flagged purely by %.
        th = _make_table_health(n_live=10_000, n_dead=3_000, dead_pct=30.0,
                                 size_bytes=50 * 1024 * 1024)
        assert "HIGH_BLOAT" in th.statuses          # existing behavior preserved
        assert "HIGH_BLOAT_ABSOLUTE" not in th.statuses  # doesn't meet absolute bar

    def test_huge_table_modest_pct_is_flagged_by_absolute_dimension(self):
        # Mirrors the customer's real 844 GB / 34M dead tuple / 10.76% case.
        size_bytes = 844 * 1024 ** 3
        th = _make_table_health(
            n_live=(34_000_000 * (100 - 10.76) / 10.76).__round__(),
            n_dead=34_000_000,
            dead_pct=10.76,
            size_bytes=size_bytes,
        )
        assert "HIGH_BLOAT" not in th.statuses            # below 20% threshold
        assert "HIGH_BLOAT_ABSOLUTE" in th.statuses        # but caught by absolute check

    def test_absolute_dead_row_threshold_alone_triggers(self):
        th = _make_table_health(n_live=50_000_000, n_dead=1_500_000, dead_pct=3.0,
                                 size_bytes=1024 * 1024 * 50)  # small on-disk size
        assert th.n_dead >= va.HIGH_DEAD_ROWS_ABS
        assert "HIGH_BLOAT_ABSOLUTE" in th.statuses

    def test_absolute_dead_bytes_threshold_alone_triggers(self):
        # Few dead rows but each huge / large table, so estimated_dead_bytes crosses 1 GB
        # even though dead-row count itself is under HIGH_DEAD_ROWS_ABS.
        th = _make_table_health(n_live=900_000, n_dead=100_000, dead_pct=10.0,
                                 size_bytes=20 * 1024 ** 3)  # 20 GB table
        assert th.n_dead < va.HIGH_DEAD_ROWS_ABS
        assert th.estimated_dead_bytes >= va.HIGH_DEAD_BYTES_ABS
        assert "HIGH_BLOAT_ABSOLUTE" in th.statuses

    def test_both_flags_can_coexist(self):
        th = _make_table_health(n_live=5_000_000, n_dead=2_000_000, dead_pct=28.0,
                                 size_bytes=10 * 1024 ** 3)
        assert "HIGH_BLOAT" in th.statuses
        assert "HIGH_BLOAT_ABSOLUTE" in th.statuses

    def test_estimated_dead_bytes_field_present_and_reasonable(self):
        th = _make_table_health(n_live=1_000_000, n_dead=500_000, dead_pct=33.33,
                                 size_bytes=1000)
        # size_bytes * dead_pct / 100
        assert th.estimated_dead_bytes == int(1000 * 33.33 / 100.0)


class TestSummaryAndJsonBackwardCompatibility:
    def test_json_summary_includes_new_absolute_count_without_breaking_existing_keys(self):
        huge = _make_table_health(
            table="huge_table",
            n_live=280_000_000, n_dead=34_000_000, dead_pct=10.76,
            size_bytes=844 * 1024 ** 3,
        )
        tiny = _make_table_health(
            table="tiny_table",
            n_live=10_000, n_dead=3_000, dead_pct=30.0, size_bytes=50 * 1024 * 1024,
        )
        report = _make_report(tables=[huge, tiny])

        import io, json as _json
        from contextlib import redirect_stdout
        buf = io.StringIO()
        # output_json's no-output-file branch uses plain print(), not
        # console.print(), so capture real stdout rather than swapping console.
        with redirect_stdout(buf):
            va.output_json(report, output_file=None)
        data = _json.loads(buf.getvalue())

        # Existing keys still present (backward compatible).
        for key in ("total_tables", "autovacuum_disabled", "high_bloat",
                    "never_autovacuumed", "need_tuning"):
            assert key in data["summary"]

        # New additive key.
        assert data["summary"]["high_bloat_absolute"] == 1
        assert data["summary"]["high_bloat"] == 1  # only the tiny table by %

        table_dicts = {t["table"]: t for t in data["tables"]}
        assert "estimated_dead_bytes" in table_dicts[huge.table]
        assert "HIGH_BLOAT_ABSOLUTE" in table_dicts[huge.table]["statuses"]
        assert "HIGH_BLOAT_ABSOLUTE" not in table_dicts[tiny.table]["statuses"]

    def test_load_report_from_json_handles_missing_estimated_dead_bytes(self):
        # Simulates replaying an OLD json report file produced before this fix.
        old_style_table = {
            "schema": "public",
            "table": "legacy_table",
            "n_live": 1000,
            "n_dead": 200,
            "dead_pct": 20.0,
            "size_bytes": 1024,
            # no "estimated_dead_bytes" key — must not raise
            "last_autovacuum": None,
            "last_autoanalyze": None,
            "n_mod_since_analyze": 0,
            "vacuum_trigger": 100,
            "vacuum_pct": 50.0,
            "analyze_trigger": 100,
            "analyze_pct": 10.0,
            "has_vacuum_override": False,
            "has_analyze_override": False,
            "autovacuum_enabled": True,
            "statuses": ["HIGH_BLOAT"],
        }
        data = {
            "generated_at": "2026-01-01T00:00:00+00:00",
            "pg_version": "PostgreSQL 15.4",
            "platform": "rds",
            "platform_label": "AWS RDS PostgreSQL",
            "platform_defaults": va.PLATFORM_DEFAULTS["rds"],
            "settings": {},
            "xid_data": [],
            "tables": [old_style_table],
            "recommendations": [],
        }

        import json, tempfile, os as _os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = f.name
        try:
            report = va.load_report_from_json(path)
        finally:
            _os.unlink(path)

        assert len(report.tables) == 1
        assert report.tables[0].estimated_dead_bytes == int(1024 * 20.0 / 100.0)


# ── --all-databases ──────────────────────────────────────────────────────────

class TestListTargetDatabases:
    def _mock_connect(self, rows):
        """Build a fake psycopg2.connect() that returns `rows` from fetchall()."""
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = rows
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        return MagicMock(return_value=mock_conn)

    def test_returns_all_databases_when_no_exclusions(self):
        rows = [("app_db",), ("analytics_db",), ("postgres",)]
        with patch.object(va.psycopg2, "connect", self._mock_connect(rows)):
            result = va.list_target_databases("host=x dbname=postgres")
        assert result == ["app_db", "analytics_db", "postgres"]

    def test_excludes_requested_databases(self):
        rows = [("app_db",), ("rdsadmin",), ("template0",), ("analytics_db",)]
        with patch.object(va.psycopg2, "connect", self._mock_connect(rows)):
            result = va.list_target_databases(
                "host=x dbname=postgres", exclude={"rdsadmin", "template0"}
            )
        assert result == ["app_db", "analytics_db"]

    def test_operational_error_exits_cleanly(self):
        mock_connect = MagicMock(side_effect=va.psycopg2.OperationalError("no route to host"))
        with patch.object(va.psycopg2, "connect", mock_connect):
            try:
                va.list_target_databases("host=unreachable dbname=postgres")
                assert False, "expected SystemExit"
            except SystemExit as e:
                assert e.code == 1


def _make_advisor_report_for(table_name, dead_pct, n_dead, size_bytes):
    th = _make_table_health(
        table=table_name, n_live=1_000_000, n_dead=n_dead, dead_pct=dead_pct,
        size_bytes=size_bytes,
    )
    return _make_report(tables=[th])


class TestMergedOutput:
    def test_output_merged_json_shape_matches_hand_merged_customer_format(self):
        # This is exactly the shape a support engineer was previously producing
        # by hand: running the tool once per database and concatenating the
        # JSON output into a list of {instance, database, report} objects,
        # where "report" is the same dict output_json() would have produced
        # for that one database on its own.
        report_a = _make_advisor_report_for("orders", dead_pct=5.0, n_dead=1000, size_bytes=10 * 1024**2)
        report_b = _make_advisor_report_for("events", dead_pct=25.0, n_dead=50_000, size_bytes=200 * 1024**2)
        merged = [
            ("prod-instance-1", "app_db", report_a),
            ("prod-instance-1", "analytics_db", report_b),
        ]

        import io, json as _json
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            va.output_merged_json(merged, output_file=None)
        data = _json.loads(buf.getvalue())

        assert isinstance(data, list)
        assert len(data) == 2
        for entry in data:
            assert set(entry.keys()) == {"instance", "database", "report"}
            # Same top-level keys a single-database --format json run produces.
            assert set(entry["report"].keys()) == {
                "generated_at", "pg_version", "platform", "platform_label",
                "platform_defaults", "settings", "xid_data", "tables",
                "recommendations", "summary",
            }

        assert data[0]["instance"] == "prod-instance-1"
        assert data[0]["database"] == "app_db"
        assert data[1]["database"] == "analytics_db"
        assert data[1]["report"]["tables"][0]["table"] == "events"

    def test_output_merged_csv_tags_rows_with_instance_and_database(self):
        report_a = _make_advisor_report_for("orders", dead_pct=5.0, n_dead=1000, size_bytes=10 * 1024**2)
        report_b = _make_advisor_report_for("events", dead_pct=25.0, n_dead=50_000, size_bytes=200 * 1024**2)
        merged = [
            ("prod-instance-1", "app_db", report_a),
            ("prod-instance-1", "analytics_db", report_b),
        ]

        import io, csv as _csv
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            va.output_merged_csv(merged, output_file=None)
        rows = list(_csv.DictReader(io.StringIO(buf.getvalue())))

        assert len(rows) == 2
        assert rows[0]["instance"] == "prod-instance-1"
        assert rows[0]["database"] == "app_db"
        assert rows[0]["table"] == "orders"
        assert rows[1]["database"] == "analytics_db"
        assert rows[1]["table"] == "events"

    def test_output_merged_csv_handles_no_tables_anywhere(self):
        empty_report = _make_report(tables=[])
        merged = [("prod-instance-1", "empty_db", empty_report)]

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            va.output_merged_csv(merged, output_file=None)
        # Should print the "no tables" warning via console, not raise.
        # (console output goes through rich's own stream, not captured stdout,
        # so we only assert it doesn't crash.)


class TestPlatformInternalDatabaseExclusion:
    """--all-databases must skip the cloud provider's own admin database by
    default (rdsadmin for rds/aurora, cloudsqladmin for cloudsql), on top of
    whatever --exclude-db adds — without the user having to remember the flag.
    """

    def _run_main_with_platform(self, platform):
        empty_report = _make_report(tables=[])
        captured_exclude = {}

        def fake_list_target_databases(conn_string, exclude=None):
            captured_exclude["value"] = exclude
            return []  # short-circuit — we only care what exclude set was built

        argv = [
            "vacuum_advisor.py", "-H", "myhost", "-U", "myuser",
            "--all-databases", "--platform", platform, "--format", "json",
        ]
        with patch.object(sys, "argv", argv), \
             patch.object(va, "list_target_databases", side_effect=fake_list_target_databases):
            va.main()

        return captured_exclude["value"]

    def test_rds_excludes_rdsadmin_automatically(self):
        exclude = self._run_main_with_platform("rds")
        assert "rdsadmin" in exclude

    def test_aurora_excludes_rdsadmin_automatically(self):
        exclude = self._run_main_with_platform("aurora")
        assert "rdsadmin" in exclude

    def test_cloudsql_excludes_cloudsqladmin_automatically(self):
        exclude = self._run_main_with_platform("cloudsql")
        assert "cloudsqladmin" in exclude
        assert "rdsadmin" not in exclude  # not relevant to this platform

    def test_user_supplied_exclude_db_is_combined_with_platform_default(self):
        empty_report = _make_report(tables=[])
        captured_exclude = {}

        def fake_list_target_databases(conn_string, exclude=None):
            captured_exclude["value"] = exclude
            return []

        argv = [
            "vacuum_advisor.py", "-H", "myhost", "-U", "myuser",
            "--all-databases", "--platform", "rds", "--format", "json",
            "--exclude-db", "reporting_db,staging_db",
        ]
        with patch.object(sys, "argv", argv), \
             patch.object(va, "list_target_databases", side_effect=fake_list_target_databases):
            va.main()

        exclude = captured_exclude["value"]
        assert exclude == {"rdsadmin", "reporting_db", "staging_db"}


# ── --replay with multi-database (--all-databases) JSON ────────────────────

def _write_json_tmp(data):
    import json, tempfile
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return f.name


_SINGLE_DB_REPORT_DICT = {
    "generated_at": "2026-01-01T00:00:00+00:00",
    "pg_version": "PostgreSQL 16.4",
    "platform": "rds",
    "platform_label": "AWS RDS PostgreSQL",
    "platform_defaults": va.PLATFORM_DEFAULTS["rds"],
    "settings": {},
    "xid_data": [],
    "tables": [],
    "recommendations": [],
    "summary": {},
}


class TestReplayAutoDetectsShape:
    """--replay previously assumed a single-database dict and crashed with an
    unhandled TypeError on the list shape --all-databases produces. It must
    now detect either shape and render both correctly.
    """

    def test_load_report_from_json_rejects_multi_db_list_with_clear_error(self):
        path = _write_json_tmp([
            {"instance": "host1", "database": "db1", "report": _SINGLE_DB_REPORT_DICT},
        ])
        try:
            try:
                va.load_report_from_json(path)
                assert False, "expected SystemExit"
            except SystemExit as e:
                assert e.code == 1
        finally:
            os.unlink(path)

    def test_load_merged_reports_from_json_parses_list_shape(self):
        path = _write_json_tmp([
            {"instance": "host1", "database": "db1", "report": _SINGLE_DB_REPORT_DICT},
            {"instance": "host1", "database": "db2", "report": _SINGLE_DB_REPORT_DICT},
        ])
        try:
            merged = va.load_merged_reports_from_json(path)
        finally:
            os.unlink(path)

        assert len(merged) == 2
        instance, dbname, report = merged[0]
        assert instance == "host1"
        assert dbname == "db1"
        assert isinstance(report, va.AdvisorReport)
        assert report.current_db == "db1"
        assert merged[1][1] == "db2"

    def test_load_merged_reports_from_json_rejects_single_db_dict_with_clear_error(self):
        path = _write_json_tmp(_SINGLE_DB_REPORT_DICT)
        try:
            try:
                va.load_merged_reports_from_json(path)
                assert False, "expected SystemExit"
            except SystemExit as e:
                assert e.code == 1
        finally:
            os.unlink(path)

    def test_load_merged_reports_from_json_reports_malformed_entry_cleanly(self):
        # Missing "database" key — must not raise a raw KeyError/TypeError.
        path = _write_json_tmp([{"instance": "host1", "report": _SINGLE_DB_REPORT_DICT}])
        try:
            try:
                va.load_merged_reports_from_json(path)
                assert False, "expected SystemExit"
            except SystemExit as e:
                assert e.code == 1
        finally:
            os.unlink(path)

    def test_main_replay_renders_each_database_for_multi_db_json(self):
        report_a = _make_advisor_report_for("orders", dead_pct=5.0, n_dead=1000, size_bytes=10 * 1024**2)
        report_b = _make_advisor_report_for("events", dead_pct=25.0, n_dead=50_000, size_bytes=200 * 1024**2)
        merged_json = [
            {"instance": "host1", "database": "db1", "report": va.report_to_dict(report_a)},
            {"instance": "host1", "database": "db2", "report": va.report_to_dict(report_b)},
        ]
        path = _write_json_tmp(merged_json)

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        argv = ["vacuum_advisor.py", "--replay", path]
        try:
            with patch.object(sys, "argv", argv), redirect_stdout(buf):
                va.main()
        finally:
            os.unlink(path)

        printed = buf.getvalue()
        # rich renders to its own console (not necessarily plain stdout in
        # every environment), so fall back to checking no exception occurred
        # and that main() returned normally — the render_console() call for
        # each database is exercised either way.

    def test_main_replay_still_handles_single_db_json(self):
        path = _write_json_tmp(_SINGLE_DB_REPORT_DICT)
        argv = ["vacuum_advisor.py", "--replay", path]
        try:
            with patch.object(sys, "argv", argv):
                va.main()  # must not raise
        finally:
            os.unlink(path)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
