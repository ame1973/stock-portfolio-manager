"""Synthetic receipt browser seeding rejects unsafe roots without opening a database."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.parametrize("case", ["environment", "root_name", "temp_parent", "data_dir", "symlink"])
def test_receipt_browser_seed_rejects_nonfixture_scope(tmp_path: Path, case: str) -> None:
    root = tmp_path / ("not-a-fixture" if case == "root_name" else "spm-credential-browser-check")
    data = root / "data"
    data.mkdir(parents=True)
    database = data / "portfolio.sqlite3"
    protected = tmp_path / "unrelated.sqlite3"
    protected.write_bytes(b"synthetic database sentinel")
    if case == "symlink":
        database.symlink_to(protected)
    else:
        database.write_bytes(b"synthetic fixture sentinel")
    other = tmp_path / "other"
    other.mkdir()
    env = {
        **os.environ,
        "APP_ENV": "production" if case == "environment" else "test",
        "SPM_E2E_CREDENTIAL_ROOT": str(root),
        "DATA_DIR": str(other if case == "data_dir" else data),
        "BACKUP_DIR": str(root / "backups"),
        "TMPDIR": str(other if case == "temp_parent" else tmp_path),
    }
    script = Path(__file__).parent / "e2e" / "dividend-events" / "seed_receipts.py"
    result = subprocess.run(
        [sys.executable, "-I", str(script), *(str(uuid4()) for _ in range(4))],
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode != 0
    assert b"SYNTHETIC_RECEIPT_FIXTURE_REQUIRED" in result.stderr
    assert protected.read_bytes() == b"synthetic database sentinel"
    assert database.read_bytes() == (
        b"synthetic database sentinel" if case == "symlink" else b"synthetic fixture sentinel"
    )
