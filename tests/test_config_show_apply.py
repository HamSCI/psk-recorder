"""Cover the JSON I/O the whiptail wizard depends on.

Same shape as mag-recorder's test_config_show_apply.py (sigmond-integration
HEAD on the mag-recorder repo) -- if/when the show/apply/serialize
machinery moves into a sigmond-provided library these tests carry
over with minimal changes.

psk-recorder's apply is intentionally narrower than mag-recorder's:
only [station], [paths], [processing] are writable.  [[radiod]] arrays
of tables pass through unchanged from the existing file but cannot
be set via apply.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tomllib
from pathlib import Path

import pytest

from psk_recorder import configurator
from psk_recorder.config import DEFAULTS


def _ns(**kw) -> argparse.Namespace:
    base = {"config": None, "defaults": False, "json": True,
            "non_interactive": False, "reconfig": False, "log_level": None,
            "path": "-"}
    base.update(kw)
    return argparse.Namespace(**base)


# ---------- config show -----------------------------------------------------

def test_show_defaults_emits_paths_and_processing(tmp_path: Path, capsys) -> None:
    rv = configurator.cmd_config_show(_ns(config=tmp_path / "nope.toml", defaults=True))
    assert rv == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out.keys()) >= {"paths", "processing"}
    assert out["paths"]["decoder"]      == DEFAULTS["paths"]["decoder"]
    assert out["processing"]["radiod_lifetime_frames"] == DEFAULTS["processing"]["radiod_lifetime_frames"]


def test_show_returns_file_contents_without_defaults(tmp_path: Path, capsys) -> None:
    config = tmp_path / "c.toml"
    config.write_text('[station]\ncallsign = "AC0G"\n')
    rv = configurator.cmd_config_show(_ns(config=config, defaults=False))
    assert rv == 0
    assert json.loads(capsys.readouterr().out) == {"station": {"callsign": "AC0G"}}


def test_show_missing_file_without_defaults_returns_empty(tmp_path: Path, capsys) -> None:
    rv = configurator.cmd_config_show(_ns(config=tmp_path / "nope.toml", defaults=False))
    assert rv == 0
    assert json.loads(capsys.readouterr().out) == {}


# ---------- config apply ----------------------------------------------------

def _apply(payload, tmp_path: Path, *, existing: str = "") -> int:
    """Drive cmd_config_apply with payload as stdin; return exit code."""
    config = tmp_path / "c.toml"
    if existing:
        config.write_text(existing)
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        return configurator.cmd_config_apply(_ns(config=config))
    finally:
        sys.stdin = old_stdin


_FIXTURE_WITH_RADIOD = '''\
[station]
callsign = "AC0G"
grid_square = "EM38ww40pk"

[paths]
spool_dir = "/var/lib/psk-recorder"
log_dir = "/var/log/psk-recorder"
decoder = "/usr/local/bin/decode_ft8"
pskreporter = "/usr/local/bin/pskreporter-sender"
keep_wav = false

[processing]
radiod_lifetime_frames = 6000

[[radiod]]
id = "test-rx888"
radiod_status = "test-status.local"

[radiod.ft8]
sample_rate = 12000
preset = "usb"
encoding = "s16be"
freqs_hz = [14074000, 7074000]

[radiod.ft4]
sample_rate = 12000
preset = "usb"
encoding = "s16be"
freqs_hz = [14080000, 7047500]
'''


def test_apply_writes_station_section(tmp_path: Path) -> None:
    rv = _apply({"station": {"callsign": "K1JT", "grid_square": "FN20"}},
                tmp_path, existing=_FIXTURE_WITH_RADIOD)
    assert rv == 0
    with open(tmp_path / "c.toml", "rb") as f:
        loaded = tomllib.load(f)
    assert loaded["station"]["callsign"]    == "K1JT"
    assert loaded["station"]["grid_square"] == "FN20"


def test_apply_preserves_radiod_blocks(tmp_path: Path) -> None:
    """[[radiod]] passes through untouched even though apply doesn't write it."""
    rv = _apply({"station": {"callsign": "K1JT"}},
                tmp_path, existing=_FIXTURE_WITH_RADIOD)
    assert rv == 0
    with open(tmp_path / "c.toml", "rb") as f:
        loaded = tomllib.load(f)
    assert isinstance(loaded["radiod"], list)
    assert loaded["radiod"][0]["id"] == "test-rx888"
    assert loaded["radiod"][0]["ft8"]["freqs_hz"] == [14074000, 7074000]
    assert loaded["radiod"][0]["ft4"]["freqs_hz"] == [14080000, 7047500]


