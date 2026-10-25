"""The unit's sandbox must leave jt9 a writable /tmp (v3.82 row 4)."""
from pathlib import Path

UNIT = Path(__file__).resolve().parents[1] / "systemd" / "psk-recorder@.service"


def _settings():
    """[(key, value)] for non-comment 'Key=Value' lines in [Service]."""
    out, in_service = [], False
    for raw in UNIT.read_text().splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_service = line == "[Service]"
        elif in_service and line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out.append((key.strip(), value.strip()))
    return out


def test_jt9_gets_a_writable_private_tmp():
    s = _settings()
    assert ("PrivateTmp", "true") in s and ("PrivateTmp", "false") not in s


def test_the_sandbox_stays_strict():
    assert ("ProtectSystem", "strict") in _settings()
