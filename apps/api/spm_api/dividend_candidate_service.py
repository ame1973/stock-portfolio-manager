"""Owned, audited candidate decisions; no HTTP authorization or financial adoption."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text

import spm_api.dividend_basis_service as bases
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.decimal_types import from_scaled, to_scaled
from spm_api.dividend_evidence import DividendTerms, EvidenceAssessment, EvidenceMatch, assess
from spm_api.errors import ApiError, Model
from spm_api.master_models import ChangeReason
from spm_api.observation_service import original_dividend

Reason = Annotated[ChangeReason, Field(pattern=r"^[^\x00]+$")]
Version = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]


class CandidateError(ValueError):
    """Fixed codes only; never expose retained decisions, citations or source content."""


class CandidateMatch(Model):
    """One explicit source association, not proof of event identity."""

    model_config = ConfigDict(frozen=True)

    observation_id: UUID
    source_sha256: Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
    basis_assertion_id: UUID | None = None
    reason: Reason = Field(repr=False)


class CandidateDecision(Model):
    """A caller's proposed revision; IDs and actor authorization remain caller obligations."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    candidate_id: UUID
    revision_no: Version
    state: Literal["OPEN", "DISMISSED"]
    terms: DividendTerms = Field(repr=False)
    matches: Annotated[tuple[CandidateMatch, ...], Field(max_length=100)] = Field(repr=False)
    change_reason: Reason = Field(repr=False)

    @field_validator("matches")
    @classmethod
    def unique_sorted_matches(cls, value: tuple[CandidateMatch, ...]) -> tuple[CandidateMatch, ...]:
        if len({match.observation_id for match in value}) != len(value):
            raise ValueError("An observation cannot be associated more than once")
        return tuple(sorted(value, key=lambda match: str(match.observation_id)))


class StoredCandidate(Model):
    """Private immutable history, not a public DTO or permission to adopt terms."""

    model_config = ConfigDict(frozen=True)

    owner_id: UUID = Field(repr=False)
    decision: CandidateDecision = Field(repr=False)
    request_id: UUID = Field(repr=False)
    created_at: Annotated[AwareDatetime, Field(strict=True)]


@dataclass(frozen=True, slots=True)
class ReviewedCandidate:
    record: StoredCandidate = field(repr=False)
    assessment: EvidenceAssessment = field(repr=False)


def _flat(record: StoredCandidate) -> dict[str, Any]:
    decision, terms = record.decision, record.decision.terms
    matches = canonical([match.model_dump(mode="json") for match in decision.matches])
    if len(matches.encode("utf-8")) > 131_072:
        raise ValueError("Candidate associations exceed their storage bound")
    return {
        "id": str(decision.id),
        "candidate_id": str(decision.candidate_id),
        "owner_id": str(record.owner_id),
        "security_id": str(terms.security_id),
        "revision_no": decision.revision_no,
        "state": decision.state,
        "event_type": terms.event_type,
        "ex_date": terms.ex_date,
        "announce_date": terms.announce_date,
        "record_date": terms.record_date,
        "pay_date": terms.pay_date,
        "dps_e8": to_scaled(terms.dps, 8) if terms.dps is not None else None,
        "currency": terms.currency,
        "adjustment_basis": terms.adjustment_basis,
        "matches_json": matches,
        "matches_sha256": digest(matches),
        "change_reason": decision.change_reason,
        "request_id": str(record.request_id),
        "created_at": stamp(record.created_at.astimezone(UTC)),
    }


