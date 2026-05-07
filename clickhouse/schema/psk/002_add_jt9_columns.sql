-- psk-recorder: add WSJT-X jt9 columns to psk.spots (schema v2).
--
-- Background: psk-recorder v0.1 used ka9q/ft8_lib's `decode_ft8` and
-- captured its internal "score" metric.  v0.2 swaps to WSJT-X's `jt9`
-- as the default decoder because (1) it reports calibrated dB SNR
-- instead of an opaque score, and (2) it surfaces the spectral_width
-- metric the FT4/FT8 protocol carries that decode_ft8 throws away.
--
-- Both decoders coexist as parallel options (operator picks via
-- `paths.decoder_kind` in the config); rows from either land in this
-- same table and self-identify via the new `decoder_kind` column.
--
-- Migration is purely additive — no rewrites of existing data.  The
-- new columns default to NULL / 'decode_ft8' respectively, so v1 rows
-- read back identically to before.

-- Calibrated dB SNR (jt9 only; NULL for decode_ft8 rows whose
-- internal `score` is not a calibrated dB).
ALTER TABLE psk.spots
  ADD COLUMN IF NOT EXISTS snr_db Nullable(Float32) CODEC(Delta(4), ZSTD(3));

-- Spectral width at 50% energy level in Hz (jt9 only).  NULL when
-- not measured.
ALTER TABLE psk.spots
  ADD COLUMN IF NOT EXISTS spectral_width_hz Nullable(Float32)
    CODEC(Delta(4), ZSTD(3));

-- Decoder identity, retained on every row.  Defaults to 'decode_ft8'
-- so existing rows back-fill correctly; new jt9 rows write 'jt9'.
ALTER TABLE psk.spots
  ADD COLUMN IF NOT EXISTS decoder_kind LowCardinality(String)
    DEFAULT 'decode_ft8' CODEC(LZ4);

-- decode_ft8's internal `score` is not a calibrated SNR; existing
-- rows already have it.  Left as Int16 (non-nullable, default 0) for
-- backward compat — for jt9 rows we write 0 and consumers should
-- prefer `snr_db` when `decoder_kind = 'jt9'`.
