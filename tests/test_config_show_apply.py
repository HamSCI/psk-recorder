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


def test_apply_rejects_radiod_writes(tmp_path: Path, capsys) -> None:
    """The wizard is not allowed to set [[radiod]] blocks."""
    rv = _apply({"radiod": [{"id": "bogus"}]}, tmp_path)
    assert rv == 2
    assert "not writable via apply" in capsys.readouterr().err.lower()


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
