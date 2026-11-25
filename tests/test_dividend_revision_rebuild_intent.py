"""Bind a future rebuild's exact normalized intent without granting a revision."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

import spm_api.dividend_revision_rebuild as rebuild
import spm_api.dividend_revision_rebuild_intent as intent
from tests.test_dividend_event_writer import manual, terms
from tests.test_dividend_revision_preview import proposal


def inputs() -> dict[str, Any]:
    security, event, previous, owner = (uuid4() for _ in range(4))
    accounts = {name: {"id": str(uuid4())} for name in ("source", "target")}
    decision = manual(
        event_id=event,
        revision_id=uuid4(),
        revision_no=2,
        terms=terms(security_id=security, dps="1.1", currency="USD"),
    )
    return {
        "owner": owner,
        "previous_revision_id": previous,
        "previous_revision_no": 1,
        "decision": decision,
        "proposal": proposal(accounts),
        "request_id": uuid4(),
        "at": datetime(2026, 11, 25, 4, 3, 2, 123456, tzinfo=UTC),
    }


def fingerprint(value: dict[str, Any]) -> str:
    settings = intent.RebuildSettings.model_validate(
        {
            "confirmation": "EXPLICIT_SUCCESSOR_SETTINGS_ONLY",
            "accounts": [
                {
                    "account_id": item.account_id,
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
                for item in value["proposal"].accounts
            ],
        }
    )
    return intent.fingerprint(**value, settings=settings)


def test_fingerprint_normalizes_decimal_spelling_account_order_and_utc() -> None:
    value = inputs()
    decision, proposed = value["decision"], value["proposal"]
    alternative = proposed.model_dump(mode="python")
    alternative["dps"] = "1.10000000"
    alternative["accounts"] = list(reversed(alternative["accounts"]))
    for account in alternative["accounts"]:
        account.update(tax_rate="0.000000", fee="0.000000", fx_rate_hkd="8.000000000000")
    equivalent = type(proposed).model_validate(alternative)
    revised = type(decision).model_validate(
        {
            **decision.model_dump(mode="python"),
            "terms": terms(
                security_id=decision.terms.security_id, dps="1.10000000", currency="USD"
            ),
        }
    )
    result = fingerprint(value)
    assert len(result) == 64 and all(char in "0123456789abcdef" for char in result)
    assert result == fingerprint(
        {
            **value,
            "decision": revised,
            "proposal": equivalent,
            "at": value["at"].astimezone(timezone(timedelta(hours=8))),
        }
    )
    assert str(value["owner"]) not in result and "1.10000000" not in result


@pytest.mark.parametrize("kind", ["VERIFY", "WITHDRAW", "CANDIDATE"])
def test_nonmanual_decisions_bind_identity_without_validating_sources(kind: str) -> None:
    value = inputs()
    manual_decision = value["decision"]
    decision = type(manual_decision).model_validate(
        {
            "event_id": manual_decision.event_id,
            "revision_id": manual_decision.revision_id,
            "revision_no": manual_decision.revision_no,
            "kind": kind,
            "candidate_id": uuid4() if kind == "CANDIDATE" else None,
            "candidate_version": 1 if kind == "CANDIDATE" else None,
            "citation": None if kind == "CANDIDATE" else "Synthetic owner reference",
            "reason": manual_decision.reason,
        }
    )
    assert fingerprint(value) != fingerprint({**value, "decision": decision})


@pytest.mark.parametrize("field", ["owner", "previous_revision_id", "request_id", "at", "decision"])
def test_distinct_identity_or_decision_changes_the_digest(field: str) -> None:
    value = inputs()
    if field == "at":
        changed: Any = value[field] + timedelta(microseconds=1)
    elif field == "decision":
        changed = value[field].model_copy(update={"reason": "A different synthetic reason."})
    else:
        changed = uuid4()
    assert fingerprint(value) != fingerprint({**value, field: changed})


@pytest.mark.parametrize("field", ["tax_rate", "fee", "fee_currency", "fx_rate_hkd", "fx_source"])
def test_independent_estimate_changes_are_bound(field: str) -> None:
    value = inputs()
    proposed = value["proposal"]
    wire = proposed.model_dump(mode="python")
    accounts = [dict(account) for account in wire["accounts"]]
    changes = {
        "tax_rate": "0.1",
        "fee": "1",
        "fee_currency": "HKD",
        "fx_rate_hkd": "8.1",
        "fx_source": "Another synthetic rate",
    }
    accounts[0][field] = changes[field]
    changed = type(proposed).model_validate({**wire, "accounts": accounts})
    assert fingerprint(value) != fingerprint({**value, "proposal": changed})


@pytest.mark.parametrize("field", ["tax_rate", "fee", "fx_rate_hkd"])
def test_unknown_is_distinct_from_an_explicit_estimate(field: str) -> None:
    value = inputs()
    proposed = value["proposal"]
    wire = proposed.model_dump(mode="python")
    accounts = [dict(account) for account in wire["accounts"]]
    accounts[0][field] = None
    if field == "fx_rate_hkd":
        accounts[0]["fx_source"] = None
    changed = type(proposed).model_validate({**wire, "accounts": accounts})
    assert fingerprint(value) != fingerprint({**value, "proposal": changed})


@pytest.mark.parametrize("field", ["as_of", "eligibility_rule", "account_id"])
def test_revision_scope_changes_are_bound(field: str) -> None:
    value = inputs()
    proposed = value["proposal"]
    wire = proposed.model_dump(mode="python")
    if field == "as_of":
        wire[field] = "2026-01-09"
    elif field == "eligibility_rule":
        wire[field] = "REQUIRES_REVIEW"
    else:
        accounts = [dict(account) for account in wire["accounts"]]
        accounts[0][field] = uuid4()
        wire["accounts"] = accounts
    changed = type(proposed).model_validate(wire)
    assert fingerprint(value) != fingerprint({**value, "proposal": changed})


def test_independent_foreign_fee_fx_and_rejection_pin_are_bound() -> None:
    value = inputs()
    proposed, decision = value["proposal"], value["decision"]
    wire = proposed.model_dump(mode="python")
    accounts = [dict(account) for account in wire["accounts"]]
    accounts[0].update(fee="1", fee_currency="HKD", fee_rate_event="0.13", fee_rate_source="One")
    changed = type(proposed).model_validate({**wire, "accounts": accounts})
    first = fingerprint({**value, "proposal": changed})
    accounts[0].update(fee_rate_event="0.14", fee_rate_source="Two")
    other = type(proposed).model_validate({**wire, "accounts": accounts})
    assert first != fingerprint({**value, "proposal": other})
    resolved = decision.model_copy(update={"resolve_rejection_id": uuid4()})
    assert fingerprint(value) != fingerprint({**value, "decision": resolved})


def test_successor_security_and_candidate_identity_are_bound_without_validating_sources() -> None:
    value = inputs()
    decision = value["decision"]
    changed_terms = terms(security_id=uuid4(), dps="1.1", currency="USD")
    changed_decision = decision.model_copy(update={"terms": changed_terms})
    assert fingerprint(value) != fingerprint({**value, "decision": changed_decision})
    candidate = type(decision).model_validate(
        {
            "event_id": decision.event_id,
            "revision_id": decision.revision_id,
            "revision_no": decision.revision_no,
            "kind": "CANDIDATE",
            "candidate_id": uuid4(),
            "candidate_version": 1,
            "reason": decision.reason,
        }
    )
    initial = fingerprint({**value, "decision": candidate})
    for changed in (
        candidate.model_copy(update={"candidate_id": uuid4()}),
        candidate.model_copy(update={"candidate_version": 2}),
    ):
        assert initial != fingerprint({**value, "decision": changed})


def test_maximum_supported_successor_version_is_bound() -> None:
    value = inputs()
    changed = value["decision"].model_copy(update={"revision_no": 2_147_483_647})
    result = fingerprint({**value, "previous_revision_no": 2_147_483_646, "decision": changed})
    assert len(result) == 64


@pytest.mark.parametrize(
    "changes",
    [
        {"owner": "not-a-uuid"},
        {"previous_revision_id": "not-a-uuid"},
        {"previous_revision_no": 0},
        {"previous_revision_no": True},
        {"previous_revision_no": 2_147_483_647},
        {"request_id": "not-a-uuid"},
        {"at": datetime(2026, 1, 1)},
        {"at": "2026-01-01T00:00:00Z"},
    ],
)
def test_invalid_scope_is_rejected_without_leaking_values(changes: dict[str, Any]) -> None:
    value = inputs()
    with pytest.raises(rebuild.RebuildError, match="^INVALID_DIVIDEND_REBUILD_INTENT$"):
        fingerprint({**value, **changes})


def test_mismatched_successor_or_manual_terms_are_rejected() -> None:
    value = inputs()
    decision = value["decision"]
    for changed in (
        {"previous_revision_id": decision.revision_id},
        {"previous_revision_no": 2},
        {
            "proposal": proposal(
                {"source": {"id": str(uuid4())}, "target": {"id": str(uuid4())}}, dps="1.2"
            )
        },
    ):
        with pytest.raises(rebuild.RebuildError, match="^INVALID_DIVIDEND_REBUILD_INTENT$"):
            fingerprint({**value, **changed})


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("ex_date", "2026-01-09"),
        ("dps", "1.2"),
        ("currency", "HKD"),
        ("adjustment_basis", "UNKNOWN"),
    ],
)
def test_manual_decision_and_proposal_must_match(field: str, replacement: str) -> None:
    value = inputs()
    decision = value["decision"]
    changed_terms = type(decision.terms).model_validate(
        {**decision.terms.model_dump(mode="python"), field: replacement}
    )
    changed = decision.model_copy(update={"terms": changed_terms})
    with pytest.raises(rebuild.RebuildError, match="^INVALID_DIVIDEND_REBUILD_INTENT$"):
        fingerprint({**value, "decision": changed})


def test_forged_model_values_and_undeclared_fields_fail_closed() -> None:
    value = inputs()
    decision, proposed = value["decision"], value["proposal"]
    forged = [
        {"decision": decision.model_copy(update={"revision_no": True})},
        {"decision": decision.model_copy(update={"unrecognized": "private"})},
        {"proposal": proposed.model_copy(update={"dps": 1.1})},
        {"proposal": proposed.model_copy(update={"unrecognized": "private"})},
        {
            "proposal": proposed.model_copy(
                update={
                    "accounts": (
                        proposed.accounts[0].model_copy(update={"unrecognized": "private"}),
                        proposed.accounts[1],
                    )
                }
            )
        },
    ]
    for changed in forged:
        with pytest.raises(rebuild.RebuildError, match="^INVALID_DIVIDEND_REBUILD_INTENT$"):
            fingerprint({**value, **changed})
