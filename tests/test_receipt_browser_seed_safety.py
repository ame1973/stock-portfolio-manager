"""Synthetic receipt browser seeding rejects unsafe roots without opening a database."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.parametrize(
    "case", ["environment", "root_name", "temp_parent", "data_dir", "backup_dir", "symlink"]
)
@pytest.mark.parametrize(
    ("script_name", "error"),
    [
        ("seed_receipts.py", b"SYNTHETIC_RECEIPT_FIXTURE_REQUIRED"),
        ("seed_cross_reconciliation.py", b"SYNTHETIC_RECONCILIATION_FIXTURE_REQUIRED"),
    ],
)
def test_receipt_browser_seed_rejects_nonfixture_scope(
    tmp_path: Path, case: str, script_name: str, error: bytes
) -> None:
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
        "SPM_E2E_COMPOSE_PROJECT": "spm-e2e-credential-browser-fixture",
        "DATA_DIR": str(other if case == "data_dir" else data),
        "BACKUP_DIR": str(other if case == "backup_dir" else root / "backups"),
        "TMPDIR": str(other if case == "temp_parent" else tmp_path),
    }
    script = Path(__file__).parent / "e2e" / "dividend-events" / script_name
    result = subprocess.run(
        [sys.executable, "-I", str(script), *(str(uuid4()) for _ in range(4))],
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode != 0
    assert error in result.stderr
    assert protected.read_bytes() == b"synthetic database sentinel"
    assert database.read_bytes() == (
        b"synthetic database sentinel" if case == "symlink" else b"synthetic fixture sentinel"
    )


@pytest.mark.parametrize("case", ["project", "root_symlink"])
def test_cross_reconciliation_seed_rejects_wrong_fixture_identity(
    tmp_path: Path, case: str
) -> None:
    actual = tmp_path / "spm-credential-browser-actual"
    data = actual / "data"
    data.mkdir(parents=True)
    database = data / "portfolio.sqlite3"
    database.write_bytes(b"synthetic fixture sentinel")
    root = actual
    if case == "root_symlink":
        root = tmp_path / "spm-credential-browser-link"
        root.symlink_to(actual, target_is_directory=True)
    env = {
        **os.environ,
        "APP_ENV": "test",
        "SPM_E2E_CREDENTIAL_ROOT": str(root),
        "SPM_E2E_COMPOSE_PROJECT": (
            "not-the-fixture" if case == "project" else "spm-e2e-credential-browser-fixture"
        ),
        "DATA_DIR": str(root / "data"),
        "BACKUP_DIR": str(root / "backups"),
        "TMPDIR": str(tmp_path),
    }
    script = Path(__file__).parent / "e2e" / "dividend-events" / "seed_cross_reconciliation.py"
    result = subprocess.run(
        [sys.executable, "-I", str(script), *(str(uuid4()) for _ in range(4))],
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode != 0
    assert b"SYNTHETIC_RECONCILIATION_FIXTURE_REQUIRED" in result.stderr
    assert database.read_bytes() == b"synthetic fixture sentinel"
