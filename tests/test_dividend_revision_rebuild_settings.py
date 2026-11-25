"""Require explicit per-account setting decisions for any future revision rebuild."""

from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

import spm_api.dividend_revision_rebuild as rebuild
import spm_api.dividend_revision_rebuild_intent as intent
from tests.test_dividend_revision_rebuild_intent import inputs


def settings_wire(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "confirmation": "EXPLICIT_SUCCESSOR_SETTINGS_ONLY",
        "accounts": [
            {
                "account_id": str(account.account_id),
                "tax": {
                    "mode": "RECORD_SUPPLIED",
                    "effective_from": "2026-01-01",
                    "select_on": "2026-01-08",
                },
                "fee": {
                    "mode": "RECORD_SUPPLIED",
                    "effective_from": "2026-01-02",
                    "select_on": "2026-01-09",
                },
            }
            for account in value["proposal"].accounts
        ],
    }


def hashed(value: dict[str, Any], settings: dict[str, Any]) -> str:
    return intent.fingerprint(**value, settings=intent.RebuildSettings.model_validate(settings))


def test_setting_decisions_are_explicit_canonical_and_account_order_independent() -> None:
    value = inputs()
    supplied = settings_wire(value)
    alternative = deepcopy(supplied)
    alternative["accounts"].reverse()
    for account in alternative["accounts"]:
        account["account_id"] = account["account_id"].upper()
    assert hashed(value, supplied) == hashed(value, alternative)
    assert len(hashed(value, supplied)) == 64


@pytest.mark.parametrize("field", ["effective_from", "select_on"])
@pytest.mark.parametrize("setting", ["tax", "fee"])
def test_effective_and_selection_dates_are_independently_bound(setting: str, field: str) -> None:
    value = inputs()
    supplied = settings_wire(value)
    changed = deepcopy(supplied)
    changed["accounts"][0][setting][field] = (
        "2026-01-03" if field == "effective_from" else "2026-01-10"
    )
    assert hashed(value, supplied) != hashed(value, changed)


@pytest.mark.parametrize("setting", ["tax", "fee"])
def test_explicit_unknown_estimate_differs_from_no_new_setting(setting: str) -> None:
    value = inputs()
    proposal = value["proposal"].model_dump(mode="python")
    accounts = [dict(account) for account in proposal["accounts"]]
    accounts[0]["tax_rate" if setting == "tax" else "fee"] = None
    value["proposal"] = type(value["proposal"]).model_validate({**proposal, "accounts": accounts})
    supplied = settings_wire(value)
    missing = deepcopy(supplied)
    missing["accounts"][0][setting].update(mode="LEAVE_MISSING", effective_from=None)
    assert hashed(value, supplied) != hashed(value, missing)


@pytest.mark.parametrize(
    "problem",
    [
        "confirmation_missing",
        "confirmation_invalid",
        "account_missing",
        "account_duplicate",
        "account_foreign",
        "tax_missing",
        "mode_missing",
        "mode_invalid",
        "effective_missing",
        "effective_null",
        "selection_missing",
        "selection_invalid",
        "after_selection",
        "missing_with_effective",
        "extra_field",
    ],
)
@pytest.mark.parametrize("setting_name", ["tax", "fee"])
def test_malformed_or_incomplete_setting_decisions_fail_closed(
    problem: str, setting_name: str
) -> None:
    value = inputs()
    supplied = settings_wire(value)
    account = supplied["accounts"][0]
    setting = account[setting_name]
    if problem == "confirmation_missing":
        del supplied["confirmation"]
    elif problem == "confirmation_invalid":
        supplied["confirmation"] = "ACCEPT_ALL"
    elif problem == "account_missing":
        supplied["accounts"].pop()
    elif problem == "account_duplicate":
        duplicate = deepcopy(account)
        duplicate["account_id"] = duplicate["account_id"].upper()
        supplied["accounts"].append(duplicate)
    elif problem == "account_foreign":
        account["account_id"] = str(uuid4())
    elif problem == "tax_missing":
        del account[setting_name]
    elif problem == "mode_missing":
        del setting["mode"]
    elif problem == "mode_invalid":
        setting["mode"] = "INHERIT"
    elif problem == "effective_missing":
        del setting["effective_from"]
    elif problem == "effective_null":
        setting["effective_from"] = None
    elif problem == "selection_missing":
        del setting["select_on"]
    elif problem == "selection_invalid":
        setting["select_on"] = "2026-02-30"
    elif problem == "after_selection":
        setting["effective_from"] = "2026-01-10"
    elif problem == "missing_with_effective":
        setting["mode"] = "LEAVE_MISSING"
    else:
        setting["unrecognized"] = "private"
    with pytest.raises((ValidationError, rebuild.RebuildError)):
        hashed(value, supplied)


