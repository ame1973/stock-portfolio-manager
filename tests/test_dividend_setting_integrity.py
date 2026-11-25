"""Keep shared recorded-setting comparisons exact and fail closed."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import Field

import spm_api.dividend_setting_integrity as integrity
from spm_api.errors import Model


class ScalarSetting(Model):
    identifier: str
    version: int = Field(strict=True)
    created_at: datetime


class NestedSetting(Model):
    values: list[int]


def sample() -> ScalarSetting:
    return ScalarSetting(
        identifier=str(uuid4()), version=1, created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )


def test_exact_setting_accepts_same_scalar_record_without_exposing_values() -> None:
    saved = sample()
    actual = saved.model_copy()
    assert integrity.exact_record(ScalarSetting, saved, actual) is actual


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "wrong_type",
        "float",
        "bool",
        "extra",
        "saved_float",
        "saved_wrong",
        "saved_extra",
    ],
)
def test_exact_setting_rejects_forged_or_equal_numeric_values(problem: str) -> None:
    saved: Any = sample()
    actual: Any = saved.model_copy()
    if problem == "missing":
        actual = None
    elif problem == "wrong_type":
        actual = "private"
    elif problem == "float":
        actual = actual.model_copy(update={"version": 1.0})
    elif problem == "bool":
        actual = actual.model_copy(update={"version": True})
    elif problem == "extra":
        actual = actual.model_copy(update={"unrecognized": "private"})
    elif problem == "saved_wrong":
        saved = "private"
    elif problem == "saved_extra":
        saved = saved.model_copy(update={"unrecognized": "private"})
    else:
        saved = saved.model_copy(update={"version": 1.0})
        actual = saved
    with pytest.raises(integrity.SettingIntegrityError, match="^HISTORY_UNAVAILABLE$"):
        integrity.exact_record(ScalarSetting, saved, actual)


def test_nested_setting_fields_are_not_silently_treated_as_type_exact() -> None:
    value = NestedSetting(values=[1])
    with pytest.raises(integrity.SettingIntegrityError, match="^HISTORY_UNAVAILABLE$"):
        integrity.exact_record(NestedSetting, value, value)
