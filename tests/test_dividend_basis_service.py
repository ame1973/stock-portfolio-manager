"""Internal basis declarations are audited, owned and immutable, never financial adoption."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

import spm_api.dividend_basis_service as service
from spm_api.audit import canonical, digest, now, stamp
from spm_api.db import unit_of_work
from spm_api.dividend_basis import SplitBasisAssertion, SplitReference, normalize_basis
from spm_api.errors import ApiError
from spm_api.observation_service import original_dividend
from spm_api.providers.base import EvidenceCitation
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from tests.helpers import create, database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import action
from tests.test_dividend_basis_migration import SECURITY, TABLE, cloned_source, seeded
from tests.test_provider_persistence import book as book
from tests.test_provider_persistence import fetched
from tests.test_provider_persistence import save as capture_source

OWNER = UUID("c0000000-0000-4000-8000-000000000001")
OTHER = UUID("c0000000-0000-4000-8000-000000000002")
AT = datetime(2026, 11, 8, tzinfo=UTC)


@pytest.fixture
def basis(engine: Engine) -> SplitBasisAssertion:
    with unit_of_work(engine, write=True) as con:
        values = seeded(con)
        for owner in (OWNER, OTHER):
            con.exec_driver_sql(
                "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
                "VALUES (?,'synthetic-internal-actor-not-http-auth',?,?)",
                (str(owner), stamp(AT), stamp(AT)),
            )
    return SplitBasisAssertion.model_validate_json(values["assertion_json"])


def save(engine: Engine, assertion: SplitBasisAssertion, **changes: Any) -> Any:
    arguments = {"owner_id": OWNER, "at": AT, "request_id": uuid4(), **changes}
    with unit_of_work(engine, write=True) as con:
        return service.save(con, assertion=assertion, **arguments)


def read(engine: Engine, identifier: UUID, owner: UUID = OWNER) -> Any:
    with unit_of_work(engine) as con:
        return service.get(con, owner, identifier)


def current(engine: Engine, identifier: UUID, owner: UUID = OWNER) -> Any:
    with unit_of_work(engine, write=True) as con:
        return service.require_current(con, owner, identifier)


def test_exact_declared_snapshot_has_one_correlated_audit_and_no_other_effects(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    before = database_snapshot(engine)
    request = uuid4()
    result = save(engine, basis, request_id=request)
    assert result.id == basis.id and result.owner_id == OWNER
    assert result.assertion == basis and result.created_at == AT and result.request_id == request
    assert read(engine, basis.id) == result and current(engine, basis.id) == result
    after = database_snapshot(engine)
    assert {name: rows for name, rows in before.items() if name not in {TABLE, "audit_events"}} == {
        name: rows for name, rows in after.items() if name not in {TABLE, "audit_events"}
    }
    assert len(after[TABLE]) == 1 and len(after["audit_events"]) == len(before["audit_events"]) + 1
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(f"SELECT * FROM {TABLE}").mappings().one()
        assert row["assertion_json"] == canonical(basis.model_dump(mode="json"))
        assert row["assertion_sha256"] == digest(row["assertion_json"])
        audit = con.exec_driver_sql(
            "SELECT owner_id,entity_type,entity_id,action,reason,request_id,before_sha256 "
            "FROM audit_events WHERE action='DIVIDEND_BASIS_ASSERTED'"
        ).one()
        assert tuple(audit) == (
            str(OWNER),
            "dividend_basis_assertion",
            str(basis.id),
            "DIVIDEND_BASIS_ASSERTED",
            basis.reason,
            str(request),
            None,
        )


def test_same_internal_artifact_replay_is_zero_write_even_after_action_revision(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    request = uuid4()
    first = save(engine, basis, request_id=request)
    reference = basis.splits[0]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET is_current=0,version=version+1 WHERE id=?",
            (str(reference.id),),
        )
        action(
            con,
            str(uuid4()),
            root_id=str(reference.root_id),
            supersedes_id=str(reference.id),
            security_id=str(SECURITY),
            revision=2,
            effective_date=reference.effective_date,
            ratio_num=2,
            ratio_den=1,
        )
    before = database_snapshot(engine)
    assert read(engine, basis.id) == first
    assert save(engine, basis, request_id=request) == first
    with pytest.raises(service.BasisError, match="^BASIS_ACTIONS_CHANGED$"):
        current(engine, basis.id)
    assert database_snapshot(engine) == before


def test_reader_is_owner_scoped_and_missing_is_not_an_empty_proof(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    save(engine, basis)
    before = database_snapshot(engine)
    assert read(engine, basis.id, OTHER) is None and read(engine, uuid4()) is None
    with pytest.raises(service.BasisError, match="^BASIS_ASSERTION_NOT_FOUND$"):
        current(engine, basis.id, OTHER)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("changed", ["request", "clock", "terms", "owner"])
def test_reusing_a_server_assertion_identity_cannot_reattribute_or_change_history(
    engine: Engine, basis: SplitBasisAssertion, changed: str
) -> None:
    request = uuid4()
    save(engine, basis, request_id=request)
    before = database_snapshot(engine)
    options: dict[str, Any] = {"request_id": request}
    body = basis
    if changed == "request":
        options["request_id"] = uuid4()
    elif changed == "clock":
        options["at"] = AT + timedelta(microseconds=1)
    elif changed == "owner":
        options["owner_id"] = OTHER
    else:
        body = basis.model_copy(update={"reason": "A distinct owner statement, never a rewrite."})
    with pytest.raises(service.BasisError, match="^BASIS_ASSERTION_CONFLICT$"):
        save(engine, body, **options)
    assert database_snapshot(engine) == before


def test_late_audit_failure_rolls_back_the_declaration_and_every_unrelated_table(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("""
            CREATE TRIGGER test_reject_basis_audit BEFORE INSERT ON audit_events
            WHEN NEW.action='DIVIDEND_BASIS_ASSERTED'
            BEGIN SELECT RAISE(ABORT,'SYNTHETIC_BASIS_AUDIT_FAILURE'); END
        """)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError, match="SYNTHETIC_BASIS_AUDIT_FAILURE"):
        save(engine, basis)
    assert database_snapshot(engine) == before


def test_source_and_declaration_citations_stay_private_without_changing_raw_hash(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    source_citation = EvidenceCitation(
        id=uuid4(),
        citation="Private synthetic source reference",
        reason="Explicit synthetic source citation.",
        url="https://example.com/synthetic.pdf?private=fixture#source",
    )
    with unit_of_work(engine, write=True) as con:
        observed = cloned_source(
            con,
            {"observation_id": str(basis.observation_id)},
            evidence=source_citation.model_dump(mode="json"),
        )
    assertion = SplitBasisAssertion.model_validate(
        {
            **basis.model_dump(),
            "observation_id": observed["observation_id"],
            "source_sha256": observed["source_sha256"],
            "url": "https://example.com/basis.pdf?private=fixture#declaration",
        }
    )
    stored = save(engine, assertion)
    before = database_snapshot(engine)
    assert read(engine, stored.id) == stored
    with unit_of_work(engine) as con:
        original = original_dividend(con, assertion.observation_id)
        assert original is not None and original.evidence is not None
        assert original.evidence.url == source_citation.url
        assert original.normalized_sha256 == assertion.source_sha256
        assert normalize_basis(assertion.observation_id, original, assertion).dps == "0.90000000"
        audit = con.exec_driver_sql(
            "SELECT after_sha256 FROM audit_events WHERE action='DIVIDEND_BASIS_ASSERTED'"
        ).scalar_one()
        assert len(audit) == 64
    assert "private=fixture" not in repr(stored) and "Private synthetic source" not in repr(stored)
    assert database_snapshot(engine) == before


def test_writer_and_reads_have_no_private_files_or_upstream_client_access(
    engine: Engine, basis: SplitBasisAssertion, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "Basis persistence cannot read private files or construct HTTP clients"
        )

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(ProviderConfigurationStore, "unchanged", forbidden)
    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    result = save(engine, basis)
    assert read(engine, basis.id) == result and current(engine, basis.id) == result


@pytest.mark.parametrize("operation", ["save", "current"])
def test_new_writes_or_current_use_require_an_active_owner_and_writer(
    engine: Engine, basis: SplitBasisAssertion, operation: str
) -> None:
    record = save(engine, basis)
    before = database_snapshot(engine)
    with (
        pytest.raises(service.BasisError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"),
        unit_of_work(engine) as con,
    ):
        if operation == "save":
            service.save(con, OWNER, basis, at=AT, request_id=record.request_id)
        else:
            service.require_current(con, OWNER, basis.id)
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (str(OWNER),))
    before = database_snapshot(engine)
    with pytest.raises(service.BasisError, match="^BASIS_OWNER_UNAVAILABLE$"):
        if operation == "save":
            save(engine, basis, request_id=record.request_id)
        else:
            current(engine, basis.id)
    assert read(engine, basis.id) == record and database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner_id", "not-a-uuid"),
        ("request_id", "not-a-uuid"),
        ("request_id", None),
        ("at", AT.replace(tzinfo=None)),
        ("at", 1),
        ("at", "2026-11-08T00:00:00Z"),
        ("at", datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=14)))),
        ("assertion", None),
        ("assertion", {}),
    ],
)
def test_invalid_internal_types_are_rejected_without_echo_or_writes(
    engine: Engine, basis: SplitBasisAssertion, field: str, value: Any
) -> None:
    before = database_snapshot(engine)
    arguments: dict[str, Any] = {
        "owner_id": OWNER,
        "assertion": basis,
        "at": AT,
        "request_id": uuid4(),
        field: value,
    }
    with pytest.raises(service.BasisError), unit_of_work(engine, write=True) as con:
        service.save(con, **arguments)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["id", "owner"])
def test_read_scope_is_checked_before_binding_sql(
    engine: Engine, basis: SplitBasisAssertion, field: str
) -> None:
    before = database_snapshot(engine)
    with (
        pytest.raises(service.BasisError, match="^INVALID_BASIS_SCOPE$"),
        unit_of_work(engine) as con,
    ):
        service.get(
            con,
            cast(Any, "private-invalid-owner" if field == "owner" else OWNER),
            cast(Any, "private-invalid-id" if field == "id" else basis.id),
        )
    assert database_snapshot(engine) == before


def test_original_read_scope_is_not_arbitrary_sql_input(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    with pytest.raises(ApiError) as error, unit_of_work(engine) as con:
        original_dividend(con, "not-an-original-id")  # type: ignore[arg-type]
    assert error.value.code == "INVALID_SOURCE_SCOPE"


def test_non_utc_aware_creation_time_is_canonical(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    result = save(engine, basis, at=AT.astimezone(timezone(timedelta(hours=8))))
    assert result.created_at == AT
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(f"SELECT created_at FROM {TABLE}").scalar_one() == stamp(AT)
    assert read(engine, basis.id) == result


@pytest.mark.parametrize("change", ["reason", "ratio", "flags", "identifier"])
def test_frozen_model_copies_do_not_bypass_input_validation(
    engine: Engine, basis: SplitBasisAssertion, change: str
) -> None:
    invalid: dict[str, dict[str, Any]] = {
        "reason": {"reason": "private-invalid\0declaration"},
        "ratio": {"splits": (basis.splits[0].model_copy(update={"ratio_numerator": 1.5}),)},
        "flags": {"complete": 1},
        "identifier": {"observation_id": "private-invalid-uuid"},
    }
    before = database_snapshot(engine)
    with pytest.raises(service.BasisError, match="^INVALID_BASIS_ASSERTION$") as error:
        save(engine, basis.model_copy(update=invalid[change]))
    assert "private-invalid" not in str(error.value) and database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["source_sha256", "security_id", "ex_date"])
def test_new_declarations_pin_actual_original_source_terms(
    engine: Engine, basis: SplitBasisAssertion, field: str
) -> None:
    changed = {"source_sha256": "0" * 64, "security_id": uuid4(), "ex_date": "2026-01-07"}[field]
    body = basis.model_copy(
        update={field: changed, "splits": () if field == "security_id" else basis.splits}
    )
    before = database_snapshot(engine)
    with pytest.raises(service.BasisError, match="^BASIS_SOURCE_CHANGED$"):
        save(engine, body)
    assert database_snapshot(engine) == before


def test_missing_source_is_unavailable_not_zero_income(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(service.BasisError, match="^BASIS_SOURCE_UNAVAILABLE$"):
        save(engine, basis.model_copy(update={"observation_id": uuid4()}))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("adjustment", ["UNKNOWN", "AS_DECLARED"])
def test_no_declaration_relabels_an_original_unknown_or_declared_basis(
    engine: Engine, basis: SplitBasisAssertion, adjustment: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        other = cloned_source(
            con, {"observation_id": str(basis.observation_id)}, adjustment_basis=adjustment
        )
    body = basis.model_copy(
        update={
            "observation_id": UUID(other["observation_id"]),
            "source_sha256": other["source_sha256"],
        }
    )
    before = database_snapshot(engine)
    with pytest.raises(service.BasisError, match="^BASIS_SOURCE_CHANGED$"):
        save(engine, body)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("missing", ["dps", "currency", "both"])
def test_null_source_amount_or_currency_stays_null_after_audited_storage(
    engine: Engine, basis: SplitBasisAssertion, missing: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        changes = {name: None for name in ("dps", "currency") if missing in {name, "both"}}
        other = cloned_source(con, {"observation_id": str(basis.observation_id)}, **changes)
    body = basis.model_copy(
        update={
            "observation_id": UUID(other["observation_id"]),
            "source_sha256": other["source_sha256"],
        }
    )
    before = database_snapshot(engine)
    stored = save(engine, body)
    assert read(engine, stored.id) == stored and current(engine, stored.id) == stored
    after = database_snapshot(engine)
    assert {name: rows for name, rows in before.items() if name not in {TABLE, "audit_events"}} == {
        name: rows for name, rows in after.items() if name not in {TABLE, "audit_events"}
    }


@pytest.mark.parametrize("kind", ["SPLIT", "REVERSE_SPLIT", "CASH_IN_LIEU", "MERGER", "SPIN_OFF"])
def test_newly_known_quantity_actions_block_new_use_but_preserve_audited_history(
    engine: Engine, basis: SplitBasisAssertion, kind: str
) -> None:
    record = save(engine, basis)
    with unit_of_work(engine, write=True) as con:
        action(
            con,
            str(uuid4()),
            security_id=str(SECURITY),
            effective_date="2026-01-09",
            action_type=kind,
            ratio_num=3 if kind == "SPLIT" else 1 if kind == "REVERSE_SPLIT" else None,
            ratio_den=2 if kind in {"SPLIT", "REVERSE_SPLIT"} else None,
            status="CONFIRMED" if kind in {"SPLIT", "REVERSE_SPLIT"} else "NEEDS_REVIEW",
        )
    before = database_snapshot(engine)
    assert read(engine, basis.id) == record
    assert save(engine, basis, request_id=record.request_id) == record
    with pytest.raises(service.BasisError, match="^BASIS_ACTIONS_CHANGED$"):
        current(engine, basis.id)
    with pytest.raises(service.BasisError, match="^BASIS_ACTIONS_CHANGED$"):
        save(engine, basis.model_copy(update={"id": uuid4()}))
    assert database_snapshot(engine) == before


def test_unconfirmed_replacement_cannot_be_ignored_as_an_empty_chain(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    record = save(engine, basis)
    ref = basis.splits[0]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET is_current=0,version=version+1 WHERE id=?", (str(ref.id),)
        )
        action(
            con,
            str(uuid4()),
            root_id=str(ref.root_id),
            supersedes_id=str(ref.id),
            revision=2,
            security_id=str(SECURITY),
            effective_date=ref.effective_date,
            status="NEEDS_REVIEW",
        )
    before = database_snapshot(engine)
    assert read(engine, basis.id) == record
    with pytest.raises(service.BasisError, match="^BASIS_ACTIONS_CHANGED$"):
        save(engine, basis.model_copy(update={"id": uuid4(), "splits": ()}))
    assert database_snapshot(engine) == before


def test_void_preserves_history_and_requires_an_explicit_new_empty_declaration(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    record = save(engine, basis)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET status='VOID',version=version+1 WHERE id=?",
            (str(basis.splits[0].id),),
        )
    before = database_snapshot(engine)
    assert read(engine, basis.id) == record
    with pytest.raises(service.BasisError, match="^BASIS_ACTIONS_CHANGED$"):
        current(engine, basis.id)
    assert database_snapshot(engine) == before
    replacement = save(engine, basis.model_copy(update={"id": uuid4(), "splits": ()}))
    assert current(engine, replacement.id) == replacement


def test_earlier_basis_excludes_ex_date_and_requires_the_exact_inverse_chain(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    identifier = uuid4()
    with unit_of_work(engine, write=True) as con:
        action(
            con,
            str(identifier),
            security_id=str(SECURITY),
            action_type="REVERSE_SPLIT",
            effective_date="2026-01-06",
            ratio_num=1,
            ratio_den=3,
        )
    earlier = basis.model_copy(
        update={"id": uuid4(), "share_basis_date": "2026-01-05", "splits": ()}
    )
    with pytest.raises(service.BasisError, match="^BASIS_ACTIONS_CHANGED$"):
        save(engine, earlier)
    reference = SplitReference.model_validate(
        {
            **basis.splits[0].model_dump(),
            "id": identifier,
            "root_id": identifier,
            "action_type": "REVERSE_SPLIT",
            "effective_date": "2026-01-06",
            "ratio_numerator": 1,
            "ratio_denominator": 3,
        }
    )
    complete = earlier.model_copy(update={"splits": (reference,)})
    stored = save(engine, complete)
    assert current(engine, stored.id) == stored
    with unit_of_work(engine) as con:
        source = original_dividend(con, stored.assertion.observation_id)
        assert (
            source is not None
            and normalize_basis(
                source_id := stored.assertion.observation_id, source, stored.assertion
            ).dps
            == "1.80000000"
        )
        assert source_id == basis.observation_id


@pytest.mark.parametrize("field", ["version", "ratio_numerator", "root_id", "id"])
def test_forged_or_stale_action_snapshots_never_become_current_terms(
    engine: Engine, basis: SplitBasisAssertion, field: str
) -> None:
    value = 2 if field == "version" else 4 if field == "ratio_numerator" else uuid4()
    body = basis.model_copy(update={"splits": (basis.splits[0].model_copy(update={field: value}),)})
    before = database_snapshot(engine)
    with pytest.raises(service.BasisError, match="^BASIS_ACTIONS_CHANGED$"):
        save(engine, body)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("assertion_sha256", "0" * 64),
        ("source_sha256", "0" * 64),
        ("share_basis_date", "2026-01-11"),
        ("created_at", "2026-11-08T00:00:00.000000+00:00"),
        ("created_at", "2026-02-30T00:00:00.000000Z"),
        ("request_id", str(uuid4()).upper()),
        ("assertion_json", "not-json-private-canary"),
        ("assertion_json", " " * 131073),
    ],
)
def test_corrupt_retained_declaration_metadata_is_not_repaired_or_echoed(
    engine: Engine, basis: SplitBasisAssertion, field: str, value: Any
) -> None:
    save(engine, basis)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_dividend_basis_no_update")
        con.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        con.exec_driver_sql(f"UPDATE {TABLE} SET {field}=?", (value,))
        con.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    before = database_snapshot(engine)
    with pytest.raises(service.BasisError, match="^BASIS_ASSERTION_UNAVAILABLE$") as error:
        read(engine, basis.id)
    assert "private-canary" not in str(error.value) and database_snapshot(engine) == before


@pytest.mark.parametrize(
    "change",
    [
        "body_and_digest",
        "duplicate_key",
        "missing_key",
        "owner",
        "missing_audit",
        "duplicate_audit",
        "bad_audit_hash",
    ],
)
def test_canonical_body_and_original_actor_audit_are_both_required(
    engine: Engine, basis: SplitBasisAssertion, change: str
) -> None:
    save(engine, basis)
    owner = OWNER
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_dividend_basis_no_update")
        if change in {"body_and_digest", "duplicate_key", "missing_key"}:
            value = basis.model_dump(mode="json")
            if change == "body_and_digest":
                value["citation"] = "A different private citation must not replace the original."
            elif change == "missing_key":
                del value["url"]
            wire = canonical(value)
            if change == "duplicate_key":
                wire = wire[:-1] + ',"complete":true}'
            con.exec_driver_sql(
                f"UPDATE {TABLE} SET assertion_json=?,assertion_sha256=?", (wire, digest(wire))
            )
        elif change == "owner":
            owner = OTHER
            con.exec_driver_sql(f"UPDATE {TABLE} SET owner_id=?", (str(OTHER),))
        elif change == "missing_audit":
            con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_delete")
            con.exec_driver_sql("DELETE FROM audit_events WHERE action='DIVIDEND_BASIS_ASSERTED'")
        elif change == "duplicate_audit":
            value = dict(
                con.exec_driver_sql(
                    "SELECT * FROM audit_events WHERE action='DIVIDEND_BASIS_ASSERTED'"
                )
                .mappings()
                .one()
            )
            insert(con, "audit_events", {**value, "id": str(uuid4())})
        else:
            con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
            con.exec_driver_sql(
                "UPDATE audit_events SET after_sha256=? WHERE action='DIVIDEND_BASIS_ASSERTED'",
                ("0" * 64,),
            )
    before = database_snapshot(engine)
    with pytest.raises(service.BasisError, match="^BASIS_ASSERTION_UNAVAILABLE$"):
        read(engine, basis.id, owner)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem", ["hash", "mirror", "noncanonical", "legacy", "first_sighting", "first_origin"]
)
def test_original_source_integrity_is_required_for_history_and_each_new_use(
    engine: Engine, basis: SplitBasisAssertion, problem: str
) -> None:
    save(engine, basis)
    with unit_of_work(engine, write=True) as con:
        if problem == "first_sighting":
            con.exec_driver_sql("DROP TRIGGER trg_provider_provider_observation_sightings_delete")
            con.exec_driver_sql(
                "DELETE FROM provider_observation_sightings WHERE dividend_observation_id=?",
                (str(basis.observation_id),),
            )
        elif problem == "first_origin":
            con.exec_driver_sql("DROP TRIGGER trg_provider_provider_runs_update")
            con.exec_driver_sql(
                "UPDATE provider_runs SET origin_group='EODHD' "
                "WHERE id=(SELECT provider_run_id FROM dividend_observations WHERE id=?)",
                (str(basis.observation_id),),
            )
        else:
            con.exec_driver_sql("DROP TRIGGER trg_dividend_observation_no_update")
            field, value = {
                "hash": ("normalized_sha256", "0" * 64),
                "mirror": ("dps_e8", 60000001),
                "legacy": ("provenance_version", 0),
                "noncanonical": (
                    "normalized_json",
                    json.dumps(
                        json.loads(
                            con.exec_driver_sql(
                                "SELECT normalized_json FROM dividend_observations WHERE id=?",
                                (str(basis.observation_id),),
                            ).scalar_one()
                        )
                    ),
                ),
            }[problem]
            con.exec_driver_sql(
                f"UPDATE dividend_observations SET {field}=? WHERE id=?",
                (value, str(basis.observation_id)),
            )
    before = database_snapshot(engine)
    with pytest.raises(service.BasisError, match="^BASIS_ASSERTION_UNAVAILABLE$"):
        read(engine, basis.id)
    with pytest.raises(service.BasisError, match="^BASIS_SOURCE_UNAVAILABLE$"):
        save(engine, basis.model_copy(update={"id": uuid4()}))
    assert database_snapshot(engine) == before


def test_a_bare_sql_row_without_its_creation_audit_cannot_be_adopted(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    values = {
        "id": str(basis.id),
        "owner_id": str(OWNER),
        "observation_id": str(basis.observation_id),
        "source_sha256": basis.source_sha256,
        "security_id": str(basis.security_id),
        "ex_date": basis.ex_date,
        "share_basis_date": basis.share_basis_date,
        "assertion_json": canonical(basis.model_dump(mode="json")),
        "assertion_sha256": digest(canonical(basis.model_dump(mode="json"))),
        "request_id": str(uuid4()),
        "created_at": stamp(AT),
    }
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, values)
    before = database_snapshot(engine)
    with pytest.raises(service.BasisError, match="^BASIS_ASSERTION_UNAVAILABLE$"):
        save(engine, basis, request_id=UUID(values["request_id"]))
    assert database_snapshot(engine) == before


def test_two_internal_retries_commit_one_declaration_and_one_audit(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    request = uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save(engine, basis, request_id=request), range(2)))
    assert results[0] == results[1] and read(engine, basis.id) == results[0]
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == 1
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='DIVIDEND_BASIS_ASSERTED'"
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize("problem", ["missing", "ratio", "noncanonical_uuid", "status"])
def test_corrupt_historical_actions_cannot_be_repaired_or_reinterpreted(
    engine: Engine, basis: SplitBasisAssertion, problem: str
) -> None:
    save(engine, basis)
    ref = basis.splits[0]
    with unit_of_work(engine, write=True) as con:
        if problem == "missing":
            con.exec_driver_sql("DROP TRIGGER trg_ledger_corporate_actions_no_delete")
            con.exec_driver_sql("DELETE FROM corporate_actions WHERE id=?", (str(ref.id),))
        else:
            con.exec_driver_sql("DROP TRIGGER trg_ledger_corporate_actions_immutable")
            con.exec_driver_sql("DROP TRIGGER trg_ledger_action_state")
            field: str
            value: str | int
            if problem == "noncanonical_uuid":
                upper = str(SECURITY).upper()
                insert(
                    con,
                    "securities",
                    {
                        "id": upper,
                        "exchange_code": "NASDAQ",
                        "display_ticker": "CORRUPT",
                        "name": "Synthetic corrupted identity",
                        "quote_currency": "USD",
                        "created_at": stamp(AT),
                    },
                )
                field, value = "security_id", upper
            else:
                field, value = (
                    ("ratio_num", 4) if problem == "ratio" else ("status", "NEEDS_REVIEW")
                )
            con.exec_driver_sql(
                f"UPDATE corporate_actions SET {field}=? WHERE id=?", (value, str(ref.id))
            )
    before = database_snapshot(engine)
    with pytest.raises(service.BasisError, match="^BASIS_ASSERTION_UNAVAILABLE$"):
        read(engine, basis.id)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("kind", ["foreign", "outside", "display_only", "void"])
def test_current_revalidation_ignores_only_known_non_applicable_actions(
    engine: Engine, basis: SplitBasisAssertion, kind: str
) -> None:
    record = save(engine, basis)
    with unit_of_work(engine, write=True) as con:
        identifier = str(uuid4())
        if kind == "display_only":
            action(
                con,
                identifier,
                security_id=str(SECURITY),
                action_type="SYMBOL_CHANGE",
                effective_date="2026-01-09",
                ratio_num=None,
                ratio_den=None,
                status="NEEDS_REVIEW",
                new_symbol="EXPLICIT",
            )
        else:
            action(
                con,
                identifier,
                security_id="stock" if kind == "foreign" else str(SECURITY),
                effective_date="2026-01-11" if kind == "outside" else "2026-01-09",
                action_type="MERGER",
                ratio_num=None,
                ratio_den=None,
                status="NEEDS_REVIEW",
            )
            if kind == "void":
                con.exec_driver_sql(
                    "UPDATE corporate_actions SET status='VOID',version=version+1 WHERE id=?",
                    (identifier,),
                )
    before = database_snapshot(engine)
    assert current(engine, record.id) == record and database_snapshot(engine) == before


def test_exact_int64_ratios_keep_unrepresentable_derived_dps_unresolved(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    old = basis.splits[0]
    identifier = uuid4()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET is_current=0,version=version+1 WHERE id=?", (str(old.id),)
        )
        action(
            con,
            str(identifier),
            root_id=str(old.root_id),
            supersedes_id=str(old.id),
            revision=2,
            security_id=str(SECURITY),
            effective_date=old.effective_date,
            ratio_num=9223372036854775807,
            ratio_den=9223372036854775806,
        )
    body = basis.model_copy(
        update={
            "splits": (
                old.model_copy(
                    update={
                        "id": identifier,
                        "ratio_numerator": 9223372036854775807,
                        "ratio_denominator": 9223372036854775806,
                    }
                ),
            )
        }
    )
    stored = save(engine, body)
    assert current(engine, stored.id) == stored
    with unit_of_work(engine) as con:
        source = original_dividend(con, stored.assertion.observation_id)
        assert source is not None
        result = normalize_basis(stored.assertion.observation_id, source, stored.assertion)
        assert result.dps == source.dps == "0.60000000"
        assert result.issues == {"UNRESOLVED_ADJUSTMENT", "NORMALIZED_DPS_PRECISION"}
        assert (
            con.exec_driver_sql(
                "SELECT dps_e8 FROM dividend_observations WHERE id=?",
                (str(source_id := stored.assertion.observation_id),),
            ).scalar_one()
            == 60000000
        )
        assert source_id == basis.observation_id


@pytest.mark.anyio
async def test_real_http_ledger_and_captured_adapter_evidence_remain_unchanged_by_basis_writes(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    page = await fetched(book, payload=[{"date": "2026-01-08", "value": "0.6", "currency": "USD"}])
    receipt = capture_source(engine, book, page)
    original = page.rows[0]
    assert original.adjustment_basis == "SPLIT_ADJUSTED"
    action_body = {
        "security_id": book["security"]["id"],
        "action_type": "SPLIT",
        "effective_date": "2026-01-08",
        "ratio_numerator": "3",
        "ratio_denominator": "2",
        "status": "CONFIRMED",
        "reason": "Explicit synthetic financial action terms.",
    }
    actual = await create(client, "corporate-actions", action_body)
    reference = SplitReference.model_validate(
        {key: actual[key] for key in SplitReference.model_fields}
    )
    body = SplitBasisAssertion(
        id=uuid4(),
        observation_id=UUID(receipt.observation_ids[0]),
        source_sha256=original.normalized_sha256,
        security_id=UUID(book["security"]["id"]),
        ex_date="2026-01-08",
        share_basis_date="2026-01-10",
        source_basis="AFTER_EFFECTIVE_DATE_ACTIONS",
        target_basis="PRE_EX_DATE_EVENTS",
        complete=True,
        split_only=True,
        splits=(reference,),
        citation="Explicit software fixture scope, not verified market history",
        reason="Real HTTP ledger; this basis declaration is an internal controller test only.",
    )
    before = database_snapshot(engine)
    actor = UUID(book["owner"]["id"])
    record = save(engine, body, owner_id=actor, at=now())
    assert read(engine, record.id, actor) == record and current(engine, record.id, actor) == record
    after = database_snapshot(engine)
    assert {name: rows for name, rows in before.items() if name not in {TABLE, "audit_events"}} == {
        name: rows for name, rows in after.items() if name not in {TABLE, "audit_events"}
    }
    changed = await client.patch(
        f"/api/v1/corporate-actions/{actual['id']}",
        headers=version_headers(client, actual["version"]),
        json={
            **action_body,
            "ratio_numerator": "2",
            "ratio_denominator": "1",
            "reason": "Explicit synthetic correction preserves historical basis statement.",
        },
    )
    assert changed.status_code == 200
    baseline = database_snapshot(engine)
    assert read(engine, record.id, actor) == record
    with pytest.raises(service.BasisError, match="^BASIS_ACTIONS_CHANGED$"):
        current(engine, record.id, actor)
    assert database_snapshot(engine) == baseline
    response = await client.get("/api/v1/dividends/observations")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    row = next(row for row in response.json()["items"] if row["id"] == str(body.observation_id))
    assert row["dps"] == "0.60000000" and row["adjustment_basis"] == "SPLIT_ADJUSTED"
    assert (
        row["normalized_sha256"] == body.source_sha256
        and row["canonical_adoption"] == "NOT_ADOPTED"
    )
