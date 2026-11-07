"""Internal owned basis declarations; HTTP authorization and canonical adoption remain external."""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text

from spm_api.audit import audit, canonical, digest, stamp
from spm_api.dividend_basis import SplitBasisAssertion, SplitReference, normalize_basis
from spm_api.errors import ApiError, Model
from spm_api.observation_service import original_dividend


class BasisError(ValueError):
    """Fixed codes only; never interpolate private assertions, references or stored rows."""


class StoredBasis(Model):
    """Private historical declaration, not a public DTO, verified history or current-use grant."""

    model_config = ConfigDict(frozen=True)

    owner_id: UUID = Field(repr=False)
    assertion: SplitBasisAssertion = Field(repr=False)
    request_id: UUID = Field(repr=False)
    created_at: Annotated[AwareDatetime, Field(strict=True)]

    @property
    def id(self) -> UUID:
        return self.assertion.id


def _flat(record: StoredBasis) -> dict[str, Any]:
    assertion = record.assertion
    wire = canonical(assertion.model_dump(mode="json"))
    return {
        "id": str(record.id),
        "owner_id": str(record.owner_id),
        "observation_id": str(assertion.observation_id),
        "source_sha256": assertion.source_sha256,
        "security_id": str(assertion.security_id),
        "ex_date": assertion.ex_date,
        "share_basis_date": assertion.share_basis_date,
        "assertion_json": wire,
        "assertion_sha256": digest(wire),
        "request_id": str(record.request_id),
        "created_at": stamp(record.created_at.astimezone(UTC)),
    }