# NOTE: an earlier version of this test asserted that [[radiod]] was
# not writable via apply at all.  The wizard now writes radiod
# blocks (with the inline-edit / pick-a-block flow); the per-block
# validation (id + radiod_status required, no duplicate ids, must
# be a list) is covered by test_apply_rejects_radiod_missing_id /
# missing_status / duplicate_ids / not_a_list below.


def test_apply_rejects_unknown_section(tmp_path: Path, capsys) -> None:
    rv = _apply({"bogus": {"x": 1}}, tmp_path)
    assert rv == 2
    assert "not writable" in capsys.readouterr().err.lower()


def test_apply_rejects_wrong_type(tmp_path: Path, capsys) -> None:
    """paths.keep_wav is a bool; a string must be rejected."""
    rv = _apply({"paths": {"keep_wav": "yes"}}, tmp_path)
    assert rv == 2
    assert "expects bool" in capsys.readouterr().err.lower()


def test_apply_rejects_negative_lifetime(tmp_path: Path, capsys) -> None:
    """processing.radiod_lifetime_frames must be a non-negative int."""
    rv = _apply({"processing": {"radiod_lifetime_frames": -1}},
                tmp_path, existing=_FIXTURE_WITH_RADIOD)
    assert rv == 2


def test_apply_is_atomic_part_rename(tmp_path: Path) -> None:
    """The write goes via .part + rename so a crash mid-write leaves
    the old file intact.  After a successful apply, no .part file
    should remain."""
    rv = _apply({"station": {"callsign": "AC0G"}},
                tmp_path, existing='[station]\ncallsign = "OLD"\n')
    assert rv == 0
    assert (tmp_path / "c.toml").exists()
    assert not (tmp_path / "c.toml.part").exists()


def test_apply_rejects_non_object_payload(tmp_path: Path) -> None:
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(["not", "a", "dict"]))
    try:
        rv = configurator.cmd_config_apply(_ns(config=tmp_path / "c.toml"))
    finally:
        sys.stdin = old_stdin
    assert rv == 2


def test_apply_rejects_invalid_json(tmp_path: Path) -> None:
    old_stdin = sys.stdin
    sys.stdin = io.StringIO("this is not json {{ broken")
    try:
        rv = configurator.cmd_config_apply(_ns(config=tmp_path / "c.toml"))
    finally:
        sys.stdin = old_stdin
    assert rv == 2


def test_apply_deep_merges_with_existing(tmp_path: Path) -> None:
    """Partial payloads preserve existing fields the wizard didn't touch."""
    rv = _apply({"station": {"callsign": "K1JT"}},
                tmp_path, existing=_FIXTURE_WITH_RADIOD)
    assert rv == 0
    with open(tmp_path / "c.toml", "rb") as f:
        loaded = tomllib.load(f)
    assert loaded["station"]["callsign"]    == "K1JT"      # overwritten
    assert loaded["station"]["grid_square"] == "EM38ww40pk"  # preserved
    assert loaded["paths"]["keep_wav"]      is False         # preserved


# ---------- serializer ------------------------------------------------------

