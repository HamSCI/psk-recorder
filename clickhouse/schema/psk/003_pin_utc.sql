-- psk-recorder: pin DateTime columns to UTC (schema v3).
--
-- ClickHouse interprets bare `DateTime` columns in the server's
-- `timezone()` setting.  When the server is in a non-UTC zone (e.g.
-- America/Chicago for a deployment that didn't set TZ=UTC) any naive
-- Python datetime written via clickhouse-connect lands as the
-- server-zone interpretation of those values, not UTC — which then
-- diverges from the actual decode time by the local offset.
--
-- ChTailer was emitting naive datetimes.  Now it emits tz-aware UTC,
-- but pinning the column type prevents future regressions: even if
-- some other writer comes along and passes a naive value, the column
-- type forces UTC interpretation.
--
-- Migration is in-place: existing rows keep their stored Unix-second
-- value; only the read-time interpretation changes.  Pre-fix rows will
-- still display 5 h offset on America/Chicago hosts because their
-- Unix-second value was set assuming server-tz interpretation; only
-- post-fix rows have the correct underlying value.

ALTER TABLE psk.spots
  MODIFY COLUMN time DateTime('UTC');

ALTER TABLE psk.spots
  MODIFY COLUMN ingested_at DateTime('UTC');
