-- =============================================================================
-- pg-vacuum-advisor — pgbench custom script: update-heavy workload
-- =============================================================================
-- Simulates a write-heavy OLTP workload that generates dead tuples quickly.
-- Designed to work against the standard pgbench_accounts table.
--
-- Usage (after pgbench -i -s 10 <db>):
--   pgbench -c 8 -T 120 -f tests/pgbench_update_heavy.sql <db>
--
-- After running, the advisor will flag pgbench_accounts (1M rows) for tuning:
--   RDS      : vacuum trigger = 50 + 0.1×1,000,000 = 100,050 dead rows
--   Aurora   : vacuum trigger = 50 + 0.2×1,000,000 = 200,050 dead rows
-- =============================================================================

-- Random account id in range
\set aid  random(1, 100000 * :scale)
\set bid  random(1, :scale)
\set tid  random(1, 10 * :scale)
\set delta random(-5000, 5000)

BEGIN;

-- Primary update — generates one dead tuple per execution in pgbench_accounts
UPDATE pgbench_accounts
    SET    abalance = abalance + :delta
    WHERE  aid = :aid;

-- Branch and teller updates (lower frequency dead tuples)
UPDATE pgbench_tellers
    SET    tbalance = tbalance + :delta
    WHERE  tid = :tid;

UPDATE pgbench_branches
    SET    bbalance = bbalance + :delta
    WHERE  bid = :bid;

INSERT INTO pgbench_history (tid, bid, aid, delta, mtime)
    VALUES (:tid, :bid, :aid, :delta, CURRENT_TIMESTAMP);

END;