def test_serialize_toml_round_trips_via_tomllib() -> None:
    src = {
        "station":    {"callsign": "AC0G", "grid_square": "EM38ww40pk"},
        "paths":      {"decoder": "/usr/local/bin/decode_ft8", "keep_wav": False},
        "processing": {"radiod_lifetime_frames": 6000},
        "radiod": [
            {
                "id": "test-rx888",
                "radiod_status": "test-status.local",
                "ft8": {"sample_rate": 12000, "freqs_hz": [14074000, 7074000]},
                "ft4": {"sample_rate": 12000, "freqs_hz": [14080000, 7047500]},
            },
        ],
    }
    text = configurator._serialize_toml(src)
    loaded = tomllib.loads(text)
    assert loaded == src


def test_serialize_toml_emits_array_of_tables() -> None:
    """[[radiod]] blocks must render with the right header syntax."""
    text = configurator._serialize_toml({
        "radiod": [
            {"id": "a", "radiod_status": "a.local"},
            {"id": "b", "radiod_status": "b.local"},
        ],
    })
    assert text.count("[[radiod]]") == 2
    loaded = tomllib.loads(text)
    assert [b["id"] for b in loaded["radiod"]] == ["a", "b"]


def test_serialize_toml_inline_arrays() -> None:
    """freqs_hz lists should render on one line."""
    text = configurator._serialize_toml({
        "radiod": [
            {"id": "x", "radiod_status": "x.local",
             "ft8": {"freqs_hz": [14074000, 7074000, 3573000]}},
        ],
    })
    # One line with the whole array, not three separate lines.
    assert any("[14074000, 7074000, 3573000]" in line for line in text.splitlines())


# ---------- wizard availability --------------------------------------------

def test_wizard_available_false_without_tty() -> None:
    """In pytest stdout isn't a TTY; the dispatcher must NOT try to exec
    the wizard.  Otherwise piping `psk-recorder config init` would hang."""
    assert configurator._wizard_available() is False


def test_wizard_available_false_when_script_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(configurator, "_WIZARD_PATH", tmp_path / "nope.sh")
    assert configurator._wizard_available() is False


# ---------- [timing] apply -------------------------------------------------

def test_apply_writes_timing_section(tmp_path: Path) -> None:
    rv = _apply({"timing": {"chain_delay_ns": 12345}},
                tmp_path, existing=_FIXTURE_WITH_RADIOD)
    assert rv == 0
    with open(tmp_path / "c.toml", "rb") as f:
        loaded = tomllib.load(f)
    assert loaded["timing"]["chain_delay_ns"] == 12345


# ---------- [[radiod]] apply (overlay-wins) --------------------------------

def test_apply_writes_radiod_blocks(tmp_path: Path) -> None:
    """The operator's full block list replaces the file's list."""
    rv = _apply({"radiod": [
                    {"id": "new-rx888",  "radiod_status": "new-status.local"},
                    {"id": "new2-rx888", "radiod_status": "new2-status.local"},
                ]}, tmp_path, existing=_FIXTURE_WITH_RADIOD)
    assert rv == 0
    with open(tmp_path / "c.toml", "rb") as f:
        loaded = tomllib.load(f)
    ids = [b["id"] for b in loaded["radiod"]]
    assert ids == ["new-rx888", "new2-rx888"]
    # The original 'test-rx888' block (and its freqs_hz) is GONE because
    # overlay-wins replaces the whole list.  This is the documented
    # contract; operators who want to preserve freqs_hz pass them
    # back in the payload, or use the wizard's "Edit raw TOML" path.
    assert "test-rx888" not in ids


def test_apply_rejects_radiod_missing_id(tmp_path: Path, capsys) -> None:
    rv = _apply({"radiod": [{"radiod_status": "x.local"}]}, tmp_path)
    assert rv == 2
    assert "id is required" in capsys.readouterr().err.lower()


def test_apply_rejects_radiod_missing_status(tmp_path: Path, capsys) -> None:
    rv = _apply({"radiod": [{"id": "x"}]}, tmp_path)
    assert rv == 2
    assert "radiod_status is required" in capsys.readouterr().err.lower()