def _stored(row: RowMapping) -> StoredCandidate:
    try:
        raw = row["matches_json"]
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 131_072:
            raise ValueError
        record = StoredCandidate.model_validate(
            {
                "owner_id": row["owner_id"],
                "request_id": row["request_id"],
                "created_at": datetime.fromisoformat(row["created_at"]),
                "decision": {
                    "id": row["id"],
                    "candidate_id": row["candidate_id"],
                    "revision_no": row["revision_no"],
                    "state": row["state"],
                    "terms": {
                        "security_id": row["security_id"],
                        "event_type": row["event_type"],
                        "ex_date": row["ex_date"],
                        "announce_date": row["announce_date"],
                        "record_date": row["record_date"],
                        "pay_date": row["pay_date"],
                        "dps": from_scaled(row["dps_e8"], 8) if row["dps_e8"] is not None else None,
                        "currency": row["currency"],
                        "adjustment_basis": row["adjustment_basis"],
                    },
                    "matches": json.loads(raw),
                    "change_reason": row["change_reason"],
                },
            }
        )
        if _flat(record) != dict(row):
            raise ValueError
        return record
    except (ValueError, TypeError, KeyError, OverflowError):
        raise CandidateError("CANDIDATE_HISTORY_UNAVAILABLE") from None


def _scope(owner: UUID, identifier: UUID, version: int | None = None) -> None:
    if (
        not isinstance(owner, UUID)
        or not isinstance(identifier, UUID)
        or (version is not None and (type(version) is not int or not 1 <= version <= 2_147_483_647))
    ):
        raise CandidateError("INVALID_CANDIDATE_SCOPE")


def _history_scope(con: Connection, owner: UUID, identifier: UUID) -> bool:
    row = con.execute(
        text("""
        SELECT count(*) AS total,
          sum(CASE WHEN owner_id=:owner THEN 1 ELSE 0 END) AS owned,
          min(revision_no) AS first_version,max(revision_no) AS last_version,
          count(security_id) AS securities,min(security_id) AS first_security,
          max(security_id) AS last_security
        FROM dividend_candidate_revisions WHERE candidate_id=:id
    """),
        {"owner": str(owner), "id": str(identifier)},
    ).one()
    if not row.total or not row.owned:
        return False
    if (
        row.owned != row.total
        or row.first_version != 1
        or row.last_version != row.total
        or row.securities != row.total
        or row.first_security != row.last_security
    ):
        raise CandidateError("CANDIDATE_HISTORY_UNAVAILABLE")
    return True


def _writer(con: Connection, owner: UUID) -> None:
    if not con.in_transaction() or not con.get_execution_options().get("writer"):
        raise CandidateError("WRITE_UNIT_OF_WORK_REQUIRED")
    if not isinstance(owner, UUID):
        raise CandidateError("INVALID_CANDIDATE_SCOPE")
    if (
        con.execute(
            text("SELECT 1 FROM owners WHERE id=:id AND is_active=1"), {"id": str(owner)}
        ).first()
        is None
    ):
        raise CandidateError("CANDIDATE_OWNER_UNAVAILABLE")


def _audit_values(record: StoredCandidate) -> dict[str, Any]:
    values = _flat(record)
    return {
        "id": values["id"],
        "candidate_id": values["candidate_id"],
        "security_id": values["security_id"],
        "revision_no": values["revision_no"],
        "state": values["state"],
        "terms": record.decision.terms.model_dump(mode="json"),
        "matches_sha256": values["matches_sha256"],
        "matches_count": len(record.decision.matches),
        "created_at": values["created_at"],
        "scope": "CANDIDATE_ONLY",
    }


