"""Protected basis declarations are immutable owned history, not canonical financial adoption."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Response
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text

import spm_api.dividend_basis_service as service
from spm_api.audit import canonical, now
from spm_api.basis_models import (
    BasisActionPin,
    BasisActionSnapshot,
    DividendBasisCreate,
    DividendBasisRecord,
)
from spm_api.cited_models import CitationInput
from spm_api.db import unit_of_work
from spm_api.dividend_basis import SplitBasisAssertion, SplitReference
from spm_api.errors import ApiError, Error
from spm_api.master_service import security_row
from spm_api.mutations import mutate
from spm_api.observation_service import original_dividend, public_citation
from spm_api.pagination import Page, PageDep, paginate, validate_time_page
from spm_api.security import Auth, AuthDep, WriteDep

router = APIRouter(
    prefix="/api/v1/dividends/basis-assertions",
    tags=["Dividend basis declarations"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 422, 503)},
)


def _public(record: service.StoredBasis) -> DividendBasisRecord:
    assertion = record.assertion
    citation = public_citation(assertion)
    assert citation is not None
    return DividendBasisRecord(
        id=record.id,
        observation_id=assertion.observation_id,
        source_sha256=assertion.source_sha256,
        security_id=assertion.security_id,
        ex_date=assertion.ex_date,
        share_basis_date=assertion.share_basis_date,
        source_basis=assertion.source_basis,
        target_basis=assertion.target_basis,
        complete=True,
        split_only=True,
        actions=tuple(
            BasisActionSnapshot(
                action_id=ref.id,
                action_root_id=ref.root_id,
                security_id=ref.security_id,
                observed_version=ref.version,
                action_type=ref.action_type,
                effective_date=ref.effective_date,
                ratio_numerator=ref.ratio_numerator,
                ratio_denominator=ref.ratio_denominator,
                observed_status=ref.status,
                observed_lifecycle_status=ref.lifecycle_status,
            )
            for ref in assertion.splits
        ),
        citation=citation,
        created_at=record.created_at,
    )


def _owned(con: Connection, owner: UUID, identifier: UUID) -> service.StoredBasis:
    record = service.get(con, owner, identifier)
    if record is None:
        raise service.BasisError("BASIS_ASSERTION_NOT_FOUND")
    return record


def _prepare(con: Connection, body: DividendBasisCreate) -> SplitBasisAssertion:
    source = original_dividend(con, body.observation_id)
    if source is None:
        raise ApiError(404, "NOT_FOUND", "Original source observation not found")
    if source.normalized_sha256 != body.source_sha256:
        raise service.BasisError("BASIS_SOURCE_CHANGED")
    if source.ex_date is None or source.adjustment_basis != "SPLIT_ADJUSTED":
        raise ApiError(
            422, "INVALID_BASIS_SOURCE", "Select an explicit dated split-adjusted source"
        )
    rows = (
        con.execute(
            text("""
        SELECT c.* FROM corporate_actions c JOIN json_each(:pins) p
          ON c.id=json_extract(p.value,'$.action_id') ORDER BY c.effective_date,c.id
    """),
            {"pins": canonical([pin.model_dump(mode="json") for pin in body.actions])},
        )
        .mappings()
        .all()
    )
    versions = {str(pin.action_id): pin.version for pin in body.actions}
    if len(rows) != len(versions) or any(
        row["version"] != versions[row["id"]]
        or row["is_current"] != 1
        or row["status"] != "CONFIRMED"
        or row["action_type"] not in {"SPLIT", "REVERSE_SPLIT"}
        for row in rows
    ):
        raise service.BasisError("BASIS_ACTIONS_CHANGED")
    try:
        refs = tuple(
            SplitReference(
                id=row["id"],
                root_id=row["root_id"],
                security_id=row["security_id"],
                version=row["version"],
                action_type=row["action_type"],
                effective_date=row["effective_date"],
                ratio_numerator=row["ratio_num"],
                ratio_denominator=row["ratio_den"],
                status="CONFIRMED",
                lifecycle_status="CURRENT",
            )
            for row in rows
        )
        return SplitBasisAssertion(
            id=uuid4(),
            observation_id=body.observation_id,
            source_sha256=body.source_sha256,
            security_id=source.security_id,
            ex_date=source.ex_date,
            share_basis_date=body.share_basis_date,
            source_basis=body.source_basis,
            target_basis=body.target_basis,
            complete=body.complete,
            split_only=body.split_only,
            splits=refs,
            **body.citation.model_dump(),
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ApiError(
            422, "INVALID_BASIS_SCOPE", "Review the explicit source and action scope"
        ) from None


@router.post("", status_code=201, operation_id="create_dividend_basis_assertion")
def create_basis(
    body: DividendBasisCreate, request: Request, response: Response, headers: WriteDep
) -> DividendBasisRecord:
    """Derive snapshots from real current rows, not caller-supplied ownership or ratios."""

    def record(con: Connection, auth: Auth) -> DividendBasisRecord:
        assertion = _prepare(con, body)
        return _public(
            service.save(
                con, auth.owner.id, assertion, at=now(), request_id=UUID(request.state.request_id)
            )
        )

    def validate_replay(con: Connection, auth: Auth, cached: DividendBasisRecord) -> None:
        historical = service.get(con, auth.owner.id, cached.id)
        if historical is None or _public(historical) != cached:
            raise service.BasisError("BASIS_ASSERTION_UNAVAILABLE")
        assertion = historical.assertion
        original_input = DividendBasisCreate(
            observation_id=assertion.observation_id,
            source_sha256=assertion.source_sha256,
            share_basis_date=assertion.share_basis_date,
            source_basis=assertion.source_basis,
            target_basis=assertion.target_basis,
            complete=assertion.complete,
            split_only=assertion.split_only,
            actions=tuple(
                BasisActionPin(action_id=ref.id, version=ref.version) for ref in assertion.splits
            ),
            citation=CitationInput(
                citation=assertion.citation, reason=assertion.reason, url=assertion.url
            ),
        )
        if original_input != body:
            raise service.BasisError("BASIS_ASSERTION_UNAVAILABLE")

    try:
        result = mutate(
            request,
            headers,
            body.model_dump(mode="json"),
            DividendBasisRecord,
            record,
            status=201,
            validate_replay=validate_replay,
        )
    except service.BasisError:
        raise
    except (ValueError, PydanticSerializationError):
        raise ApiError(
            503, "BASIS_ASSERTION_UNAVAILABLE", "The basis declaration cannot be retained safely"
        ) from None
    response.headers["Location"] = f"/api/v1/dividends/basis-assertions/{result.id}"
    return result


@router.get("", operation_id="list_dividend_basis_assertions")
def list_basis(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    observation_id: UUID | None = None,
    security_id: UUID | None = None,
) -> Page[DividendBasisRecord]:
    validate_time_page(page)
    with unit_of_work(request.app.state.engine) as con:
        if security_id is not None:
            security_row(con, str(security_id))
        if observation_id is not None and original_dividend(con, observation_id) is None:
            raise ApiError(404, "NOT_FOUND", "Original source observation not found")
        rows = (
            con.execute(
                text(r"""
            SELECT b.* FROM dividend_basis_assertions b
            LEFT JOIN securities s ON s.id=b.security_id
            WHERE b.owner_id=:owner AND (:source IS NULL OR b.observation_id=:source)
              AND (:security IS NULL OR b.security_id=:security)
              AND (b.observation_id LIKE :search ESCAPE '\'
                OR s.display_ticker LIKE :search ESCAPE '\' OR s.name LIKE :search ESCAPE '\')
              AND (:cursor_value IS NULL OR (b.created_at,b.id)<(:cursor_value,:cursor_id))
            ORDER BY b.created_at DESC,b.id DESC LIMIT :limit
        """),
                {
                    **page.params(),
                    "owner": str(auth.owner.id),
                    "source": str(observation_id) if observation_id is not None else None,
                    "security": str(security_id) if security_id is not None else None,
                },
            )
            .mappings()
            .all()
        )

        def convert(row: RowMapping) -> DividendBasisRecord:
            try:
                identifier = UUID(row["id"])
                if str(identifier) != row["id"]:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise service.BasisError("BASIS_ASSERTION_UNAVAILABLE") from None
            return _public(_owned(con, auth.owner.id, identifier))

        return paginate(rows, page, "created_at", convert)


@router.get("/{id}", operation_id="get_dividend_basis_assertion")
def get_basis(id: UUID, request: Request, auth: AuthDep) -> DividendBasisRecord:
    with unit_of_work(request.app.state.engine) as con:
        return _public(_owned(con, auth.owner.id, id))
