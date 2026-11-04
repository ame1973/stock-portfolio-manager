"""Worker leadership cannot be held twice and is recovered after release."""

from pathlib import Path
from unittest.mock import patch

from spm_worker.main import healthy, heartbeat, leader_lock


def test_single_leader_and_release(tmp_path: Path) -> None:
    with leader_lock(tmp_path) as first:
        assert first
        with leader_lock(tmp_path) as second:
            assert not second
    with leader_lock(tmp_path) as successor:
        assert successor


def test_heartbeat_fresh_stale_and_clock_reversal(tmp_path: Path) -> None:
    assert not healthy(tmp_path)
    with patch("spm_worker.main.time.time", return_value=100):
        heartbeat(tmp_path, "synthetic-leader")
        assert healthy(tmp_path)
    with patch("spm_worker.main.time.time", return_value=131):
        assert not healthy(tmp_path)
    with patch("spm_worker.main.time.time", return_value=99):
        assert not healthy(tmp_path)
    (tmp_path / ".worker-heartbeat.json").write_text("invalid")
    assert not healthy(tmp_path)
