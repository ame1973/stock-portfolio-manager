"""The shared engine fixture must never open or migrate non-temporary data."""

from pathlib import Path

import pytest

from spm_api.config import Settings
from tests.conftest import _check_isolated_database


def test_fixture_accepts_an_empty_database_under_its_pytest_root(
    settings: Settings, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    _check_isolated_database(settings, tmp_path, tmp_path_factory.getbasetemp())
    assert not settings.data_dir.exists()


@pytest.mark.parametrize(
    "failure", ["environment", "data", "backups", "base", "existing", "data_symlink", "db_symlink"]
)
def test_fixture_rejects_foreign_or_existing_database_before_migration(
    settings: Settings, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, failure: str
) -> None:
    base = tmp_path_factory.getbasetemp().resolve(strict=True)
    root = tmp_path.resolve(strict=True)
    assert root != base and root.is_relative_to(base)
    assert settings.data_dir.resolve() == root / "data"
    assert settings.database_path.resolve() == root / "data" / "portfolio.sqlite3"
    safe_data = root / "data"
    safe_db = safe_data / "portfolio.sqlite3"
    assert not safe_data.exists() and not safe_db.exists()
    if failure == "environment":
        settings = settings.model_copy(update={"app_env": "production"})
    elif failure == "data":
        settings = settings.model_copy(update={"data_dir": tmp_path.parent / "foreign-data"})
    elif failure == "backups":
        settings = settings.model_copy(update={"backup_dir": tmp_path.parent / "foreign-backups"})
    elif failure == "base":
        base = tmp_path
    elif failure == "data_symlink":
        target = root / "synthetic-other-data"
        target.mkdir()
        safe_data.symlink_to(target, target_is_directory=True)
    elif failure == "db_symlink":
        safe_data.mkdir()
        target = root / "synthetic-other.sqlite3"
        with target.open("xb") as stream:
            stream.write(b"synthetic existing database; do not modify")
        safe_db.symlink_to(target)
    else:
        safe_data.mkdir()
        with safe_db.open("xb") as stream:
            stream.write(b"synthetic existing database; do not modify")
    before = safe_db.read_bytes() if failure in {"existing", "db_symlink"} else None
    with pytest.raises(RuntimeError, match="^TEST_DATABASE_SCOPE_REJECTED$"):
        _check_isolated_database(settings, tmp_path, base)
    if before is not None:
        assert safe_db.read_bytes() == before
