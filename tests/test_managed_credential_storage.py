"""Managed credential storage is explicit, separate and never an activation or runtime fallback."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr
from sqlalchemy import Engine

from spm_api.config import ROOT, Settings
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.credentials import CredentialError, managed_artifacts
from tests.helpers import database_snapshot
from tests.test_provider_persistence import book as book


@pytest.mark.parametrize("value", [None, ""])
def test_optional_managed_directory_never_defaults_to_operator_or_data_storage(
    settings: Settings, value: Any
) -> None:
    configured = Settings(**{**settings.model_dump(), "managed_provider_secret_dir": value})
    assert configured.managed_provider_secret_dir is None
    before = (
        settings.data_dir.exists(),
        settings.backup_dir.exists(),
        settings.provider_secret_dir.exists(),
    )
    with pytest.raises(CredentialError, match="^MANAGED_CREDENTIAL_STORAGE_NOT_CONFIGURED$"):
        managed_artifacts(configured)
    assert (
        settings.data_dir.exists(),
        settings.backup_dir.exists(),
        settings.provider_secret_dir.exists(),
    ) == before


def test_explicit_environment_path_is_private_and_has_no_creation_side_effect(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "explicit-managed-secrets"
    monkeypatch.setenv("MANAGED_PROVIDER_SECRET_DIR", str(root))
    local = Settings(**settings.model_dump(exclude={"managed_provider_secret_dir"}))
    assert local.managed_provider_secret_dir == root and str(root) not in repr(local)
    assert not root.exists() and not settings.data_dir.exists()


def configured(settings: Settings, path: Path, operator: Path) -> Settings:
    return settings.model_copy(
        update={"managed_provider_secret_dir": path, "provider_secret_dir": operator}
    )


def test_explicit_managed_directory_is_not_created_by_settings_or_factory(
    settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "managed-secrets"
    local = configured(settings, root, tmp_path / "operator-secrets")
    assert str(root) not in repr(local)
    repository = managed_artifacts(local)
    assert not root.exists()
    with pytest.raises(CredentialError, match="^CREDENTIAL_PREPARATION_FAILED$"):
        repository.prepare("eodhd", SecretStr("synthetic-managed-storage-not-active"))
    assert not root.exists() and not settings.data_dir.exists()


def test_explicit_private_root_reuses_immutable_preparation_without_operator_adoption(
    settings: Settings, tmp_path: Path
) -> None:
    root, operator = tmp_path / "managed-secrets", tmp_path / "operator-secrets"
    root.mkdir(mode=0o700)
    local = configured(settings, root, operator)
    repository = managed_artifacts(local)
    before = ProviderConfigurationStore(local).load("eodhd")
    prepared = repository.prepare("eodhd", SecretStr("synthetic-managed-storage-not-active"))
    assert (
        repository.unchanged(prepared)
        and managed_artifacts(local).load(prepared.reference) == prepared
    )
    assert ProviderConfigurationStore(local).load("eodhd") == before
    assert not operator.exists() and not (settings.data_dir / "provider-config").exists()


@pytest.mark.parametrize(
    "where",
    [
        "operator",
        "operator-child",
        "operator-parent",
        "data",
        "data-child",
        "backup",
        "backup-child",
    ],
)
def test_managed_root_cannot_share_operator_data_or_backup_trees(
    settings: Settings, tmp_path: Path, where: str
) -> None:
    operator = tmp_path / "private-operator" / "keys"
    root = {
        "operator": operator,
        "operator-child": operator / "managed",
        "operator-parent": operator.parent,
        "data": settings.data_dir,
        "data-child": settings.data_dir / "keys",
        "backup": settings.backup_dir,
        "backup-child": settings.backup_dir / "keys",
    }[where]
    before = root.exists()
    with pytest.raises(CredentialError) as error:
        managed_artifacts(configured(settings, root, operator))
    assert str(root) not in str(error.value) and root.exists() == before


def test_operator_symlink_alias_is_not_a_separate_managed_store(
    settings: Settings, tmp_path: Path
) -> None:
    root, alias = tmp_path / "managed-secrets", tmp_path / "operator-alias"
    root.mkdir(mode=0o700)
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(CredentialError, match="^UNSAFE_MANAGED_CREDENTIAL_LOCATION$"):
        managed_artifacts(configured(settings, root, alias))
    assert list(root.iterdir()) == []


def test_managed_symlink_is_never_followed_to_write_an_edition(
    settings: Settings, tmp_path: Path
) -> None:
    root, alias = tmp_path / "private-target", tmp_path / "managed-alias"
    root.mkdir(mode=0o700)
    alias.symlink_to(root, target_is_directory=True)
    repository = managed_artifacts(configured(settings, alias, tmp_path / "operator-secrets"))
    with pytest.raises(CredentialError):
        repository.prepare("eodhd", SecretStr("synthetic-managed-storage-not-active"))
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("error", [OSError, RuntimeError, ValueError])
def test_unresolvable_managed_location_has_only_a_fixed_safe_code(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error("private-path-must-not-be-echoed")

    monkeypatch.setattr(Path, "resolve", fail)
    with pytest.raises(CredentialError, match="^UNSAFE_MANAGED_CREDENTIAL_LOCATION$"):
        managed_artifacts(configured(settings, tmp_path / "managed", tmp_path / "operator"))


def test_compose_keeps_operator_read_only_and_separates_managed_writer_from_worker() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    api, worker = (compose["services"][name] for name in ("api", "worker"))
    target = "/run/spm-managed-provider-secrets"
    assert api["environment"]["MANAGED_PROVIDER_SECRET_DIR"] == target
    assert worker["environment"]["MANAGED_PROVIDER_SECRET_DIR"] == target
    assert f"managed_provider_secrets:{target}" in api["volumes"]
    assert f"managed_provider_secrets:{target}:ro" in worker["volumes"]
    for service in (api, worker):
        assert "provider_secrets:/run/spm-provider-secrets:ro" in service["volumes"]
        assert service["read_only"] is True and service["cap_drop"] == ["ALL"]
    assert "managed_provider_secrets" in compose["volumes"]
    assert "/managed-secrets/" in (ROOT / ".gitignore").read_text()
    assert "managed-secrets" in (ROOT / ".dockerignore").read_text().splitlines()


@pytest.mark.anyio
async def test_managed_storage_preparation_leaves_actual_financial_source_and_operator_facts(
    book: dict[str, Any], engine: Engine, tmp_path: Path
) -> None:
    root = tmp_path / "managed-credential-editions"
    root.mkdir(mode=0o700)
    settings = book["settings"].model_copy(update={"managed_provider_secret_dir": root})
    operator = ProviderConfigurationStore(settings)
    observed = operator.load("eodhd")
    before = database_snapshot(engine)
    prepared = managed_artifacts(settings).prepare(
        "eodhd", SecretStr("synthetic-managed-storage-not-active")
    )
    assert managed_artifacts(settings).unchanged(prepared)
    assert operator.unchanged(observed) and database_snapshot(engine) == before