def _stored(row: RowMapping) -> StoredBasis:
    try:
        wire = row["assertion_json"]
        if not isinstance(wire, str) or len(wire.encode()) > 131072:
            raise ValueError
        record = StoredBasis(
            owner_id=row["owner_id"],
            assertion=SplitBasisAssertion.model_validate_json(wire),
            request_id=row["request_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
        if _flat(record) != dict(row):
            raise ValueError
        return record
    except (ValueError, TypeError, KeyError, OverflowError):
        raise BasisError("BASIS_ASSERTION_UNAVAILABLE") from None


def _scope(owner: UUID, identifier: UUID) -> None:
    if not isinstance(owner, UUID) or not isinstance(identifier, UUID):
        raise BasisError("INVALID_BASIS_SCOPE")


def _writer(con: Connection, owner: UUID) -> None:
    if not con.in_transaction() or not con.get_execution_options().get("writer"):
        raise BasisError("WRITE_UNIT_OF_WORK_REQUIRED")
    if not isinstance(owner, UUID):
        raise BasisError("INVALID_BASIS_SCOPE")
    if (
        con.execute(
            text("SELECT 1 FROM owners WHERE id=:id AND is_active=1"), {"id": str(owner)}
        ).first()
        is None
    ):
        raise BasisError("BASIS_OWNER_UNAVAILABLE")


def _audit_values(record: StoredBasis) -> dict[str, Any]:
    assertion = record.assertion
    return {
        "id": str(record.id),
        "observation_id": str(assertion.observation_id),
        "security_id": str(assertion.security_id),
        "source_sha256": assertion.source_sha256,
        "assertion_sha256": _flat(record)["assertion_sha256"],
        "share_basis_date": assertion.share_basis_date,
        "split_count": len(assertion.splits),
        "scope": "DECLARED_BASIS_ONLY",
    }


def _audited(con: Connection, record: StoredBasis) -> None:
    rows = con.execute(
        text("""
        SELECT owner_id,request_id,reason,before_sha256,after_sha256 FROM audit_events
        WHERE entity_type='dividend_basis_assertion' AND entity_id=:id
          AND action='DIVIDEND_BASIS_ASSERTED'
    """),
        {"id": str(record.id)},
    ).all()
    expected = (
        str(record.owner_id),
        str(record.request_id),
        record.assertion.reason,
        None,
        digest(canonical(_audit_values(record))),
    )
    if len(rows) != 1 or tuple(rows[0]) != expected:
        raise ValueError("Inconsistent declaration authorship")


def _reference(row: RowMapping) -> SplitReference:
    values = {
        "id": row["id"],
        "root_id": row["root_id"],
        "security_id": row["security_id"],
        "version": row["version"],
        "action_type": row["action_type"],
        "effective_date": row["effective_date"],
        "ratio_numerator": row["ratio_num"],
        "ratio_denominator": row["ratio_den"],
        "status": "CONFIRMED",
        "lifecycle_status": "CURRENT",
    }
    result = SplitReference.model_validate(values)
    if result.model_dump(mode="json") != values:
        raise ValueError("Noncanonical action snapshot")
    return result


def _historical_actions(con: Connection, assertion: SplitBasisAssertion) -> None:
    rows = (
        con.execute(
            text("""
        SELECT c.* FROM corporate_actions c
        JOIN json_each(:assertion,'$.splits') j ON c.id=json_extract(j.value,'$.id')
    """),
            {"assertion": canonical(assertion.model_dump(mode="json"))},
        )
        .mappings()
        .all()
    )
    if len(rows) != len(assertion.splits):
        raise ValueError("Missing historical action")
    expected = {str(reference.id): reference for reference in assertion.splits}
    for row in rows:
        current, observed = _reference(row), expected[row["id"]]
        if (
            row["status"] not in {"CONFIRMED", "VOID"}
            or type(row["is_current"]) is not int
            or row["is_current"] not in (0, 1)
            or current.version < observed.version
            or current.model_copy(update={"version": observed.version}) != observed
        ):
            raise ValueError("Inconsistent historical action")


def get(con: Connection, owner_id: UUID, identifier: UUID) -> StoredBasis | None:
    """Read original audited history after action corrections; never assert currentness."""
    _scope(owner_id, identifier)
    row = (
        con.execute(
            text("""
        SELECT * FROM dividend_basis_assertions WHERE id=:id AND owner_id=:owner
    """),
            {"id": str(identifier), "owner": str(owner_id)},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    record = _stored(row)
    try:
        source = original_dividend(con, record.assertion.observation_id)
        if source is None:
            raise ValueError
        normalize_basis(record.assertion.observation_id, source, record.assertion)
        _historical_actions(con, record.assertion)
        _audited(con, record)
    except (ValueError, TypeError, KeyError, OverflowError, ApiError):
        raise BasisError("BASIS_ASSERTION_UNAVAILABLE") from None
    return record


def _current(con: Connection, assertion: SplitBasisAssertion) -> None:
    try:
        source = original_dividend(con, assertion.observation_id)
    except ApiError:
        raise BasisError("BASIS_SOURCE_UNAVAILABLE") from None
    if source is None:
        raise BasisError("BASIS_SOURCE_UNAVAILABLE")
    try:
        normalize_basis(assertion.observation_id, source, assertion)
    except ValueError:
        raise BasisError("BASIS_SOURCE_CHANGED") from None
    rows = (
        con.execute(
            text("""
        SELECT * FROM corporate_actions WHERE security_id=:security
          AND is_current=1 AND status!='VOID' AND action_type!='SYMBOL_CHANGE'
          AND ((:basis>=:ex AND effective_date BETWEEN :ex AND :basis)
            OR (:basis<:ex AND effective_date>:basis AND effective_date<:ex))
        ORDER BY effective_date,id LIMIT 101
    """),
            {
                "security": str(assertion.security_id),
                "basis": assertion.share_basis_date,
                "ex": assertion.ex_date,
            },
        )
        .mappings()
        .all()
    )
    try:
        if any(row["status"] != "CONFIRMED" for row in rows):
            raise ValueError
        actual = sorted((_reference(row) for row in rows), key=lambda reference: str(reference.id))
        expected = sorted(assertion.splits, key=lambda reference: str(reference.id))
        if actual != expected:
            raise ValueError
    except (ValueError, TypeError, KeyError, OverflowError):
        raise BasisError("BASIS_ACTIONS_CHANGED") from None


def require_current(con: Connection, owner_id: UUID, identifier: UUID) -> StoredBasis:
    """Fence a new use inside its writer; stored existence and historical reads are not grants."""
    _writer(con, owner_id)
    record = get(con, owner_id, identifier)
    if record is None:
        raise BasisError("BASIS_ASSERTION_NOT_FOUND")
    _current(con, record.assertion)
    return record


def save(
    con: Connection,
    owner_id: UUID,
    assertion: SplitBasisAssertion,
    *,
    at: datetime,
    request_id: UUID,
) -> StoredBasis:
    """Append a server-minted declaration/audit; callers still owe HTTP session/CSRF checks."""
    _writer(con, owner_id)
    try:
        if (
            not isinstance(assertion, SplitBasisAssertion)
            or not isinstance(request_id, UUID)
            or not isinstance(at, datetime)
            or at.utcoffset() is None
        ):
            raise ValueError
        record = StoredBasis(
            owner_id=owner_id,
            assertion=SplitBasisAssertion.model_validate(assertion.model_dump(warnings="error")),
            request_id=request_id,
            created_at=at.astimezone(UTC),
        )
        values = _flat(record)
    except (ValueError, TypeError, OverflowError, PydanticSerializationError):
        raise BasisError("INVALID_BASIS_ASSERTION") from None
    existing = con.execute(
        text("SELECT 1 FROM dividend_basis_assertions WHERE id=:id"), {"id": str(record.id)}
    ).first()
    if existing is not None:
        original = get(con, owner_id, record.id)
        if original is None or _flat(original) != values:
            raise BasisError("BASIS_ASSERTION_CONFLICT")
        return original
    _current(con, record.assertion)
    con.execute(
        text(
            f"INSERT INTO dividend_basis_assertions({','.join(values)}) "
            f"VALUES ({','.join(':' + name for name in values)})"
        ),
        values,
    )
    audit(
        con,
        str(owner_id),
        "dividend_basis_assertion",
        str(record.id),
        "DIVIDEND_BASIS_ASSERTED",
        request_id=str(request_id),
        after=_audit_values(record),
        reason=record.assertion.reason,
    )
    return record