def _audited(con: Connection, record: StoredCandidate) -> None:
    decision = record.decision
    previous = None
    if decision.revision_no > 1:
        row = (
            con.execute(
                text(
                    "SELECT * FROM dividend_candidate_revisions "
                    "WHERE candidate_id=:id AND revision_no=:version"
                ),
                {"id": str(decision.candidate_id), "version": decision.revision_no - 1},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise CandidateError("CANDIDATE_HISTORY_UNAVAILABLE")
        previous = _stored(row)
        if (
            previous.owner_id != record.owner_id
            or previous.decision.terms.security_id != decision.terms.security_id
            or previous.created_at > record.created_at
        ):
            raise CandidateError("CANDIDATE_HISTORY_UNAVAILABLE")
    rows = con.execute(
        text("""
        SELECT owner_id,request_id,reason,before_sha256,after_sha256 FROM audit_events
        WHERE entity_type='dividend_candidate_revision' AND entity_id=:id
          AND action='DIVIDEND_CANDIDATE_REVISED'
    """),
        {"id": str(decision.id)},
    ).all()
    expected = (
        str(record.owner_id),
        str(record.request_id),
        decision.change_reason,
        digest(canonical(_audit_values(previous))) if previous is not None else None,
        digest(canonical(_audit_values(record))),
    )
    if len(rows) != 1 or tuple(rows[0]) != expected:
        raise CandidateError("CANDIDATE_HISTORY_UNAVAILABLE")


def _checked(con: Connection, row: RowMapping) -> StoredCandidate:
    record = _stored(row)
    _audited(con, record)
    return record


def _assessment(con: Connection, record: StoredCandidate, *, current: bool) -> EvidenceAssessment:
    decision = record.decision
    matches: list[EvidenceMatch] = []
    for reference in decision.matches:
        try:
            source = original_dividend(con, reference.observation_id)
        except ApiError:
            raise CandidateError("CANDIDATE_SOURCE_UNAVAILABLE") from None
        if source is None:
            raise CandidateError("CANDIDATE_SOURCE_UNAVAILABLE")
        if (
            source.security_id != decision.terms.security_id
            or source.normalized_sha256 != reference.source_sha256
        ):
            raise CandidateError("CANDIDATE_SOURCE_CHANGED")
        assertion = None
        if reference.basis_assertion_id is not None:
            try:
                stored = (
                    bases.require_current(con, record.owner_id, reference.basis_assertion_id)
                    if current
                    else bases.get(con, record.owner_id, reference.basis_assertion_id)
                )
            except bases.BasisError as error:
                code = (
                    "CANDIDATE_BASIS_CHANGED"
                    if current and str(error) == "BASIS_ACTIONS_CHANGED"
                    else "CANDIDATE_BASIS_UNAVAILABLE"
                )
                raise CandidateError(code) from None
            if stored is None:
                raise CandidateError("CANDIDATE_BASIS_UNAVAILABLE")
            assertion = stored.assertion
            if (
                assertion.observation_id != reference.observation_id
                or assertion.source_sha256 != reference.source_sha256
                or assertion.security_id != decision.terms.security_id
            ):
                raise CandidateError("CANDIDATE_BASIS_CHANGED")
        matches.append(
            EvidenceMatch(
                candidate_id=decision.candidate_id,
                observation_id=reference.observation_id,
                observation=source,
                reason=reference.reason,
                basis_assertion=assertion,
            )
        )
    try:
        return assess(decision.candidate_id, decision.terms, matches)
    except ValueError:
        raise CandidateError("INVALID_CANDIDATE_DECISION") from None


def _historical(con: Connection, row: RowMapping) -> ReviewedCandidate:
    record = _checked(con, row)
    try:
        result = _assessment(con, record, current=False)
    except CandidateError:
        raise CandidateError("CANDIDATE_HISTORY_UNAVAILABLE") from None
    return ReviewedCandidate(record, result)


def get(
    con: Connection, owner_id: UUID, candidate_id: UUID, *, version: int | None = None
) -> ReviewedCandidate | None:
    """Read an audited original decision, never claiming a basis remains current."""
    _scope(owner_id, candidate_id, version)
    if not _history_scope(con, owner_id, candidate_id):
        return None
    row = (
        con.execute(
            text("""
            SELECT * FROM dividend_candidate_revisions
            WHERE owner_id=:owner AND candidate_id=:id
              AND (:version IS NULL OR revision_no=:version)
            ORDER BY revision_no DESC LIMIT 1
        """),
            {"owner": str(owner_id), "id": str(candidate_id), "version": version},
        )
        .mappings()
        .first()
    )
    return _historical(con, row) if row is not None else None


def require_current(
    con: Connection, owner_id: UUID, candidate_id: UUID, *, version: int
) -> ReviewedCandidate:
    """Fence a new use inside its writer; historical comparison alone is not a grant."""
    _writer(con, owner_id)
    if type(version) is not int:
        raise CandidateError("INVALID_CANDIDATE_SCOPE")
    _scope(owner_id, candidate_id, version)
    if not _history_scope(con, owner_id, candidate_id):
        raise CandidateError("CANDIDATE_NOT_FOUND")
    row = (
        con.execute(
            text("""
            SELECT * FROM dividend_candidate_revisions
            WHERE owner_id=:owner AND candidate_id=:id ORDER BY revision_no DESC LIMIT 1
        """),
            {"owner": str(owner_id), "id": str(candidate_id)},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise CandidateError("CANDIDATE_NOT_FOUND")
    record = _checked(con, row)
    if record.decision.revision_no != version:
        raise CandidateError("CANDIDATE_VERSION_CONFLICT")
    if record.decision.state != "OPEN":
        raise CandidateError("CANDIDATE_DISMISSED")
    return ReviewedCandidate(record, _assessment(con, record, current=True))


def save(
    con: Connection,
    owner_id: UUID,
    decision: CandidateDecision,
    *,
    at: datetime,
    request_id: UUID,
) -> ReviewedCandidate:
    """Append one decision/audit atomically; callers still owe actual session/CSRF checks."""
    _writer(con, owner_id)
    try:
        if (
            not isinstance(decision, CandidateDecision)
            or not isinstance(request_id, UUID)
            or not isinstance(at, datetime)
            or at.utcoffset() is None
        ):
            raise ValueError
        record = StoredCandidate(
            owner_id=owner_id,
            decision=CandidateDecision.model_validate(decision.model_dump(warnings="error")),
            request_id=request_id,
            created_at=at.astimezone(UTC),
        )
        values = _flat(record)
    except (ValueError, TypeError, OverflowError, PydanticSerializationError):
        raise CandidateError("INVALID_CANDIDATE_DECISION") from None
    existing = (
        con.execute(text("SELECT * FROM dividend_candidate_revisions WHERE id=:id"), values)
        .mappings()
        .first()
    )
    if existing is not None:
        if existing["owner_id"] != str(owner_id):
            raise CandidateError("CANDIDATE_REVISION_CONFLICT")
        original = _historical(con, existing)
        if not _history_scope(con, owner_id, original.record.decision.candidate_id):
            raise CandidateError("CANDIDATE_HISTORY_UNAVAILABLE")
        if _flat(original.record) != values:
            raise CandidateError("CANDIDATE_REVISION_CONFLICT")
        return original
    previous_row = (
        con.execute(
            text(
                "SELECT * FROM dividend_candidate_revisions "
                "WHERE candidate_id=:id ORDER BY revision_no DESC LIMIT 1"
            ),
            {"id": str(record.decision.candidate_id)},
        )
        .mappings()
        .first()
    )
    previous = None
    if previous_row is not None:
        if not _history_scope(con, owner_id, record.decision.candidate_id):
            raise CandidateError("CANDIDATE_VERSION_CONFLICT")
        previous = _historical(con, previous_row).record
        if (
            previous.decision.terms.security_id != record.decision.terms.security_id
            or record.decision.revision_no != previous.decision.revision_no + 1
        ):
            raise CandidateError("CANDIDATE_VERSION_CONFLICT")
        if record.created_at < previous.created_at:
            raise CandidateError("CANDIDATE_TIME_CONFLICT")
    elif record.decision.revision_no != 1:
        raise CandidateError("CANDIDATE_VERSION_CONFLICT")
    if (
        con.execute(
            text("SELECT 1 FROM securities WHERE id=:id"),
            {"id": str(record.decision.terms.security_id)},
        ).first()
        is None
    ):
        raise CandidateError("CANDIDATE_SECURITY_UNAVAILABLE")
    result = _assessment(con, record, current=True)
    con.execute(
        text(
            f"INSERT INTO dividend_candidate_revisions({','.join(values)}) "
            f"VALUES ({','.join(':' + name for name in values)})"
        ),
        values,
    )
    audit(
        con,
        str(owner_id),
        "dividend_candidate_revision",
        str(record.decision.id),
        "DIVIDEND_CANDIDATE_REVISED",
        request_id=str(request_id),
        before=_audit_values(previous) if previous is not None else None,
        after=_audit_values(record),
        reason=record.decision.change_reason,
    )
    return ReviewedCandidate(record, result)
