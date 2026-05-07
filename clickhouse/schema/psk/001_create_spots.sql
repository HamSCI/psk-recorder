-- psk-recorder: psk.spots — FT4/FT8 decoded spots from decode_ft8.
--
-- Field set is faithful to what `decode_ft8` (ka9q/ft8_lib decode_ft8.c:363)
-- actually emits: ISO timestamp, integer score, signed dt (seconds),
-- absolute frequency in Hz, decoded message text.  Callsign / grid /
-- report parsing is best-effort (the message is freeform; we keep
-- the raw text and surface common forms when we can recognize them).
--
-- ORDER BY tuple is the natural query / dedup key:
-- "spots received by this station, this mode, this band, around this
-- time, with this message" — the message disambiguates spots that
-- collide on (host, mode, freq, time).

CREATE TABLE IF NOT EXISTS psk.spots
(
    -- common header (CONTRACT v0.6 §17 design convention)
    time               DateTime               CODEC(Delta(4), ZSTD(1)),
    mode               LowCardinality(String) CODEC(LZ4),       -- 'ft4' | 'ft8'
    host_call          LowCardinality(String) CODEC(LZ4),
    host_grid          LowCardinality(String) CODEC(LZ4),
    radiod_id          LowCardinality(String) CODEC(LZ4),
    instance           LowCardinality(String) CODEC(LZ4),
    processing_version LowCardinality(String) CODEC(LZ4),

    -- decode_ft8 emits these directly
    score              Int16                  CODEC(T64, ZSTD(1)),   -- decode score (ft8_lib)
    dt                 Float32                CODEC(Delta(4), ZSTD(3)), -- time offset (s)
    frequency          Int64                  CODEC(Delta(8), ZSTD(3)), -- absolute Hz
    frequency_mhz      Float64                CODEC(Delta(8), ZSTD(3)),
    message            String                 CODEC(ZSTD(3)),           -- raw decoded text

    -- best-effort parse of message (nullable when freeform/unparseable)
    tx_call            LowCardinality(String) CODEC(LZ4),
    rx_call            LowCardinality(String) CODEC(LZ4),
    grid               LowCardinality(String) CODEC(LZ4),
    report             Nullable(Int16)        CODEC(T64, ZSTD(1)),

    ingested_at        DateTime DEFAULT now() CODEC(Delta(4), ZSTD(1))
)
ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(time)
ORDER BY (host_call, mode, frequency, time, message)
SETTINGS index_granularity = 32768;