def test_apply_rejects_radiod_duplicate_ids(tmp_path: Path, capsys) -> None:
    rv = _apply({"radiod": [
                    {"id": "dup", "radiod_status": "a.local"},
                    {"id": "dup", "radiod_status": "b.local"},
                ]}, tmp_path)
    assert rv == 2
    assert "duplicate ids" in capsys.readouterr().err.lower()


def test_apply_rejects_radiod_not_a_list(tmp_path: Path, capsys) -> None:
    rv = _apply({"radiod": {"id": "x"}}, tmp_path)  # dict, not list
    assert rv == 2
    assert "must be a list" in capsys.readouterr().err.lower()


# ---------- env show / env apply ------------------------------------------

def _env_ns(**kw) -> argparse.Namespace:
    base = {"instance": None, "json": True, "log_level": None, "path": "-",
            "config": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_env_show_missing_file_returns_empty(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(configurator, "_ENV_DIR", tmp_path)
    rv = configurator.cmd_env_show(_env_ns(instance="nope-rx888"))
    assert rv == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_env_show_parses_existing_file(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(configurator, "_ENV_DIR", tmp_path)
    (tmp_path / "rx0.env").write_text(
        '# leading comment\n'
        'PSK_USE_HS_UPLOADER=1\n'
        'PSK_DELIVERY_PIPELINES="direct,server-raw"\n'
        '\n'
    )
    rv = configurator.cmd_env_show(_env_ns(instance="rx0"))
    assert rv == 0
    out = json.loads(capsys.readouterr().out)
    assert out["PSK_USE_HS_UPLOADER"]      == "1"
    assert out["PSK_DELIVERY_PIPELINES"]   == "direct,server-raw"


def test_env_show_requires_instance(monkeypatch, capsys) -> None:
    rv = configurator.cmd_env_show(_env_ns(instance=None))
    assert rv == 2
    assert "--instance" in capsys.readouterr().err.lower()


def _env_apply(payload, tmp_path: Path, monkeypatch, *,
               instance: str = "rx0", existing: str = "") -> int:
    monkeypatch.setattr(configurator, "_ENV_DIR", tmp_path)
    if existing:
        (tmp_path / f"{instance}.env").write_text(existing)
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        return configurator.cmd_env_apply(_env_ns(instance=instance))
    finally:
        sys.stdin = old_stdin


def test_env_apply_writes_new_file(tmp_path: Path, monkeypatch) -> None:
    rv = _env_apply({"PSK_DELIVERY_PIPELINES": "direct"}, tmp_path, monkeypatch)
    assert rv == 0
    text = (tmp_path / "rx0.env").read_text()
    assert "PSK_DELIVERY_PIPELINES=direct" in text


def test_env_apply_merges_with_existing(tmp_path: Path, monkeypatch) -> None:
    rv = _env_apply({"PSK_DELIVERY_PIPELINES": "direct,server-raw"},
                    tmp_path, monkeypatch,
                    existing="PSK_USE_HS_UPLOADER=1\nPSK_DIRECT_DEDUP=0\n")
    assert rv == 0
    parsed = configurator._parse_env_file(tmp_path / "rx0.env")
    assert parsed["PSK_DELIVERY_PIPELINES"] == "direct,server-raw"
    assert parsed["PSK_USE_HS_UPLOADER"]    == "1"   # preserved
    assert parsed["PSK_DIRECT_DEDUP"]       == "0"   # preserved


def test_env_apply_null_value_deletes_key(tmp_path: Path, monkeypatch) -> None:
    rv = _env_apply({"PSK_DIRECT_DEDUP": None},
                    tmp_path, monkeypatch,
                    existing="PSK_USE_HS_UPLOADER=1\nPSK_DIRECT_DEDUP=1\n")
    assert rv == 0
    parsed = configurator._parse_env_file(tmp_path / "rx0.env")
    assert "PSK_DIRECT_DEDUP" not in parsed
    assert parsed["PSK_USE_HS_UPLOADER"] == "1"


def test_env_apply_rejects_unknown_key(tmp_path: Path, monkeypatch, capsys) -> None:
    rv = _env_apply({"NOT_MY_KEY": "value"}, tmp_path, monkeypatch)
    assert rv == 2
    assert "unknown / unmanaged" in capsys.readouterr().err.lower()


def test_env_apply_rejects_bad_pipeline(tmp_path: Path, monkeypatch, capsys) -> None:
    rv = _env_apply({"PSK_DELIVERY_PIPELINES": "direct,bogus"},
                    tmp_path, monkeypatch)
    assert rv == 2
    assert "unknown pipelines" in capsys.readouterr().err.lower()


def test_env_apply_rejects_non_01_bool(tmp_path: Path, monkeypatch, capsys) -> None:
    rv = _env_apply({"PSK_USE_HS_UPLOADER": "true"},
                    tmp_path, monkeypatch)
    assert rv == 2
    assert "'0' or '1'" in capsys.readouterr().err


def test_env_apply_accepts_legacy_delivery_mode(tmp_path: Path, monkeypatch) -> None:
    rv = _env_apply({"PSK_DELIVERY_MODE": "both"}, tmp_path, monkeypatch)
    assert rv == 0
    parsed = configurator._parse_env_file(tmp_path / "rx0.env")
    assert parsed["PSK_DELIVERY_MODE"] == "both"


def test_env_apply_rejects_invalid_legacy_delivery_mode(tmp_path: Path, monkeypatch, capsys) -> None:
    rv = _env_apply({"PSK_DELIVERY_MODE": "garbage"}, tmp_path, monkeypatch)
    assert rv == 2
    err = capsys.readouterr().err.lower()
    assert "delivery_mode" in err


# ---------- environment-cache radiod picker (cross-pollinated from wspr-recorder)

# These pin the parser logic that lives inside scripts/config-wizard.sh's
# pick_radiod_status function.  The shell scaffolding around it (menu
# construction, fallback path) is mechanical -- the interesting bit is
# this filter, and we want a regression test that catches the next time
# sigmond changes the cache schema.

import subprocess


def _run_parser(cache_path: Path) -> list[tuple[str, str]]:
    """Run the same Python heredoc the wizard runs, return parsed
    (endpoint, label) tuples."""
    src = '''
import json, os
try:
    data = json.load(open(os.environ['CACHE']))
except Exception:
    raise SystemExit(0)
seen = set()
for obs in data.get('observations') or []:
    if obs.get('source') not in ('mdns', 'multicast'):
        continue
    if obs.get('kind') != 'radiod' or not obs.get('ok', True):
        continue
    endpoint = (obs.get('endpoint') or '').rsplit(':', 1)[0]
    if not endpoint or endpoint in seen:
        continue
    seen.add(endpoint)
    fields = obs.get('fields') or {}
    label = (fields.get('mdns_name') or obs.get('id') or endpoint).strip()
    print(f'{endpoint}|{label}')
'''
    out = subprocess.run(
        ["python3", "-c", src],
        env={"CACHE": str(cache_path)},
        capture_output=True, text=True, check=False,
    ).stdout
    return [tuple(line.split("|", 1)) for line in out.splitlines() if line]


def test_env_cache_parser_handles_multicast_source(tmp_path: Path) -> None:
    """bee1's cache uses source='multicast' (not 'mdns').  wspr-recorder's
    original port only accepted 'mdns', which yielded an empty cache on
    multicast-discovery hosts.  Our port must accept both."""
    cache = tmp_path / "env-cache.json"
    cache.write_text(json.dumps({"observations": [{
        "source": "multicast", "kind": "radiod", "ok": True,
        "endpoint": "bee1-status.local", "id": "bee1-rx888",
        "fields": {},
    }]}))
    out = _run_parser(cache)
    assert out == [("bee1-status.local", "bee1-rx888")]


def test_env_cache_parser_handles_mdns_source(tmp_path: Path) -> None:
    cache = tmp_path / "env-cache.json"
    cache.write_text(json.dumps({"observations": [{
        "source": "mdns", "kind": "radiod", "ok": True,
        "endpoint": "ax.local", "id": "ax-rx888",
        "fields": {"mdns_name": "AC0G @EM38ww B1 T3FD"},
    }]}))
    out = _run_parser(cache)
    assert out == [("ax.local", "AC0G @EM38ww B1 T3FD")]


def test_env_cache_parser_strips_port_from_endpoint(tmp_path: Path) -> None:
    cache = tmp_path / "env-cache.json"
    cache.write_text(json.dumps({"observations": [{
        "source": "mdns", "kind": "radiod", "ok": True,
        "endpoint": "h.local:5006", "fields": {},
    }]}))
    out = _run_parser(cache)
    assert out == [("h.local", "h.local")]


def test_env_cache_parser_skips_non_radiod_kinds(tmp_path: Path) -> None:
    cache = tmp_path / "env-cache.json"
    cache.write_text(json.dumps({"observations": [
        {"source": "mdns", "kind": "gpsdo", "ok": True, "endpoint": "g.local"},
        {"source": "ntp",  "kind": "time_source", "ok": True, "endpoint": "n:123"},
        {"source": "mdns", "kind": "radiod", "ok": True, "endpoint": "r.local"},
    ]}))
    out = _run_parser(cache)
    assert out == [("r.local", "r.local")]


def test_env_cache_parser_skips_failed_observations(tmp_path: Path) -> None:
    cache = tmp_path / "env-cache.json"
    cache.write_text(json.dumps({"observations": [
        {"source": "mdns", "kind": "radiod", "ok": False, "endpoint": "bad.local"},
        {"source": "mdns", "kind": "radiod", "ok": True,  "endpoint": "good.local"},
    ]}))
    out = _run_parser(cache)
    assert [endpoint for endpoint, _ in out] == ["good.local"]


def test_env_cache_parser_deduplicates_repeated_endpoints(tmp_path: Path) -> None:
    """If sigmond's discovery wrote two observations for the same radiod
    (e.g. both mdns and multicast saw bee1), the picker should show one
    menu row, not two."""
    cache = tmp_path / "env-cache.json"
    cache.write_text(json.dumps({"observations": [
        {"source": "mdns",      "kind": "radiod", "ok": True, "endpoint": "bee1.local"},
        {"source": "multicast", "kind": "radiod", "ok": True, "endpoint": "bee1.local"},
    ]}))
    out = _run_parser(cache)
    assert len(out) == 1


def test_env_cache_parser_returns_empty_on_missing_or_invalid(tmp_path: Path) -> None:
    assert _run_parser(tmp_path / "absent.json")        == []
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert _run_parser(bad)                              == []


def test_env_cache_parser_returns_empty_on_no_observations(tmp_path: Path) -> None:
    cache = tmp_path / "env-cache.json"
    cache.write_text(json.dumps({"observations": []}))
    assert _run_parser(cache) == []


# ---------- env-file serializer / parser ----------------------------------

def test_serialize_env_file_quotes_values_with_whitespace() -> None:
    text = configurator._serialize_env_file({"K": "value with spaces"})
    assert text == 'K="value with spaces"\n'


def test_serialize_env_file_quotes_values_with_equals() -> None:
    text = configurator._serialize_env_file({"K": "a=b"})
    assert text == 'K="a=b"\n'


def test_parse_env_file_strips_quotes() -> None:
    f = Path("/tmp/psk-env-parse-test.env")
    f.write_text('K1="quoted"\nK2=\'single\'\nK3=plain\n')
    try:
        parsed = configurator._parse_env_file(f)
        assert parsed == {"K1": "quoted", "K2": "single", "K3": "plain"}
    finally:
        f.unlink()


def test_parse_env_file_skips_comments_and_blanks() -> None:
    f = Path("/tmp/psk-env-parse-test2.env")
    f.write_text('# this is a comment\n\n\nKEY=value\n# trailing comment\n')
    try:
        parsed = configurator._parse_env_file(f)
        assert parsed == {"KEY": "value"}
    finally:
        f.unlink()