def test_account_specific_dates_cannot_be_swapped_without_changing_intent() -> None:
    value = inputs()
    supplied = settings_wire(value)
    supplied["accounts"][0]["tax"]["effective_from"] = "2025-12-31"
    swapped = deepcopy(supplied)
    first, second = swapped["accounts"]
    first["tax"], second["tax"] = second["tax"], first["tax"]
    assert hashed(value, supplied) != hashed(value, swapped)


def test_setting_decisions_are_required_for_fingerprint() -> None:
    with pytest.raises(TypeError):
        intent.fingerprint(**inputs())


def test_empty_explicit_scope_and_same_day_selection_are_valid() -> None:
    value = inputs()
    proposal = value["proposal"].model_dump(mode="python")
    value["proposal"] = type(value["proposal"]).model_validate({**proposal, "accounts": ()})
    assert len(hashed(value, settings_wire(value))) == 64

    value = inputs()
    supplied = settings_wire(value)
    for account in supplied["accounts"]:
        for name in ("tax", "fee"):
            account[name]["effective_from"] = account[name]["select_on"]
    assert len(hashed(value, supplied)) == 64


@pytest.mark.parametrize("setting", ["tax", "fee"])
def test_missing_setting_needs_explicit_selection_day_and_null_effective_date(setting: str) -> None:
    value = inputs()
    supplied = settings_wire(value)
    supplied["accounts"][0][setting].update(mode="LEAVE_MISSING", effective_from=None)
    missing = deepcopy(supplied)
    del missing["accounts"][0][setting]["effective_from"]
    with pytest.raises(ValidationError):
        hashed(value, missing)
    changed = deepcopy(supplied)
    changed["accounts"][0][setting]["select_on"] = "2026-01-10"
    assert hashed(value, supplied) != hashed(value, changed)


@pytest.mark.parametrize("bad", [None, True, 0, "2026-1-01", "20260201", "2026-02-30"])
@pytest.mark.parametrize("setting", ["tax", "fee"])
@pytest.mark.parametrize("field", ["effective_from", "select_on"])
def test_dates_require_explicit_canonical_calendar_strings(
    bad: object, setting: str, field: str
) -> None:
    value = inputs()
    supplied = settings_wire(value)
    supplied["accounts"][0][setting][field] = bad
    with pytest.raises(ValidationError):
        hashed(value, supplied)


def test_setting_models_and_errors_withhold_private_inputs() -> None:
    value = inputs()
    supplied = settings_wire(value)
    private_id = supplied["accounts"][0]["account_id"]
    private_date = supplied["accounts"][0]["tax"]["effective_from"]
    settings = intent.RebuildSettings.model_validate(supplied)
    assert private_id not in repr(settings)
    assert private_id not in repr(settings.accounts[0])
    assert private_date not in repr(settings.accounts[0].tax)
    supplied["accounts"][0]["tax"]["mode"] = "private-invalid-mode"
    with pytest.raises(ValidationError) as error:
        intent.RebuildSettings.model_validate(supplied)
    assert "private-invalid-mode" not in str(error.value)


@pytest.mark.parametrize("settings", [None, True, {}, "private"])
def test_fingerprint_rejects_wrong_setting_model_without_disclosure(settings: Any) -> None:
    with pytest.raises(rebuild.RebuildError, match="^INVALID_DIVIDEND_REBUILD_INTENT$"):
        intent.fingerprint(**inputs(), settings=settings)


def test_forged_nested_setting_instances_cannot_be_bound() -> None:
    value = inputs()
    settings = intent.RebuildSettings.model_validate(settings_wire(value))
    account = settings.accounts[0]
    for forged in (
        settings.model_copy(update={"unrecognized": "private"}),
        settings.model_copy(
            update={
                "accounts": (
                    account.model_copy(update={"unrecognized": "private"}),
                    *settings.accounts[1:],
                )
            }
        ),
        settings.model_copy(update={"accounts": list(settings.accounts)}),
        settings.model_copy(
            update={
                "accounts": (
                    account.model_copy(update={"tax": account.tax.model_copy(update={"mode": 1})}),
                    *settings.accounts[1:],
                )
            }
        ),
    ):
        with pytest.raises(rebuild.RebuildError, match="^INVALID_DIVIDEND_REBUILD_INTENT$"):
            intent.fingerprint(**value, settings=forged)
