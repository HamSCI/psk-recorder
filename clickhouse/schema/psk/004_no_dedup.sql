-- psk-recorder: switch psk.spots from ReplacingMergeTree → MergeTree
-- so every raw decode event is preserved (schema v4).
--
-- ReplacingMergeTree was the original engine because the ORDER BY tuple
-- was framed as a "natural dedup key".  Operators have since clarified
-- that they want every raw spot record kept — duplicates from log
-- re-reads / restarts / replay should remain visible for forensic
-- analysis.  PSK Reporter's protocol-level dedup (20-min window) is
-- unaffected: that happens in the pskreporter library AFTER psk.spots,
-- not in ClickHouse.
--
-- ClickHouse can't ALTER engine in-place; this migration is performed
-- by a clone-and-swap.  Idempotent: safe to re-run (the EXCHANGE is
-- a no-op when psk.spots is already MergeTree, and CREATE … IF NOT
-- EXISTS guards the staging table).

CREATE TABLE IF NOT EXISTS psk.spots_mt_migration
ENGINE = MergeTree()
ORDER BY (host_call, mode, frequency, time, message)
AS SELECT * FROM psk.spots WHERE 1 = 0;

INSERT INTO psk.spots_mt_migration SELECT * FROM psk.spots;

EXCHANGE TABLES psk.spots AND psk.spots_mt_migration;

DROP TABLE IF EXISTS psk.spots_mt_migration;
